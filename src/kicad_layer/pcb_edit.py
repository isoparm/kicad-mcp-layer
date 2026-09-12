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
        absolute angles carried by pads and texts change."""
        fp = self.find(ref)
        at_node = child(fp.node, "at")
        if at_node is None:
            raise LayerError(EDIT_CONFLICT, f"{ref} has no position.")
        new_x, new_y = at if at is not None else fp.at
        new_rot = rotation if rotation is not None else fp.rotation
        delta = (new_rot - fp.rotation) % 360
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
