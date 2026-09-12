"""Read symbols and footprints from KiCad's own libraries.

Symbols are flattened the way eeschema does before it caches them in a schematic: a
symbol that ``extends`` another takes the parent's graphics and pins and keeps its own
properties. Footprints are read as-is; pad positions are local to the footprint origin.
"""

from __future__ import annotations

import copy
import functools
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from kicad_layer.sexpr import Node, Sym, atoms, child, children, parse, tag, value


def share_dir() -> Path:
    for candidate in (
        Path(os.environ.get("KICAD10_SYMBOL_DIR", "")).parent if os.environ.get("KICAD10_SYMBOL_DIR") else None,
        Path(r"C:\Program Files\KiCad\10.0\share\kicad") if sys.platform == "win32" else None,
        Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport") if sys.platform == "darwin" else None,
        Path("/usr/share/kicad"),
    ):
        if candidate and (candidate / "symbols").is_dir():
            return candidate
    raise FileNotFoundError("KiCad share directory with symbols/ and footprints/ not found")


def symbol_lib_path(lib: str) -> Path:
    return share_dir() / "symbols" / f"{lib}.kicad_sym"


def footprint_path(lib: str, name: str) -> Path:
    d = _fp_dirs.get(lib)
    if d is not None:
        return d / f"{name}.kicad_mod"
    return share_dir() / "footprints" / f"{lib}.pretty" / f"{name}.kicad_mod"


_fp_dirs: dict[str, Path] = {}


def register_footprint_lib(lib: str, pretty_dir: Path) -> None:
    """Resolve footprints of nickname ``lib`` from an explicit ``.pretty`` directory (a project library)."""
    pretty_dir = Path(pretty_dir)
    if _fp_dirs.get(lib) != pretty_dir:
        _fp_dirs[lib] = pretty_dir
        load_footprint.cache_clear()


def register_symbol_lib(lib: str, path: Path) -> None:
    """Resolve symbols of nickname ``lib`` from an explicit ``.kicad_sym`` file (a project library)."""
    path = Path(path)
    if _lib_paths.get(lib) != path:
        _lib_paths[lib] = path
        _load_lib.cache_clear()
        load_symbol.cache_clear()


@dataclass
class Pin:
    number: str
    name: str
    etype: str
    shape: str
    x: float
    y: float
    rotation: float
    length: float
    hidden: bool
    unit: int

    @property
    def end(self) -> tuple[float, float]:
        """The connection point (tip) of the pin in symbol coordinates (Y up)."""
        return (self.x, self.y)


@dataclass
class Symbol:
    lib: str
    name: str
    tree: Node  # flattened, named "Name"
    pins: list[Pin] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)
    power: bool = False

    @property
    def lib_id(self) -> str:
        return f"{self.lib}:{self.name}"

    def pin(self, number: str) -> Pin:
        for p in self.pins:
            if p.number == number:
                return p
        raise KeyError(f"{self.lib_id} has no pin {number!r}")

    def pins_named(self, name: str) -> list[Pin]:
        return [p for p in self.pins if p.name == name]

    def cache_entry(self) -> Node:
        """A copy renamed ``Lib:Name`` for a schematic's lib_symbols block."""
        entry = copy.deepcopy(self.tree)
        entry[1] = self.lib_id
        return entry

    @classmethod
    def from_cache_entry(cls, lib_id: str, entry: Node) -> "Symbol":
        """Rebuild a Symbol from a schematic's lib_symbols entry (already flattened)."""
        lib, _, name = lib_id.partition(":")
        tree = copy.deepcopy(entry)
        tree[1] = name
        # sub-symbols in a cache are named after the bare symbol name
        props = {str(p[1]): str(p[2]) for p in children(tree, "property") if len(p) > 2}
        return cls(lib=lib, name=name, tree=tree, pins=_parse_pins(tree), properties=props, power=child(tree, "power") is not None)


_lib_paths: dict[str, Path] = {}


@functools.lru_cache(maxsize=64)
def _load_lib(lib: str) -> dict[str, Node]:
    path = _lib_paths.get(lib) or symbol_lib_path(lib)
    root = parse(path.read_text(encoding="utf-8"))
    out: dict[str, Node] = {}
    for node in children(root, "symbol"):
        out[str(node[1])] = node
    return out


def load_symbol_from(path: Path, lib: str, name: str) -> "Symbol":
    """Load ``name`` from an explicit ``.kicad_sym`` file under the nickname ``lib``."""
    register_symbol_lib(lib, path)
    return load_symbol(lib, name)


def _rename_subsymbols(tree: Node, old: str, new: str) -> None:
    for sub in children(tree, "symbol"):
        subname = str(sub[1])
        if subname.startswith(old + "_"):
            sub[1] = new + subname[len(old):]


def _flatten(lib: str, name: str, seen: tuple[str, ...] = ()) -> Node:
    table = _load_lib(lib)
    if name not in table:
        raise KeyError(f"symbol {lib}:{name} not found in {symbol_lib_path(lib)}")
    node = copy.deepcopy(table[name])
    ext = child(node, "extends")
    if ext is None:
        return node
    parent_name = str(ext[1])
    if parent_name in seen:
        raise ValueError(f"extends cycle at {lib}:{name}")
    parent = _flatten(lib, parent_name, seen + (name,))
    # Start from the parent (graphics, pins, flags), then apply the child's properties.
    merged = copy.deepcopy(parent)
    merged[1] = name
    _rename_subsymbols(merged, parent_name, name)
    child_props = {str(p[1]): p for p in children(node, "property")}
    for i, c in enumerate(merged):
        if isinstance(c, list) and tag(c) == "property" and str(c[1]) in child_props:
            merged[i] = child_props.pop(str(c[1]))
    for p in child_props.values():
        # insert after the last existing property
        idx = max((i for i, c in enumerate(merged) if isinstance(c, list) and tag(c) == "property"), default=1)
        merged.insert(idx + 1, p)
    # child may override a few flags
    for flag in ("exclude_from_sim", "in_bom", "on_board", "in_pos_files"):
        c = child(node, flag)
        if c is not None:
            for i, m in enumerate(merged):
                if isinstance(m, list) and tag(m) == flag:
                    merged[i] = c
    return merged


def _parse_pins(tree: Node) -> list[Pin]:
    pins: list[Pin] = []
    base = str(tree[1])
    for sub in children(tree, "symbol"):
        subname = str(sub[1])
        suffix = subname[len(base) + 1 :] if subname.startswith(base + "_") else ""
        parts = suffix.split("_")
        unit = int(parts[0]) if parts and parts[0].isdigit() else 0
        for p in children(sub, "pin"):
            at = child(p, "at")
            name_node = child(p, "name")
            number_node = child(p, "number")
            hidden = child(p, "hide") is not None and (atoms(child(p, "hide")) or ["yes"])[0] != "no"
            pins.append(
                Pin(
                    number=str(number_node[1]) if number_node else "",
                    name=str(name_node[1]) if name_node else "",
                    etype=str(p[1]),
                    shape=str(p[2]) if len(p) > 2 and not isinstance(p[2], list) else "line",
                    x=float(at[1]) if at else 0.0,
                    y=float(at[2]) if at else 0.0,
                    rotation=float(at[3]) if at and len(at) > 3 else 0.0,
                    length=float(value(p, "length", default="0") or 0),
                    hidden=hidden,
                    unit=unit,
                )
            )
    return pins


@functools.lru_cache(maxsize=256)
def load_symbol(lib: str, name: str) -> Symbol:
    tree = _flatten(lib, name)
    props = {str(p[1]): str(p[2]) for p in children(tree, "property")}
    power = child(tree, "power") is not None
    return Symbol(lib=lib, name=name, tree=tree, pins=_parse_pins(tree), properties=props, power=power)


# --------------------------------------------------------------------------------------
# placed-symbol geometry (eeschema TRANSFORM: rotate, then mirror on the right, then Y flip)
# --------------------------------------------------------------------------------------

_ANGLE = {0: (1, 0, 0, 1), 90: (0, 1, -1, 0), 180: (-1, 0, 0, -1), 270: (0, -1, 1, 0)}


def transform_point(px: float, py: float, cx: float, cy: float, angle: int = 0, mirror: str | None = None) -> tuple[float, float]:
    """Map a symbol-space point (Y up) to sheet coordinates (Y down) for a placed symbol."""
    x1, y1, x2, y2 = _ANGLE[int(angle) % 360]
    if mirror == "x":
        x1, y1, x2, y2 = x1, y1, -x2, -y2
    if mirror == "y":
        x1, y1, x2, y2 = -x1, -y1, x2, y2
    rpx = x1 * px - y1 * py
    rpy = -x2 * px + y2 * py
    return (round(cx + rpx, 4), round(cy - rpy, 4))


# --------------------------------------------------------------------------------------
# footprints
# --------------------------------------------------------------------------------------


@dataclass
class Pad:
    number: str
    kind: str  # smd, thru_hole, np_thru_hole
    shape: str
    x: float
    y: float
    rotation: float
    size: tuple[float, float]
    drill: float | None
    layers: list[str]


@dataclass
class Footprint:
    lib: str
    name: str
    tree: Node
    pads: list[Pad]

    @property
    def lib_id(self) -> str:
        return f"{self.lib}:{self.name}"

    def pad(self, number: str) -> Pad:
        for p in self.pads:
            if p.number == number:
                return p
        raise KeyError(f"{self.lib_id} has no pad {number!r}")

    def pad_position(self, number: str, fx: float, fy: float, rotation: float, layer: str = "F.Cu") -> tuple[float, float]:
        """Absolute board position of a pad for a footprint placed at (fx, fy, rotation). On B.Cu the
        footprint is first mirrored about its own X axis (local y negated), which is how KiCad flips it."""
        p = self.pad(number)
        return rotate_about(p.x, -p.y if layer == "B.Cu" else p.y, rotation, fx, fy)


def rotate_about(lx: float, ly: float, rotation_deg: float, ox: float, oy: float) -> tuple[float, float]:
    """Footprint-local (lx, ly) to board coordinates. KiCad rotates counter-clockwise on screen
    with Y pointing down, which is a clockwise rotation of the local axes."""
    a = math.radians(rotation_deg)
    c, s = math.cos(a), math.sin(a)
    x = ox + lx * c + ly * s
    y = oy - lx * s + ly * c
    return (round(x, 4), round(y, 4))


@functools.lru_cache(maxsize=256)
def load_footprint(lib: str, name: str) -> Footprint:
    tree = parse(footprint_path(lib, name).read_text(encoding="utf-8"))
    pads: list[Pad] = []
    for p in children(tree, "pad"):
        at = child(p, "at") or []
        size = child(p, "size") or []
        drill = child(p, "drill")
        layers_node = child(p, "layers") or []
        pads.append(
            Pad(
                number=str(p[1]),
                kind=str(p[2]),
                shape=str(p[3]),
                x=float(at[1]) if len(at) > 1 else 0.0,
                y=float(at[2]) if len(at) > 2 else 0.0,
                rotation=float(at[3]) if len(at) > 3 else 0.0,
                size=(float(size[1]), float(size[2])) if len(size) > 2 else (0.0, 0.0),
                drill=float(atoms(drill)[0]) if drill and atoms(drill) and atoms(drill)[0] not in ("oval",) else None,
                layers=[str(x) for x in layers_node[1:]],
            )
        )
    return Footprint(lib=lib, name=name, tree=tree, pads=pads)
