"""Edit a board file that KiCad does not have open: the file channel for milestone 3.

Same contract as the schematic editor: a concrete syntax tree keeps every untouched byte,
writes are atomic and snapshotted, a lock file or a board this process has seen live over
the API means refusal. Zones cannot be filled here; ``kicad-cli pcb drc --refill-zones
--save-board`` does that afterwards, and the tools say so.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
import uuid as uuidlib
from dataclasses import dataclass
from pathlib import Path

from kicad_layer.cst import CNode, is_clean, make, mark_dirty, parse_cst, render_file, to_cnode
from kicad_layer.errors import EDIT_CONFLICT, INVALID_ARGUMENT, NOT_FOUND_IN_DESIGN, SCHEMATIC_LOCKED, LayerError
from kicad_layer.kicad_libs import Footprint, load_footprint
from kicad_layer.pcb_writer import BoardBuilder
from kicad_layer.sexpr import S, Sym, child, children, tag, value

Point = tuple[float, float]


def new_uuid() -> str:
    return str(uuidlib.uuid4())


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


@dataclass
class FootprintView:
    node: CNode
    uuid: str
    lib_id: str
    reference: str
    value: str
    at: Point
    rotation: float
    layer: str


@dataclass
class SaveResult:
    path: Path
    snapshot: Path | None
    sha256_before: str
    sha256_after: str
    changed: bool


def _carry_zones(fp_node: CNode, old_at: Point, old_rot: float, new_at: Point, new_rot: float) -> int:
    """Move the zones inside a footprint with it (their points are board coordinates); the count moved."""
    from kicad_layer.kicad_libs import rotate_about

    if old_at == tuple(new_at) and (old_rot - new_rot) % 360 == 0:
        return 0
    n = 0
    for z in children(fp_node, "zone"):
        for holder in children(z, "polygon") + children(z, "filled_polygon"):
            pts = child(holder, "pts")
            for xy in children(pts, "xy") if pts is not None else []:
                lx, ly = rotate_about(float(xy[1]) - old_at[0], float(xy[2]) - old_at[1], -old_rot, 0.0, 0.0)
                x, y = rotate_about(lx, ly, new_rot, new_at[0], new_at[1])
                xy[1:3] = [S("x", x)[1], S("x", y)[1]]
                mark_dirty(xy)  # type: ignore[arg-type]
            if pts is not None:
                mark_dirty(pts)  # type: ignore[arg-type]
        n += 1
    return n


def outline_pieces(points: list[Point], corner_radius: float = 0.0) -> list[tuple]:
    """A closed outline as ("line", a, b) and ("arc", start, mid, end) pieces, each corner rounded
    with ``corner_radius`` (tangent arcs, so every piece ends where the next begins)."""
    import math

    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) > 1 and math.dist(pts[0], pts[-1]) < 1e-9:
        pts = pts[:-1]
    if len(pts) < 3:
        raise LayerError(INVALID_ARGUMENT, "An outline needs at least three corners.")
    n = len(pts)
    if corner_radius <= 0:
        return [("line", pts[i], pts[(i + 1) % n]) for i in range(n)]
    corners = []  # per vertex: (tangent point on the incoming edge, arc mid, tangent point on the outgoing edge)
    for i in range(n):
        p, v, q = pts[i - 1], pts[i], pts[(i + 1) % n]
        a = (p[0] - v[0], p[1] - v[1])
        b = (q[0] - v[0], q[1] - v[1])
        la, lb = math.hypot(*a), math.hypot(*b)
        if la < 1e-9 or lb < 1e-9:
            raise LayerError(INVALID_ARGUMENT, f"Corner {i} repeats a point.")
        ua, ub = (a[0] / la, a[1] / la), (b[0] / lb, b[1] / lb)
        cos_t = max(-1.0, min(1.0, ua[0] * ub[0] + ua[1] * ub[1]))
        theta = math.acos(cos_t)  # the angle between the two edges at the vertex
        if theta > math.pi - 1e-6:
            corners.append((v, None, v))  # a straight run: nothing to round
            continue
        d = corner_radius / math.tan(theta / 2)
        if d > min(la, lb) / 2 + 1e-9:
            raise LayerError(INVALID_ARGUMENT, f"corner_radius_mm {corner_radius} does not fit at corner {i} ({v[0]}, {v[1]}).",
                             hint="Use a smaller radius or longer edges.")
        t1 = (v[0] + ua[0] * d, v[1] + ua[1] * d)
        t2 = (v[0] + ub[0] * d, v[1] + ub[1] * d)
        bis = (ua[0] + ub[0], ua[1] + ub[1])
        lbis = math.hypot(*bis)
        k = corner_radius / math.sin(theta / 2) - corner_radius  # vertex to the arc's middle
        mid = (v[0] + bis[0] / lbis * k, v[1] + bis[1] / lbis * k)
        corners.append((t1, mid, t2))
    out: list[tuple] = []
    for i in range(n):
        t1, mid, t2 = corners[i]
        if mid is not None:
            out.append(("arc", t1, mid, t2))
        nxt = corners[(i + 1) % n][0]
        if math.dist(t2, nxt) > 1e-9:
            out.append(("line", t2, nxt))
    return [(piece[0], *[(round(c[0], 6), round(c[1], 6)) for c in piece[1:]]) for piece in out]


EDGE_ITEMS = ("gr_line", "gr_arc", "gr_rect", "gr_poly", "gr_circle", "gr_curve")


class BoardFile:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.source = self.path.read_text(encoding="utf-8")
        self.root: CNode = parse_cst(self.source)
        if tag(self.root) != "kicad_pcb":
            raise LayerError(EDIT_CONFLICT, f"{self.path.name} is not a board.")
        self.sha_loaded = sha256_text(self.source)

    # -- queries ----------------------------------------------------------------------

    def footprints(self) -> list[FootprintView]:
        out = []
        for n in children(self.root, "footprint"):
            props = {str(p[1]): str(p[2]) for p in children(n, "property") if len(p) > 2}
            at = child(n, "at") or []
            out.append(
                FootprintView(
                    node=n, uuid=value(n, "uuid") or "", lib_id=str(n[1]), reference=props.get("Reference", ""),
                    value=props.get("Value", ""), at=(float(at[1]), float(at[2])) if len(at) > 2 else (0.0, 0.0),
                    rotation=float(at[3]) if len(at) > 3 else 0.0, layer=value(n, "layer") or "F.Cu",
                )
            )
        return out

    def find(self, ref: str) -> FootprintView:
        for f in self.footprints():
            if f.reference == ref:
                return f
        refs = sorted(f.reference for f in self.footprints())
        raise LayerError(NOT_FOUND_IN_DESIGN, f"No footprint {ref!r} on {self.path.name}.", hint=f"References: {', '.join(refs[:40])}")

    def find_uuid(self, item_uuid: str) -> CNode:
        for c in self.root:
            if isinstance(c, list) and value(c, "uuid") == item_uuid:
                return c  # type: ignore[return-value]
        raise LayerError(NOT_FOUND_IN_DESIGN, f"No item with uuid {item_uuid} on {self.path.name}.")

    # -- edits ------------------------------------------------------------------------

    def move_footprint(self, ref: str, at: Point | None = None, rotation: float | None = None) -> FootprintView:
        """Move and/or rotate. Child coordinates are footprint-local, so only the header and the
        absolute angles carried by pads and texts change, except zones: a zone inside a footprint (a
        keep-out, an antenna clearance) is stored in board coordinates and is carried along here."""
        fp = self.find(ref)
        at_node = child(fp.node, "at")
        if at_node is None:
            raise LayerError(EDIT_CONFLICT, f"{ref} has no position.")
        new_x, new_y = at if at is not None else fp.at
        new_rot = rotation if rotation is not None else fp.rotation
        delta = (new_rot - fp.rotation) % 360
        _carry_zones(fp.node, fp.at, fp.rotation, (new_x, new_y), new_rot)
        del at_node[1:]
        at_node.extend([S("x", new_x)[1], S("x", new_y)[1]])
        if new_rot % 360:
            at_node.append(S("x", new_rot % 360)[1])
        mark_dirty(at_node)  # type: ignore[arg-type]
        if delta:
            for c in fp.node:
                if not isinstance(c, list):
                    continue
                t = tag(c)
                target = None
                if t == "pad":
                    target = child(c, "at")
                elif t in ("property", "fp_text"):
                    target = child(c, "at")
                if target is not None and len(target) > 2:
                    base = float(target[3]) if len(target) > 3 else 0.0
                    del target[3:]
                    ang = (base + delta) % 360
                    if ang:
                        target.append(S("x", ang)[1])
                    mark_dirty(target)  # type: ignore[arg-type]
        return self.find(ref)

    def move_footprints(self, moves: list[tuple[str, Point | None, float | None]]) -> list[tuple[FootprintView, FootprintView]]:
        """Several moves on this one tree, for one save: (before, after) per move. Every reference is
        checked before anything changes, so an unknown one leaves the board untouched."""
        refs = [m[0] for m in moves]
        dup = sorted({r for r in refs if refs.count(r) > 1})
        if dup:
            raise LayerError(INVALID_ARGUMENT, f"Each footprint may be moved once per batch; repeated: {', '.join(dup)}.")
        known = {f.reference for f in self.footprints()}
        missing = [r for r in refs if r not in known]
        if missing:
            raise LayerError(NOT_FOUND_IN_DESIGN, f"No footprint {', '.join(missing)} on {self.path.name}; nothing was moved.",
                             hint=f"References: {', '.join(sorted(known)[:40])}")
        out = []
        for ref, at, rotation in moves:
            before = self.find(ref)
            out.append((before, self.move_footprint(ref, at, rotation)))
        return out

    def place_footprint(self, lib_id: str, ref: str, at: Point, rotation: float = 0.0, *, value_text: str = "", pad_nets: dict[str, str] | None = None,
                        sheetfile: str = "", path_uuid: str = "", layer: str = "F.Cu") -> FootprintView:
        if any(f.reference == ref for f in self.footprints()):
            raise LayerError(EDIT_CONFLICT, f"Reference {ref} already exists on {self.path.name}.")
        lib, _, name = lib_id.partition(":")
        fp: Footprint = load_footprint(lib, name)
        builder = BoardBuilder(sheetfile=sheetfile or "")
        node = builder.footprint(fp, ref, value_text or name, at, rotation, path_uuid=path_uuid or new_uuid(), pad_nets=pad_nets or {}, hide_ref=False, layer=layer)
        if not path_uuid:
            # a footprint with no schematic symbol behind it must not claim a path
            node[:] = [c for c in node if not (isinstance(c, list) and tag(c) in ("path", "sheetname", "sheetfile"))]
        cnode = to_cnode(node)
        self._insert_after_last("footprint", cnode)
        return self.find(ref)

    def add_segment(self, a: Point, b: Point, *, width: float, layer: str, net: str) -> str:
        u = new_uuid()
        self._insert_after_last("segment", make("segment", S("start", a[0], a[1]), S("end", b[0], b[1]), S("width", width), S("layer", layer), S("net", net), S("uuid", u)))
        return u

    def add_via(self, p: Point, *, net: str, size: float = 0.8, drill: float = 0.3) -> str:
        u = new_uuid()
        self._insert_after_last("via", make("via", S("at", p[0], p[1]), S("size", size), S("drill", drill), S("layers", "F.Cu", "B.Cu"), S("net", net), S("uuid", u)))
        return u

    def add_zone(self, polygon: list[Point], *, net: str, layer: str, name: str = "", clearance: float = 0.2, min_thickness: float = 0.25, priority: int = 0) -> str:
        if len(polygon) < 3:
            raise LayerError(INVALID_ARGUMENT, "A zone needs at least three points.")
        u = new_uuid()
        z = S("zone", S("net", net), S("layer", layer), S("uuid", u))
        if name:
            z.append(S("name", name))
        z.append(S("hatch", Sym("edge"), 0.508))
        if priority:
            z.append(S("priority", priority))
        z += [S("connect_pads", S("clearance", clearance)), S("min_thickness", min_thickness), S("filled_areas_thickness", Sym("no")),
              S("fill", Sym("yes"), S("thermal_gap", 0.3), S("thermal_bridge_width", 0.4), S("island_removal_mode", 0)),
              S("polygon", S("pts", *[S("xy", x, y) for x, y in polygon]))]
        self._insert_after_last("zone", to_cnode(z))
        return u

    def set_outline(self, points: list[Point], *, corner_radius: float = 0.0, replace: bool = True, width: float = 0.05) -> dict:
        """Write the board outline on Edge.Cuts as gr_line and gr_arc (start, mid, end) items, KiCad 10 style.
        ``replace`` removes every Edge.Cuts drawing first (the board's, not footprints')."""
        pieces = outline_pieces(points, corner_radius)
        removed = []
        if replace:
            for c in list(self.root):
                if isinstance(c, list) and tag(c) in EDGE_ITEMS and value(c, "layer") == "Edge.Cuts":
                    self.root.remove(c)
                    removed.append(value(c, "uuid") or "")
            mark_dirty(self.root)
        stroke = S("stroke", S("width", width), S("type", Sym("default")))
        added = []
        for piece in pieces:
            u = new_uuid()
            if piece[0] == "line":
                node = make("gr_line", S("start", *piece[1]), S("end", *piece[2]), stroke, S("layer", "Edge.Cuts"), S("uuid", u))
            else:
                node = make("gr_arc", S("start", *piece[1]), S("mid", *piece[2]), S("end", *piece[3]), stroke, S("layer", "Edge.Cuts"), S("uuid", u))
            self._insert_after_last(piece[0], node)
            added.append({"kind": piece[0], "id": u, "points": [list(p) for p in piece[1:]]})
        return {"items": added, "removed": removed}

    def add_mounting_hole(self, at: Point, *, drill: float, pad: float = 0.0, net: str = "", ref: str = "") -> FootprintView:
        """A mounting hole as an inline footprint, board-only and out of the BOM and position files:
        plated with a pad of ``pad`` mm (on ``net`` when given) when ``pad`` exceeds the drill, else a bare NPTH."""
        if drill <= 0:
            raise LayerError(INVALID_ARGUMENT, "drill must be positive.")
        if pad and pad < drill:
            raise LayerError(INVALID_ARGUMENT, f"pad {pad} mm is smaller than the drill {drill} mm.")
        taken = {f.reference for f in self.footprints()}
        if not ref:
            n = 1
            while f"H{n}" in taken:
                n += 1
            ref = f"H{n}"
        elif ref in taken:
            raise LayerError(EDIT_CONFLICT, f"Reference {ref} already exists on {self.path.name}.")
        plated = pad > drill
        size = pad if plated else drill
        name = f"MountingHole_{drill:g}mm" + ("_Pad" if plated else "")
        font = S("effects", S("font", S("size", 1, 1), S("thickness", 0.15)))
        small = S("effects", S("font", S("size", 1.27, 1.27)))
        off = round(size / 2 + 1.0, 4)
        pad_node = S("pad", "1" if plated else "", Sym("thru_hole" if plated else "np_thru_hole"), Sym("circle"), S("at", 0, 0), S("size", size, size),
                     S("drill", drill), S("layers", "*.Cu", "*.Mask"))
        if plated:
            pad_node.append(S("remove_unused_layers", Sym("no")))
            if net:
                pad_node.append(S("net", net))
        pad_node.append(S("uuid", new_uuid()))
        fp = S("footprint", f"MountingHole:{name}", S("layer", "F.Cu"), S("uuid", new_uuid()), S("at", at[0], at[1]),
               S("descr", f"Mounting hole, drill {drill:g} mm" + (f", pad {pad:g} mm" if plated else ", no annular ring")),
               S("property", "Reference", ref, S("at", 0, -off, 0), S("layer", "F.SilkS"), S("uuid", new_uuid()), font),
               S("property", "Value", name, S("at", 0, off, 0), S("layer", "F.Fab"), S("uuid", new_uuid()), font),
               S("property", "Datasheet", "", S("at", 0, 0, 0), S("layer", "F.Fab"), S("hide", Sym("yes")), S("uuid", new_uuid()), small),
               S("property", "Description", "", S("at", 0, 0, 0), S("layer", "F.Fab"), S("hide", Sym("yes")), S("uuid", new_uuid()), small),
               S("attr", Sym("board_only"), Sym("exclude_from_pos_files"), Sym("exclude_from_bom")),
               S("fp_circle", S("center", 0, 0), S("end", round(size / 2, 4), 0), S("stroke", S("width", 0.15), S("type", Sym("solid"))), S("fill", Sym("no")),
                 S("layer", "Cmts.User"), S("uuid", new_uuid())),
               S("fp_circle", S("center", 0, 0), S("end", round(size / 2 + 0.25, 4), 0), S("stroke", S("width", 0.05), S("type", Sym("solid"))), S("fill", Sym("no")),
                 S("layer", "F.CrtYd"), S("uuid", new_uuid())),
               pad_node, S("embedded_fonts", Sym("no")))
        self._insert_after_last("footprint", to_cnode(fp))
        return self.find(ref)

    def delete(self, item_uuid: str) -> str:
        node = self.find_uuid(item_uuid)
        self.root.remove(node)
        mark_dirty(self.root)
        return tag(node) or ""

    def _insert_after_last(self, name: str, node: CNode) -> None:
        idx = None
        order = ["footprint", "gr_line", "gr_arc", "gr_circle", "gr_rect", "gr_poly", "gr_text", "segment", "arc", "via", "zone", "group"]
        rank = order.index(name) if name in order else len(order)
        for i, c in enumerate(self.root):
            if not isinstance(c, list):
                continue
            t = tag(c)
            if t in ("embedded_fonts", "embedded_files"):
                if idx is None:
                    idx = i
                break
            if t in order and order.index(t) <= rank:
                idx = i + 1
        self.root.insert(idx if idx is not None else len(self.root), node)
        mark_dirty(self.root)

    # -- save ---------------------------------------------------------------------------

    def render(self) -> str:
        return render_file(self.root, self.source)

    def is_modified(self) -> bool:
        return not is_clean(self.root)

    def lock_file(self) -> Path:
        return self.path.parent / f"~{self.path.name}.lck"

    def save(self, *, force: bool = False) -> SaveResult:
        if self.lock_file().exists() and not force:
            raise LayerError(SCHEMATIC_LOCKED, f"KiCad has {self.path.name} open (lock file exists).",
                             hint="Close the PCB Editor and retry, or use the live board tools while it is open.")
        current = self.path.read_text(encoding="utf-8")
        if sha256_text(current) != self.sha_loaded:
            raise LayerError(EDIT_CONFLICT, f"{self.path.name} changed on disk since it was read.", hint="Reload and repeat the edit.", retryable=True)
        text = self.render()
        if text == self.source:
            return SaveResult(self.path, None, self.sha_loaded, self.sha_loaded, False)
        snap_dir = self.path.parent / ".kicad-layer" / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        snap = snap_dir / f"{self.path.name}.{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(self.path, snap)
        files = sorted(snap_dir.glob("*"), key=lambda p: p.stat().st_mtime)
        for old in files[:-30]:
            old.unlink(missing_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.kicad-layer.tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        after = sha256_text(text)
        result = SaveResult(self.path, snap, self.sha_loaded, after, True)
        self.source, self.root, self.sha_loaded = text, parse_cst(text), after
        return result
