"""Edit existing KiCad 10 schematics without disturbing what is not edited.

Every operation works on the concrete syntax tree from :mod:`kicad_layer.cst`; saving
writes untouched nodes back verbatim. Writes are atomic, refuse while KiCad holds the
sheet's lock file, and snapshot the previous file first. Connectivity is geometric, so
callers place things on the 1.27 mm grid and this module reports pin positions to help.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path

from kicad_layer.cst import CNode, is_clean, make, mark_dirty, parse_cst, render_file, to_cnode
from kicad_layer.errors import EDIT_CONFLICT, NOT_FOUND_IN_DESIGN, SCHEMATIC_LOCKED, LayerError
from kicad_layer.kicad_libs import Symbol, load_symbol_from, transform_point
from kicad_layer.libtables import symbol_lib_path_for
from kicad_layer.paths import locate_project
from kicad_layer.sexpr import S, Sym, atoms, child, children, tag, value

Point = tuple[float, float]
GRID = 1.27

ITEM_ORDER = [
    "rectangle", "text", "text_box", "junction", "no_connect", "bus_entry", "wire", "bus", "polyline",
    "image", "label", "global_label", "hierarchical_label", "netclass_flag", "rule_area", "symbol", "sheet",
]
TAIL_TAGS = {"sheet_instances", "embedded_fonts", "embedded_files"}


def new_uuid() -> str:
    return str(uuidlib.uuid4())


def snap(v: float, grid: float = GRID) -> float:
    return round(round(v / grid) * grid, 4)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


@dataclass
class PlacedView:
    """A read-only view of a placed symbol."""

    node: CNode
    uuid: str
    lib_id: str
    at: Point
    rotation: int
    mirror: str | None
    unit: int
    properties: dict[str, str]
    pin_uuids: dict[str, str] = field(default_factory=dict)

    @property
    def reference(self) -> str:
        return self.properties.get("Reference", "")


@dataclass
class SaveResult:
    path: Path
    snapshot: Path | None
    bytes_before: int
    bytes_after: int
    sha256_before: str
    sha256_after: str
    changed: bool


class Schematic:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.source = self.path.read_text(encoding="utf-8")
        self.root: CNode = parse_cst(self.source)
        if tag(self.root) != "kicad_sch":
            raise LayerError(EDIT_CONFLICT, f"{self.path.name} is not a schematic.")
        self.sha_loaded = sha256_text(self.source)
        self.project = locate_project(self.path)

    # -- basic queries -----------------------------------------------------------------

    @property
    def uuid(self) -> str:
        return value(self.root, "uuid") or ""

    def items(self, name: str) -> list[CNode]:
        return [c for c in self.root if isinstance(c, list) and tag(c) == name]

    def lib_symbols(self) -> CNode:
        node = child(self.root, "lib_symbols")
        if node is None:
            node = make("lib_symbols")
            self.root.insert(self._index_after_header(), node)
        return node  # type: ignore[return-value]

    def lib_symbol(self, lib_id: str) -> CNode | None:
        for s in children(self.lib_symbols(), "symbol"):
            if str(s[1]) == lib_id:
                return s  # type: ignore[return-value]
        return None

    def placed(self) -> list[PlacedView]:
        return [self._view(n) for n in self.items("symbol")]

    def _view(self, node: CNode) -> PlacedView:
        at = child(node, "at") or []
        mirror = child(node, "mirror")
        props = {str(p[1]): str(p[2]) for p in children(node, "property") if len(p) > 2}
        return PlacedView(
            node=node,
            uuid=value(node, "uuid") or "",
            lib_id=value(node, "lib_id") or "",
            at=(float(at[1]), float(at[2])) if len(at) > 2 else (0.0, 0.0),
            rotation=int(float(at[3])) if len(at) > 3 else 0,
            mirror=str(mirror[1]) if mirror is not None and len(mirror) > 1 else None,
            unit=int(value(node, "unit", default="1") or 1),
            properties=props,
            pin_uuids={str(p[1]): (value(p, "uuid") or "") for p in children(node, "pin")},
        )

    def find(self, ref: str) -> PlacedView:
        for v in self.placed():
            if v.reference == ref or self._instance_reference(v.node) == ref:
                return v
        refs = sorted({v.reference for v in self.placed() if not v.reference.startswith("#")})
        raise LayerError(NOT_FOUND_IN_DESIGN, f"No symbol with reference {ref!r} on {self.path.name}.", hint=f"References here: {', '.join(refs[:40])}")

    def find_uuid(self, item_uuid: str) -> CNode:
        for c in self.root:
            if isinstance(c, list) and value(c, "uuid") == item_uuid:
                return c  # type: ignore[return-value]
        raise LayerError(NOT_FOUND_IN_DESIGN, f"No item with uuid {item_uuid} on {self.path.name}.")

    def _instance_reference(self, node: CNode) -> str | None:
        inst = child(node, "instances")
        if inst is None:
            return None
        path = self.instance_path()
        for proj in children(inst, "project"):
            for p in children(proj, "path"):
                if str(p[1]) == path:
                    return value(p, "reference")
        return None

    # -- pins ---------------------------------------------------------------------------

    def symbol_definition(self, lib_id: str) -> Symbol:
        node = self.lib_symbol(lib_id)
        if node is None:
            raise LayerError(EDIT_CONFLICT, f"{lib_id} is used on the sheet but missing from its lib_symbols cache.")
        return Symbol.from_cache_entry(lib_id, node)

    def pin_positions(self, view: PlacedView) -> dict[str, tuple[float, float, str, str]]:
        """pin number -> (x, y, name, type) in sheet coordinates for one placed symbol."""
        sym = self.symbol_definition(view.lib_id)
        out = {}
        for p in sym.pins:
            if p.unit not in (0, view.unit):
                continue
            x, y = transform_point(p.x, p.y, view.at[0], view.at[1], view.rotation, view.mirror)
            out[p.number] = (x, y, p.name, p.etype)
        return out

    # -- instance path -----------------------------------------------------------------

    def instance_path(self) -> str:
        """``/<root uuid>[/<sheet uuid>]`` for this sheet within its project."""
        root_file = self.project.root_schematic
        if root_file is None or root_file.resolve() == self.path.resolve():
            return f"/{self.uuid}"
        root_text = root_file.read_text(encoding="utf-8")
        root_node = parse_cst(root_text)
        root_uuid = value(root_node, "uuid") or ""
        for sheet in children(root_node, "sheet"):
            props = {str(p[1]): str(p[2]) for p in children(sheet, "property") if len(p) > 2}
            if props.get("Sheetfile", "") == self.path.name:
                return f"/{root_uuid}/{value(sheet, 'uuid')}"
        return f"/{root_uuid}"

    # -- edits: symbols ----------------------------------------------------------------

    def set_property(self, ref: str, name: str, new_value: str) -> tuple[str | None, bool]:
        view = self.find(ref)
        for p in children(view.node, "property"):
            if str(p[1]) == name:
                old = str(p[2])
                if old == new_value:
                    return old, False
                p[2] = new_value
                mark_dirty(p)  # type: ignore[arg-type]
                if name == "Reference":
                    self._set_instance_reference(view.node, new_value)
                return old, True
        # new property: hidden, at the symbol position
        x, y = view.at
        prop = make("property", name, new_value, S("at", x, y, 0), S("hide", Sym("yes")), S("show_name", Sym("no")),
                    S("do_not_autoplace", Sym("no")), S("effects", S("font", S("size", 1.27, 1.27))))
        last = max(i for i, c in enumerate(view.node) if isinstance(c, list) and tag(c) == "property")
        view.node.insert(last + 1, prop)
        mark_dirty(view.node)
        return None, True

    def _set_instance_reference(self, node: CNode, ref: str) -> None:
        inst = child(node, "instances")
        if inst is None:
            return
        path = self.instance_path()
        for proj in children(inst, "project"):
            for p in children(proj, "path"):
                if str(p[1]) == path:
                    r = child(p, "reference")
                    if r is not None:
                        r[1] = ref
                        mark_dirty(r)  # type: ignore[arg-type]

    def add_symbol(
        self,
        lib_id: str,
        ref: str,
        at: Point,
        *,
        rotation: int = 0,
        mirror: str | None = None,
        value_text: str | None = None,
        footprint: str | None = None,
        unit: int = 1,
        project_dir: Path | None = None,
    ) -> tuple[str, dict[str, tuple[float, float, str, str]]]:
        """Place a library symbol; returns (uuid, pin positions)."""
        lib, _, name = lib_id.partition(":")
        cache = self.lib_symbol(lib_id)
        if cache is None:
            path = symbol_lib_path_for(lib, project_dir or self.project.directory)
            sym = load_symbol_from(path, lib, name)
            self.lib_symbols().append(to_cnode(sym.cache_entry()))
            mark_dirty(self.lib_symbols())
        sym = self.symbol_definition(lib_id)
        x, y = at
        sym_uuid = new_uuid()
        val = value_text if value_text is not None else sym.properties.get("Value", name)
        fp = footprint if footprint is not None else sym.properties.get("Footprint", "")
        node = make("symbol", S("lib_id", lib_id), S("at", x, y, rotation))
        if mirror:
            node.append(to_cnode(S("mirror", Sym(mirror))))
        for t, default in (("exclude_from_sim", "no"), ("in_bom", "yes"), ("on_board", "yes"), ("in_pos_files", "yes")):
            node.append(to_cnode(S(t, Sym(value(sym.tree, t, default=default) or default))))
        node.insert(2, to_cnode(S("unit", unit)))
        node.insert(3, to_cnode(S("body_style", 1)))
        node.append(to_cnode(S("dnp", Sym("no"))))
        node.append(to_cnode(S("uuid", sym_uuid)))

        def prop(pname: str, text: str, pos: Point, hide: bool) -> CNode:
            p = S("property", pname, text, S("at", pos[0], pos[1], 0))
            if hide:
                p.append(S("hide", Sym("yes")))
            p += [S("show_name", Sym("no")), S("do_not_autoplace", Sym("no")), S("effects", S("font", S("size", 1.27, 1.27)))]
            return to_cnode(p)

        node.append(prop("Reference", ref, (x, y - 2.54), False))
        node.append(prop("Value", val, (x, y + 2.54), False))
        node.append(prop("Footprint", fp, (x, y), True))
        node.append(prop("Datasheet", sym.properties.get("Datasheet", ""), (x, y), True))
        node.append(prop("Description", sym.properties.get("Description", ""), (x, y), True))
        for p in sym.pins:
            node.append(to_cnode(S("pin", p.number, S("uuid", new_uuid()))))
        node.append(to_cnode(S("instances", S("project", self.project.name, S("path", self.instance_path(), S("reference", ref), S("unit", unit))))))
        self._insert_item(node)
        view = self._view(node)
        return sym_uuid, self.pin_positions(view)

    # -- edits: connectivity ----------------------------------------------------------

    def add_wire(self, a: Point, b: Point) -> str:
        u = new_uuid()
        self._insert_item(make("wire", S("pts", S("xy", a[0], a[1]), S("xy", b[0], b[1])), S("stroke", S("width", 0), S("type", Sym("default"))), S("uuid", u)))
        return u

    def add_junction(self, p: Point) -> str:
        u = new_uuid()
        self._insert_item(make("junction", S("at", p[0], p[1]), S("diameter", 0), S("color", 0, 0, 0, 0), S("uuid", u)))
        return u

    def add_no_connect(self, p: Point) -> str:
        u = new_uuid()
        self._insert_item(make("no_connect", S("at", p[0], p[1]), S("uuid", u)))
        return u

    def add_label(self, text: str, p: Point, rotation: int = 0, kind: str = "local", shape: str = "input") -> str:
        u = new_uuid()
        justify = {0: ("left", "bottom"), 90: ("left", "bottom"), 180: ("right", "bottom"), 270: ("right", "bottom")}[rotation % 360]
        effects = S("effects", S("font", S("size", 1.27, 1.27)), S("justify", *[Sym(j) for j in justify]))
        if kind == "local":
            node = make("label", text, S("at", p[0], p[1], rotation), effects, S("uuid", u))
        elif kind == "global":
            node = make("global_label", text, S("shape", Sym(shape)), S("at", p[0], p[1], rotation), S("fields_autoplaced", Sym("yes")), effects, S("uuid", u),
                        S("property", "Intersheetrefs", "${INTERSHEET_REFS}", S("at", p[0], p[1], 0), S("hide", Sym("yes")), S("show_name", Sym("no")),
                          S("do_not_autoplace", Sym("no")), S("effects", S("font", S("size", 1.27, 1.27)))))
        elif kind == "hierarchical":
            node = make("hierarchical_label", text, S("shape", Sym(shape)), S("at", p[0], p[1], rotation), effects, S("uuid", u))
        else:
            raise LayerError(EDIT_CONFLICT, f"unknown label kind {kind!r}")
        self._insert_item(node)
        return u

    def wire_endpoints(self) -> list[tuple[Point, Point]]:
        out = []
        for w in self.items("wire"):
            pts = child(w, "pts")
            xy = children(pts, "xy") if pts is not None else []
            if len(xy) >= 2:
                out.append(((float(xy[0][1]), float(xy[0][2])), (float(xy[-1][1]), float(xy[-1][2]))))
        return out

    def junction_points(self) -> set[Point]:
        out = set()
        for j in self.items("junction"):
            at = child(j, "at")
            if at is not None:
                out.add((float(at[1]), float(at[2])))
        return out

    def needs_junction(self, p: Point) -> bool:
        """True when a point where a new wire ends touches existing wires in a way that needs a dot."""
        if p in self.junction_points():
            return False
        ends = 0
        mid = False
        for a, b in self.wire_endpoints():
            if p in (a, b):
                ends += 1
            elif _on_segment(p, a, b):
                mid = True
        return mid or ends >= 2

    # -- edits: delete and annotate ----------------------------------------------------

    def delete(self, item_uuid: str) -> str:
        node = self.find_uuid(item_uuid)
        self.root.remove(node)
        mark_dirty(self.root)
        return tag(node) or ""

    def annotate(self) -> dict[str, str]:
        """Give every ``X?`` reference on this sheet the next free number for its prefix."""
        used: dict[str, set[int]] = {}
        for v in self.placed():
            ref = v.reference
            prefix, num = _split_ref(ref)
            if num is not None:
                used.setdefault(prefix, set()).add(num)
        changes: dict[str, str] = {}
        for v in self.placed():
            ref = v.reference
            if not ref.endswith("?"):
                continue
            prefix = ref[:-1]
            n = 1
            while n in used.setdefault(prefix, set()):
                n += 1
            used[prefix].add(n)
            new = f"{prefix}{n}"
            self.set_property(ref, "Reference", new)
            changes[ref if ref not in changes else f"{ref}#{len(changes)}"] = new
        return changes

    # -- structure ----------------------------------------------------------------------

    def _index_after_header(self) -> int:
        idx = 1
        for i, c in enumerate(self.root):
            if isinstance(c, list) and tag(c) in ("version", "generator", "generator_version", "uuid", "paper", "title_block"):
                idx = i + 1
        return idx

    def _insert_item(self, node: CNode) -> None:
        """Insert a top-level item after the last item of its type (KiCad's own ordering)."""
        t = tag(node)
        order = {name: i for i, name in enumerate(ITEM_ORDER)}
        rank = order.get(t, len(ITEM_ORDER))
        insert_at = None
        for i, c in enumerate(self.root):
            if not isinstance(c, list):
                continue
            ct = tag(c)
            if ct in TAIL_TAGS:
                if insert_at is None:
                    insert_at = i
                break
            if ct in order and order[ct] <= rank:
                insert_at = i + 1
        if insert_at is None:
            insert_at = max(self._index_after_header(), len(self.root))
        self.root.insert(insert_at, node)
        mark_dirty(self.root)

    # -- output ---------------------------------------------------------------------

    def render(self) -> str:
        return render_file(self.root, self.source)

    def is_modified(self) -> bool:
        return not is_clean(self.root)

    def lock_file(self) -> Path:
        return self.path.parent / f"~{self.path.name}.lck"

    def save(self, *, force: bool = False, snapshot_dir: Path | None = None) -> SaveResult:
        if self.lock_file().exists() and not force:
            raise LayerError(
                SCHEMATIC_LOCKED,
                f"KiCad has {self.path.name} open (lock file {self.lock_file().name} exists).",
                hint="Close the Schematic Editor and retry, or pass force=true if you are sure nothing is unsaved there.",
            )
        current = self.path.read_text(encoding="utf-8")
        if sha256_text(current) != self.sha_loaded:
            raise LayerError(EDIT_CONFLICT, f"{self.path.name} changed on disk since it was read.", hint="Reload and repeat the edit.", retryable=True)
        text = self.render()
        if text == self.source:
            return SaveResult(self.path, None, len(current.encode("utf-8")), len(current.encode("utf-8")), self.sha_loaded, self.sha_loaded, False)
        snap_dir = snapshot_dir or (self.path.parent / ".kicad-layer" / "snapshots")
        snap_dir.mkdir(parents=True, exist_ok=True)
        snap = snap_dir / f"{self.path.name}.{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(self.path, snap)
        _prune(snap_dir, keep=30)
        tmp = self.path.with_name(f".{self.path.name}.kicad-layer.tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        after = sha256_text(text)
        result = SaveResult(self.path, snap, len(current.encode("utf-8")), len(text.encode("utf-8")), self.sha_loaded, after, True)
        self.source = text
        self.root = parse_cst(text)
        self.sha_loaded = after
        return result


def _prune(directory: Path, keep: int) -> None:
    files = sorted(directory.glob("*"), key=lambda p: p.stat().st_mtime)
    for p in files[:-keep]:
        p.unlink(missing_ok=True)


def _split_ref(ref: str) -> tuple[str, int | None]:
    i = len(ref)
    while i > 0 and ref[i - 1].isdigit():
        i -= 1
    return (ref[:i], int(ref[i:]) if i < len(ref) else None)


def _on_segment(p: Point, a: Point, b: Point, tol: float = 0.01) -> bool:
    (px, py), (ax, ay), (bx, by) = p, a, b
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > tol:
        return False
    return min(ax, bx) - tol <= px <= max(ax, bx) + tol and min(ay, by) - tol <= py <= max(ay, by) + tol
