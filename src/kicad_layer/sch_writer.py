"""Write a KiCad 10 schematic from scratch.

This is the "new nodes" half of the schematic layer: it produces files in exactly the
shape eeschema 10.0 writes (format 20260306), with a flattened ``lib_symbols`` cache, pin
UUIDs, ``instances`` blocks carrying the reference designators, and items sorted the way
KiCad sorts them. Connectivity is geometric, so every wire end, pin tip, junction and
label anchor must coincide exactly; callers place things on the 1.27 mm grid.
"""

from __future__ import annotations

import uuid

from .formats import KICAD_RELEASE, SCHEMATIC_FORMAT
from .ids import IdFactory
from dataclasses import dataclass, field

from kicad_layer.kicad_libs import Symbol, load_symbol, transform_point
from kicad_layer.sexpr import S, Node, Sym, child, dumps, value

Point = tuple[float, float]


def new_uuid() -> str:
    return str(uuid.uuid4())


def _effects(size: float = 1.27, justify: tuple[str, ...] | None = None, hide: bool = False) -> Node:
    n = S("effects", S("font", S("size", size, size)))
    if justify:
        n.append(S("justify", *[Sym(j) for j in justify]))
    if hide:
        n.append(S("hide", Sym("yes")))
    return n


def _property(name: str, text: str, at: Point, rot: int = 0, hide: bool = False, size: float = 1.27, justify=None) -> Node:
    n = S("property", name, text, S("at", at[0], at[1], rot))
    if hide:
        n.append(S("hide", Sym("yes")))
    n.append(S("show_name", Sym("no")))
    n.append(S("do_not_autoplace", Sym("no")))
    n.append(_effects(size, justify))
    return n


@dataclass
class Placed:
    symbol: Symbol
    ref: str
    at: Point
    rot: int
    mirror: str | None
    uuid: str
    unit: int = 1
    pin_uuids: dict[str, str] = field(default_factory=dict)

    def pin(self, number: str) -> Point:
        """Sheet coordinates of a pin's connection point."""
        p = self.symbol.pin(number)
        return transform_point(p.x, p.y, self.at[0], self.at[1], self.rot, self.mirror)

    def pin_by_name(self, name: str) -> Point:
        pins = [p for p in self.symbol.pins if p.name == name]
        if not pins:
            raise KeyError(f"{self.symbol.lib_id} has no pin named {name!r}")
        p = pins[0]
        return transform_point(p.x, p.y, self.at[0], self.at[1], self.rot, self.mirror)


SHEET_PIN_SHAPES = ("input", "output", "bidirectional", "tri_state", "passive")


@dataclass
class PlacedSheet:
    """A sheet symbol placed in a parent schematic; ``pin(name)`` is the wire attachment point."""

    name: str
    file: str
    uuid: str
    at: Point
    size: tuple[float, float]
    pins: dict[str, Point] = field(default_factory=dict)
    page: int = 2

    def pin(self, name: str) -> Point:
        return self.pins[name]


class SchematicBuilder:
    ORDER = ["junction", "no_connect", "wire", "text", "label", "global_label", "hierarchical_label", "symbol", "sheet"]

    def __init__(
        self,
        project_name: str,
        *,
        paper: str = "A4",
        title: str = "",
        date: str = "",
        rev: str = "",
        company: str = "",
        comments: list[str] | None = None,
        instance_path: str | None = None,
        ids: IdFactory | None = None,
    ) -> None:
        """A root schematic, or with ``instance_path`` (``/root-uuid/sheet-uuid``) a sub-sheet."""
        self.project_name = project_name
        self.ids = ids or IdFactory()
        self.uuid = self.ids.make("schematic", instance_path or "root")
        self.paper = paper
        self.title_block = dict(title=title, date=date, rev=rev, company=company)
        self.comments = comments or []
        self.instance_path = instance_path
        self.cache: dict[str, Symbol] = {}
        self.items: list[Node] = []
        self.placed: list[Placed] = []
        self.sheets: list[PlacedSheet] = []
        self._pwr_counter = 0
        self._flag_counter = 0
        self._next_page = 2

    @property
    def path(self) -> str:
        """The sheet path KiCad writes into symbol instances."""
        return self.instance_path or f"/{self.uuid}"

    @property
    def is_root(self) -> bool:
        return self.instance_path is None

    # -- symbols --------------------------------------------------------------------

    def place(
        self,
        lib: str,
        name: str,
        ref: str,
        at: Point,
        *,
        rot: int = 0,
        mirror: str | None = None,
        unit: int = 1,
        value_text: str | None = None,
        footprint: str = "",
        ref_pos: Point | None = None,
        value_pos: Point | None = None,
        hide_ref: bool = False,
        hide_value: bool = False,
        text_size: float = 1.27,
        extra_props: dict[str, str] | None = None,
        in_bom: bool | None = None,
        on_board: bool | None = None,
        dnp: bool = False,
    ) -> Placed:
        """``in_bom`` and ``on_board`` override the library symbol's flags (a mounting-hole symbol
        excluded from the BOM can stand for a solder nut that must be bought, for instance).
        ``dnp`` marks the symbol do-not-populate, as KiCad's own flag: it stays in the BOM marked DNP,
        and KiCad's BOM and position exports leave it out when asked to (``--exclude-dnp``)."""
        sym = load_symbol(lib, name)
        self.cache.setdefault(sym.lib_id, sym)
        placed = Placed(symbol=sym, ref=ref, at=at, rot=rot, mirror=mirror, uuid=self.ids.make("symbol", ref, unit), unit=unit)
        x, y = at
        val = value_text if value_text is not None else sym.properties.get("Value", name)
        fp = footprint or sym.properties.get("Footprint", "")
        datasheet = sym.properties.get("Datasheet", "")
        description = sym.properties.get("Description", "")

        def flag(key: str, override: bool | None, default: str) -> Sym:
            if override is not None:
                return Sym("yes" if override else "no")
            return Sym(value(sym.tree, key, default=default) or default)

        node = S("symbol", S("lib_id", sym.lib_id), S("at", x, y, rot))
        if mirror:
            node.append(S("mirror", Sym(mirror)))
        node += [
            S("unit", unit),
            S("body_style", 1),
            S("exclude_from_sim", Sym(value(sym.tree, "exclude_from_sim", default="no") or "no")),
            S("in_bom", flag("in_bom", in_bom, "yes")),
            S("on_board", flag("on_board", on_board, "yes")),
            S("in_pos_files", Sym(value(sym.tree, "in_pos_files", default="yes") or "yes")),
            S("dnp", Sym("yes" if dnp else "no")),
            S("uuid", placed.uuid),
            # KiCad stores a field's angle relative to the symbol's, so a rotated symbol needs 90 here
            # for the text to read horizontally; KiCad's own files keep field angles to 0 or 90.
            _property("Reference", ref, ref_pos or (x, y - 2.54), rot=rot % 180, hide=hide_ref, size=text_size),
            _property("Value", val, value_pos or (x, y + 2.54), rot=rot % 180, hide=hide_value, size=text_size),
            _property("Footprint", fp, (x, y), hide=True),
            _property("Datasheet", datasheet, (x, y), hide=True),
            _property("Description", description, (x, y), hide=True),
        ]
        for k, v in (extra_props or {}).items():
            node.append(_property(k, v, (x, y), hide=True))
        for p in sym.pins:
            if p.unit not in (0, unit):
                continue
            pu = self.ids.make("pin", ref, unit, p.number)
            placed.pin_uuids[p.number] = pu
            node.append(S("pin", p.number, S("uuid", pu)))
        node.append(
            S("instances", S("project", self.project_name, S("path", self.path, S("reference", ref), S("unit", unit))))
        )
        self.items.append(node)
        self.placed.append(placed)
        return placed

    def mark_dnp(self, refs: set[str] | list[str] | tuple[str, ...]) -> int:
        """Mark every placed unit of the references in ``refs`` do-not-populate; returns how many symbols changed."""
        wanted, n = set(refs), 0
        for node in self.items:
            if str(node[0]) != "symbol":
                continue
            ref = next((str(c[2]) for c in node if isinstance(c, list) and c and str(c[0]) == "property" and str(c[1]) == "Reference"), None)
            if ref in wanted:
                flag = child(node, "dnp")
                if flag is not None and str(flag[1]) != "yes":
                    flag[1] = Sym("yes")
                    n += 1
        return n

    def power(self, name: str, at: Point, *, rot: int = 0) -> Placed:
        """A power symbol (+5V, GND, ...). Its pin is at ``at``."""
        self._pwr_counter += 1
        ref = f"#PWR{self._pwr_counter:03d}"
        x, y = at
        below = name.upper().startswith("GND")
        value_pos = (x, y + 2.54) if below else (x, y - 3.556)
        return self.place("power", name, ref, at, rot=rot, hide_ref=True, value_pos=value_pos, ref_pos=(x, y + 1.27 if not below else y - 1.27))

    def pwr_flag(self, at: Point) -> Placed:
        self._flag_counter += 1
        ref = f"#FLG{self._flag_counter:02d}"
        x, y = at
        return self.place("power", "PWR_FLAG", ref, at, hide_ref=True, value_pos=(x, y - 3.81), ref_pos=(x, y + 1.27))

    # -- connectivity ----------------------------------------------------------------

    def wire(self, a: Point, b: Point) -> None:
        if a == b:
            return
        self.items.append(
            S("wire", S("pts", S("xy", a[0], a[1]), S("xy", b[0], b[1])), S("stroke", S("width", 0), S("type", Sym("default"))), S("uuid", self.ids.make("wire", a[0], a[1], b[0], b[1])))
        )

    def polyline(self, *pts: Point) -> None:
        for a, b in zip(pts, pts[1:]):
            self.wire(a, b)

    def junction(self, p: Point) -> None:
        self.items.append(S("junction", S("at", p[0], p[1]), S("diameter", 0), S("color", 0, 0, 0, 0), S("uuid", self.ids.make("junction", p[0], p[1]))))

    def no_connect(self, p: Point) -> None:
        self.items.append(S("no_connect", S("at", p[0], p[1]), S("uuid", self.ids.make("no_connect", p[0], p[1]))))

    def label(self, text: str, p: Point, rot: int = 0, size: float = 1.27) -> None:
        justify = {0: ("left", "bottom"), 90: ("left", "bottom"), 180: ("right", "bottom"), 270: ("right", "bottom")}[rot % 360]
        self.items.append(S("label", text, S("at", p[0], p[1], rot), _effects(size, justify), S("uuid", self.ids.make("label", text, p[0], p[1]))))

    def text(self, text: str, p: Point, size: float = 1.27, rot: int = 0) -> None:
        self.items.append(
            S("text", text, S("exclude_from_sim", Sym("no")), S("at", p[0], p[1], rot), _effects(size, ("left", "bottom")), S("uuid", self.ids.make("text", text, p[0], p[1])))
        )

    # -- hierarchy ------------------------------------------------------------------

    def hier_label(self, text: str, p: Point, *, shape: str = "input", rot: int = 0, size: float = 1.27) -> None:
        """A hierarchical label whose connection point is ``p``.

        Rotation 0 puts the label to the right of the point (the wire arrives from the left);
        180 puts it to the left; 90 above; 270 below. ``shape`` is what the matching sheet pin
        will show: input, output, bidirectional, tri_state or passive.
        """
        if shape not in SHEET_PIN_SHAPES:
            raise ValueError(f"unknown label shape {shape!r}; use one of {SHEET_PIN_SHAPES}")
        justify = {0: ("left",), 90: ("left",), 180: ("right",), 270: ("right",)}[rot % 360]
        self.items.append(S("hierarchical_label", text, S("shape", Sym(shape)), S("at", p[0], p[1], rot), _effects(size, justify), S("uuid", self.ids.make("hier_label", text, p[0], p[1]))))

    def sheet(
        self,
        name: str,
        file: str,
        at: Point,
        size: tuple[float, float],
        *,
        pins_left: list[tuple[str, str]] | None = None,
        pins_right: list[tuple[str, str]] | None = None,
        pitch: float = 2.54,
    ) -> PlacedSheet:
        """A sheet symbol with pins spaced ``pitch`` apart down each side, starting one pitch
        below the top edge. Pins are ``(name, shape)``; the sheet's own file gets matching
        hierarchical labels from the child builder (see ``child``)."""
        x0, y0 = at
        w, h = size
        needed = (max(len(pins_left or []), len(pins_right or [])) + 1) * pitch
        if h < needed:
            raise ValueError(f"sheet {name!r} is {h} mm tall but its pins need {needed} mm")
        sheet_uuid = self.ids.make("sheet", file)
        placed = PlacedSheet(name=name, file=file, uuid=sheet_uuid, at=at, size=size, page=self._next_page)
        self._next_page += 1
        node = S("sheet", S("at", x0, y0), S("size", w, h), S("exclude_from_sim", Sym("no")), S("in_bom", Sym("yes")), S("on_board", Sym("yes")), S("dnp", Sym("no")),
                 S("fields_autoplaced", Sym("yes")), S("stroke", S("width", 0.1524), S("type", Sym("solid"))), S("fill", S("color", 0, 0, 0, 0.0)), S("uuid", sheet_uuid))
        sn = S("property", "Sheetname", name, S("at", x0, round(y0 - 0.7116, 4), 0), S("show_name", Sym("no")), S("do_not_autoplace", Sym("no")),
               S("effects", S("font", S("size", 1.27, 1.27), S("thickness", 0.254), S("bold", Sym("yes"))), S("justify", Sym("left"), Sym("bottom"))))
        sf = S("property", "Sheetfile", file, S("at", x0, round(y0 + h + 0.7596, 4), 0), S("hide", Sym("yes")), S("show_name", Sym("no")), S("do_not_autoplace", Sym("no")),
               S("effects", S("font", S("size", 1.27, 1.27)), S("justify", Sym("left"), Sym("top"))))
        node += [sn, sf]
        # an empty pin name leaves that row blank, which lets facing sheets keep their rows aligned
        for i, (pname, shape) in enumerate(pins_left or []):
            if not pname:
                continue
            if shape not in SHEET_PIN_SHAPES:
                raise ValueError(f"sheet pin {pname!r}: unknown shape {shape!r}")
            p = (x0, round(y0 + pitch * (i + 1), 4))
            placed.pins[pname] = p
            node.append(S("pin", pname, Sym(shape), S("at", p[0], p[1], 180), S("uuid", self.ids.make("sheet_pin", file, pname)), _effects(1.27, ("left",))))
        for i, (pname, shape) in enumerate(pins_right or []):
            if not pname:
                continue
            if shape not in SHEET_PIN_SHAPES:
                raise ValueError(f"sheet pin {pname!r}: unknown shape {shape!r}")
            p = (round(x0 + w, 4), round(y0 + pitch * (i + 1), 4))
            placed.pins[pname] = p
            node.append(S("pin", pname, Sym(shape), S("at", p[0], p[1], 0), S("uuid", self.ids.make("sheet_pin", file, pname)), _effects(1.27, ("right",))))
        node.append(S("instances", S("project", self.project_name, S("path", self.path, S("page", str(placed.page))))))
        self.items.append(node)
        self.sheets.append(placed)
        return placed

    def child(self, sheet: PlacedSheet, **kwargs) -> "SchematicBuilder":
        """The builder for a placed sheet's own file; its symbols get the right instance path."""
        kwargs.setdefault("paper", self.paper)
        kwargs.setdefault("ids", self.ids.child("sheet", sheet.file))
        cb = SchematicBuilder(self.project_name, instance_path=f"{self.path}/{sheet.uuid}", **kwargs)
        # power symbols and flags get a per-sheet reference range, like KiCad's own annotation
        cb._pwr_counter = 100 * sheet.page
        cb._flag_counter = 10 * sheet.page
        cb._next_page = self._next_page + 100 * sheet.page
        return cb

    def sheet_list(self, children: dict[str, "SchematicBuilder"] | None = None) -> list[list[str]]:
        """The ``sheets`` array for the project file: root first, then every placed sheet."""
        out = [[self.uuid, "Root"]] if self.is_root else []
        for s in self.sheets:
            out.append([s.uuid, s.name])
            cb = (children or {}).get(s.name)
            if cb is not None:
                out += cb.sheet_list(children)
        return out

    # -- output ---------------------------------------------------------------------

    def build(self) -> Node:
        tb = S("title_block")
        for k in ("title", "date", "rev", "company"):
            if self.title_block.get(k):
                tb.append(S(k, self.title_block[k]))
        for i, c in enumerate(self.comments, start=1):
            tb.append(S("comment", i, c))
        lib_symbols = S("lib_symbols")
        for lib_id in sorted(self.cache):
            lib_symbols.append(self.cache[lib_id].cache_entry())
        order = {t: i for i, t in enumerate(self.ORDER)}

        def key(n: Node) -> tuple[int, str]:
            t = str(n[0])
            u = value(n, "uuid", default="") or ""
            return (order.get(t, 99), u)

        root = S("kicad_sch", S("version", SCHEMATIC_FORMAT), S("generator", "kicad_layer"), S("generator_version", KICAD_RELEASE), S("uuid", self.uuid), S("paper", self.paper))
        if len(tb) > 1:
            root.append(tb)
        root.append(lib_symbols)
        root.extend(sorted(self.items, key=key))
        if self.is_root:
            root.append(S("sheet_instances", S("path", "/", S("page", "1"))))
        root.append(S("embedded_fonts", Sym("no")))
        return root

    def write(self, path: str) -> None:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(dumps(self.build()) + "\n")
