"""Write a KiCad 10 board from scratch (format 20260206).

Footprints are embedded from KiCad's libraries with their pads bound to net names; child
coordinates stay footprint-local, as KiCad writes them, and pad angles carry the footprint
rotation. Nets are referenced by name only; KiCad 10 boards carry no net table.

Back-side placement follows what KiCad's own ``FOOTPRINT::Flip`` writes, derived from KiCad-authored
boards (the pic_programmer and multichannel demos in tests/fixtures, the CM5 MINIMA board and the
Jetson AGX Thor demo shipped with KiCad 10): the footprint keeps ``(at x y rot)`` and gets
``(layer "B.Cu")``; every child is mirrored about the footprint's X axis, so local y is negated and
x kept; F.* layers become B.* and vice versa; a pad's relative angle is negated (``rot - angle``);
a text's angle becomes ``rot + 180 - angle`` and its mirror justify flag is toggled; arcs swap
start and end so the (start, mid, end) triple keeps its sweep. The absolute position of a pad is
then ``rotate_about(local_x, local_y, rot, x, y)`` on either side, which is how the tracks in the
routed multichannel demo meet the pads of its back-side SOICs.
"""

from __future__ import annotations

import copy
import math
import uuid

from .formats import BOARD_FORMAT, KICAD_RELEASE
from .ids import IdFactory
from pathlib import Path

from kicad_layer.kicad_libs import Footprint
from kicad_layer.sexpr import S, Node, Sym, child, children, dumps, num, parse, remove_children, tag

Point = tuple[float, float]


def new_uuid() -> str:
    return str(uuid.uuid4())


LAYERS = [
    (0, "F.Cu", "signal"), (2, "B.Cu", "signal"),  # inner copper layers are inserted between these two
    (9, "F.Adhes", "user", "F.Adhesive"), (11, "B.Adhes", "user", "B.Adhesive"),
    (13, "F.Paste", "user"), (15, "B.Paste", "user"),
    (5, "F.SilkS", "user", "F.Silkscreen"), (7, "B.SilkS", "user", "B.Silkscreen"),
    (1, "F.Mask", "user"), (3, "B.Mask", "user"),
    (17, "Dwgs.User", "user", "User.Drawings"), (19, "Cmts.User", "user", "User.Comments"),
    (21, "Eco1.User", "user", "User.Eco1"), (23, "Eco2.User", "user", "User.Eco2"),
    (25, "Edge.Cuts", "user"), (27, "Margin", "user"),
    (31, "F.CrtYd", "user", "F.Courtyard"), (29, "B.CrtYd", "user", "B.Courtyard"),
    (35, "F.Fab", "user"), (33, "B.Fab", "user"),
]


def copper_layer_names(copper_layers: int) -> list[str]:
    """F.Cu, In1.Cu ... B.Cu for a board with ``copper_layers`` layers."""
    if copper_layers < 2 or copper_layers % 2:
        raise ValueError(f"copper_layers must be an even number of at least 2, not {copper_layers}")
    return ["F.Cu"] + [f"In{i}.Cu" for i in range(1, copper_layers - 1)] + ["B.Cu"]


def _layers_node(copper_layers: int = 2) -> Node:
    """KiCad's layer table; inner copper layers carry ids 4, 6, 8 ... as KiCad 9 and 10 number them."""
    n = S("layers")
    for entry in LAYERS:
        item = S(Sym(str(entry[0])), entry[1], Sym(entry[2]))
        if len(entry) > 3:
            item.append(entry[3])
        n.append(item)
        if entry[1] == "F.Cu":
            for i in range(1, copper_layers - 1):
                n.append(S(Sym(str(2 + 2 * i)), f"In{i}.Cu", Sym("signal")))
    return n


# --------------------------------------------------------------------------------------
# flipping a footprint to the back, the way FOOTPRINT::Flip does it
# --------------------------------------------------------------------------------------


def flip_layer(name: str) -> str:
    """Where a footprint item lands when its footprint changes sides: F.* <-> B.*; *.Cu, F&B.Cu and
    the user layers stay where they are."""
    if name.startswith("F."):
        return "B." + name[2:]
    if name.startswith("B."):
        return "F." + name[2:]
    return name


def _flip_layer_atoms(node: Node | None) -> None:
    """Flip every layer name in a ``(layer ...)`` or ``(layers ...)`` node, keeping its quoting."""
    if node is None:
        return
    for i in range(1, len(node)):
        a = node[i]
        if not isinstance(a, list):
            node[i] = Sym(flip_layer(str(a))) if isinstance(a, Sym) else flip_layer(str(a))


def _negate_y(node: Node | None) -> None:
    """Negate the second coordinate of ``(start x y)``, ``(xy x y)``, ``(at x y ...)``, ``(offset x y)`` and friends."""
    if node is not None and len(node) > 2 and not isinstance(node[2], list):
        node[2] = num(-float(node[2]))


def mirror_geometry(item: Node) -> None:
    """Mirror a graphic item (or a custom pad's primitive) about the footprint's X axis in place: every
    y is negated, x is kept, and an arc's start and end swap so its (start, mid, end) keeps the same
    sweep. This is what the back-side parts in KiCad-authored boards show against their library files."""
    for k in ("start", "end", "mid", "center"):
        _negate_y(child(item, k))
    pts = child(item, "pts")
    if pts is not None:
        for xy in children(pts, "xy"):
            _negate_y(xy)
        for arc in children(pts, "arc"):  # curved polygon corners: (arc (start) (mid) (end)) inside pts
            for k in ("start", "mid", "end"):
                _negate_y(child(arc, k))
            s_, e_ = child(arc, "start"), child(arc, "end")
            if s_ is not None and e_ is not None:
                s_[1:], e_[1:] = e_[1:], s_[1:]
    if tag(item) in ("fp_arc", "gr_arc"):
        s, e = child(item, "start"), child(item, "end")
        if s is not None and e is not None:
            s[1:], e[1:] = e[1:], s[1:]


_CHAMFER_FLIP = {"top_left": "bottom_left", "bottom_left": "top_left", "top_right": "bottom_right", "bottom_right": "top_right"}


def mirror_pad(pad: Node) -> None:
    """Mirror everything of a pad except its ``at`` (position and angle are the caller's): layers, the
    drill offset, the trapezoid delta, chamfered corners and custom-shape primitives."""
    _flip_layer_atoms(child(pad, "layers"))
    drill = child(pad, "drill")
    if drill is not None:
        _negate_y(child(drill, "offset"))
    _negate_y(child(pad, "rect_delta"))
    chamfer = child(pad, "chamfer")
    if chamfer is not None:
        for i in range(1, len(chamfer)):
            chamfer[i] = Sym(_CHAMFER_FLIP.get(str(chamfer[i]), str(chamfer[i])))
    primitives = child(pad, "primitives")
    if primitives is not None:
        for g in primitives:
            if isinstance(g, list):
                mirror_geometry(g)


def toggle_mirror(text_item: Node) -> None:
    """Toggle the ``mirror`` justify flag of a text's effects, as PCB_TEXT::Flip toggles IsMirrored():
    a library text that was already mirrored comes out plain (the CM5 MINIMA board has such a field)."""
    eff = child(text_item, "effects")
    if eff is None:
        eff = S("effects")
        text_item.append(eff)
    j = child(eff, "justify")
    if j is None:
        eff.append(S("justify", Sym("mirror")))
    elif "mirror" in j[1:]:
        j[:] = [a for a in j if a != "mirror"]
        if len(j) == 1:
            eff.remove(j)
    else:
        j.append(Sym("mirror"))


class BoardBuilder:
    def __init__(self, *, sheetfile: str, title: str = "", date: str = "", rev: str = "", company: str = "", setup_template: Path | None = None, copper_layers: int = 2,
                 ids: IdFactory | None = None) -> None:
        self.sheetfile = sheetfile
        self.ids = ids or IdFactory()
        self.title_block = dict(title=title, date=date, rev=rev, company=company)
        self.copper_layers = copper_layers
        copper_layer_names(copper_layers)  # validates
        self.footprints: list[Node] = []
        self.items: list[Node] = []
        self.setup = self._load_setup(setup_template, copper_layers)

    @staticmethod
    def _load_setup(template: Path | None, copper_layers: int = 2) -> Node:
        if template and template.is_file():
            root = parse(template.read_text(encoding="utf-8"))
            s = child(root, "setup")
            if s is not None:
                s = copy.deepcopy(s)
                # keep everything except the plot output dir, which is project specific
                pp = child(s, "pcbplotparams")
                if pp is not None:
                    od = child(pp, "outputdirectory")
                    if od is not None:
                        od[1] = ""
                # a stackup describes a specific layer count; drop it when ours differs and let KiCad default it
                st = child(s, "stackup")
                if st is not None:
                    copper_in_stackup = sum(1 for l in children(st, "layer") if str(l[1]).endswith(".Cu"))
                    if copper_in_stackup != copper_layers:
                        remove_children(s, "stackup")
                return s
        return S("setup", S("pad_to_mask_clearance", 0), S("allow_soldermask_bridges_in_footprints", Sym("no")))

    # -- footprints -----------------------------------------------------------------

    def footprint(
        self,
        fp: Footprint,
        ref: str,
        value_text: str,
        at: Point,
        rot: float,
        *,
        path_uuid: str = "",
        pad_nets: dict[str, str],
        hide_ref: bool = False,
        hide_value: bool = True,
        description: str = "",
        datasheet: str = "",
        path: str = "",
        sheetname: str = "/",
        sheetfile: str = "",
        layer: str = "F.Cu",
        fields: dict[str, str] | None = None,
    ) -> Node:
        """Embed a library footprint. ``path`` is the full symbol path (``/root-uuid/sheet-uuid/symbol-uuid``);
        ``path_uuid`` is the shorthand for a symbol on the root sheet. ``sheetname`` and ``sheetfile``
        name the sheet the symbol lives on, as KiCad records them. ``rot`` is the orientation KiCad shows
        for the footprint on either side; ``layer`` "B.Cu" mirrors the part the way KiCad's flip does."""
        if layer not in ("F.Cu", "B.Cu"):
            raise ValueError(f"a footprint sits on F.Cu or B.Cu, not {layer!r}")
        back = layer == "B.Cu"
        # A flipped footprint is mirrored about its own X axis: y negated, every angle reversed, texts
        # turned half a circle so they read from the back (the module docstring names the evidence).
        sign = -1.0 if back else 1.0
        text_turn = 180.0 if back else 0.0

        def item_angle(base: float, *, text: bool = False) -> float:
            return (rot + (text_turn if text else 0.0) + sign * base) % 360

        def place_text(item: Node) -> None:
            """Give a property, fp_text or fp_text_box its board angle (KiCad stores text angles
            absolute, so they turn with the footprint) and mirror it when the part is on the back."""
            at_node = child(item, "at")
            if at_node is not None:
                # (at x y [angle] [unlocked]): older libraries append the 'unlocked' flag after the angle
                tail = [a for a in at_node[3:] if not isinstance(a, list)]
                base = 0.0
                flags = []
                for a in tail:
                    try:
                        base = float(str(a))
                    except ValueError:
                        flags.append(a)
                if back:
                    _negate_y(at_node)
                del at_node[3:]
                at_node.append(num(item_angle(base, text=True)))
                at_node.extend(flags)
            angle_node = child(item, "angle")  # text boxes carry their angle separately
            if angle_node is not None and len(angle_node) > 1 and not isinstance(angle_node[1], list):
                angle_node[1] = num(item_angle(float(angle_node[1]), text=True))
            if back:
                mirror_geometry(item)
                _flip_layer_atoms(child(item, "layer"))
                toggle_mirror(item)

        src = fp.tree
        node = S("footprint", fp.lib_id, S("layer", layer), S("uuid", self.ids.make("footprint", ref)), S("at", at[0], at[1], rot if rot else None))
        for t in ("descr", "tags"):
            c = child(src, t)
            if c is not None:
                node.append(copy.deepcopy(c))

        def prop(name: str, text: str, template: Node | None, layer_name: str, hide: bool) -> Node:
            if template is not None:
                p = copy.deepcopy(template)
                p[2] = text
                place_text(p)
                remove_children(p, "uuid")
                remove_children(p, "hide")
                idx = next((i for i, c in enumerate(p) if isinstance(c, list) and tag(c) == "layer"), len(p))
                if hide:
                    p.insert(idx + 1, S("hide", Sym("yes")))
                p.insert(idx + 1 + (1 if hide else 0), S("uuid", self.ids.make("property", ref, name)))
                return p
            p = S("property", name, text, S("at", 0, 0, item_angle(0.0, text=True)), S("layer", flip_layer(layer_name) if back else layer_name))
            if hide:
                p.append(S("hide", Sym("yes")))
            p.append(S("uuid", self.ids.make("property", ref, name)))
            p.append(S("effects", S("font", S("size", 1.27, 1.27), S("thickness", 0.15))))
            if back:
                toggle_mirror(p)
            return p

        lib_props = {str(p[1]): p for p in children(src, "property")}
        node.append(prop("Reference", ref, lib_props.get("Reference"), "F.SilkS", hide_ref))
        node.append(prop("Value", value_text, lib_props.get("Value"), "F.Fab", hide_value))
        node.append(prop("Footprint", fp.lib_id, None, "F.Fab", True))
        node.append(prop("Datasheet", datasheet, None, "F.Fab", True))
        node.append(prop("Description", description, None, "F.Fab", True))
        # every other symbol field (MPN, LCSC, ...) rides along hidden, so DRC's schematic parity stays clean
        for name, text in (fields or {}).items():
            if name not in ("Reference", "Value", "Footprint", "Datasheet", "Description") and name:
                node.append(prop(name, text, None, "F.Fab", True))
        node.append(S("path", path or f"/{path_uuid}"))
        node.append(S("sheetname", sheetname))
        node.append(S("sheetfile", sheetfile or self.sheetfile))
        # copy the attributes as they are; inventing one makes KiCad report a library mismatch
        attr = child(src, "attr")
        if attr is not None:
            node.append(copy.deepcopy(attr))
        node.append(S("duplicate_pad_numbers_are_jumpers", Sym("no")))

        for c in src:
            if not isinstance(c, list):
                continue
            t = tag(c)
            if t in ("fp_line", "fp_poly", "fp_rect", "fp_circle", "fp_arc", "fp_text", "fp_text_box"):
                g = copy.deepcopy(c)
                if t in ("fp_text", "fp_text_box"):
                    place_text(g)
                elif back:
                    mirror_geometry(g)
                    _flip_layer_atoms(child(g, "layer"))
                if child(g, "uuid") is None:
                    g.append(S("uuid", self.ids.make("graphic", ref)))
                node.append(g)
        for c in children(src, "pad"):
            p = copy.deepcopy(c)
            at_node = child(p, "at")
            if at_node is None:
                at_node = S("at", 0, 0)
                p.insert(4, at_node)
            base = float(at_node[3]) if len(at_node) > 3 else 0.0
            angle = item_angle(base)
            if back:
                _negate_y(at_node)
                mirror_pad(p)
            del at_node[3:]
            if angle:
                at_node.append(num(angle))
            remove_children(p, "net")
            remove_children(p, "uuid")
            number = str(p[1])
            net = pad_nets.get(number)
            if net:
                p.append(S("net", net))
            p.append(S("uuid", self.ids.make("pad", ref, number)))
            node.append(p)
        node.append(S("embedded_fonts", Sym("no")))
        for c in children(src, "model"):
            node.append(copy.deepcopy(c))
        self.footprints.append(node)
        return node

    # -- copper ---------------------------------------------------------------------

    def segment(self, a: Point, b: Point, *, width: float, layer: str, net: str) -> None:
        if a == b:
            return
        self.items.append(S("segment", S("start", a[0], a[1]), S("end", b[0], b[1]), S("width", width), S("layer", layer), S("net", net), S("uuid", self.ids.make("segment", net, layer, a[0], a[1], b[0], b[1]))))

    def track(self, pts: list[Point], *, width: float, layer: str, net: str) -> None:
        for a, b in zip(pts, pts[1:]):
            self.segment(a, b, width=width, layer=layer, net=net)

    def via(self, p: Point, *, net: str, size: float = 0.8, drill: float = 0.3, layers: tuple[str, str] = ("F.Cu", "B.Cu")) -> None:
        self.items.append(S("via", S("at", p[0], p[1]), S("size", size), S("drill", drill), S("layers", layers[0], layers[1]), S("net", net), S("uuid", self.ids.make("via", net, p[0], p[1]))))

    def zone(self, *, net: str, layer: str, polygon: list[Point], name: str = "", priority: int = 0, clearance: float = 0.2, min_thickness: float = 0.25) -> None:
        z = S("zone", S("net", net), S("layer", layer), S("uuid", self.ids.make("zone", net, layer, name)))
        if name:
            z.append(S("name", name))
        z.append(S("hatch", Sym("edge"), 0.508))
        if priority:
            z.append(S("priority", priority))
        z.append(S("connect_pads", S("clearance", clearance)))
        z.append(S("min_thickness", min_thickness))
        z.append(S("filled_areas_thickness", Sym("no")))
        z.append(S("fill", Sym("yes"), S("thermal_gap", 0.3), S("thermal_bridge_width", 0.4), S("island_removal_mode", 0)))
        z.append(S("polygon", S("pts", *[S("xy", x, y) for x, y in polygon])))
        self.items.append(z)

    # -- graphics -------------------------------------------------------------------

    def line(self, a: Point, b: Point, *, layer: str, width: float = 0.1) -> None:
        if abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6:
            return  # a zero-length segment makes KiCad call the outline malformed (a rounded rect whose radius is half its height)
        self.items.append(S("gr_line", S("start", a[0], a[1]), S("end", b[0], b[1]), S("stroke", S("width", width), S("type", Sym("default"))), S("layer", layer), S("uuid", self.ids.make("gr_line", layer, a[0], a[1], b[0], b[1]))))

    def arc(self, start: Point, mid: Point, end: Point, *, layer: str, width: float = 0.1) -> None:
        self.items.append(S("gr_arc", S("start", *start), S("mid", *mid), S("end", *end), S("stroke", S("width", width), S("type", Sym("default"))), S("layer", layer), S("uuid", self.ids.make("gr_arc", layer, *start, *mid, *end))))

    def rounded_rect_outline(self, x0: float, y0: float, x1: float, y1: float, r: float, *, layer: str = "Edge.Cuts", width: float = 0.1) -> None:
        """Rounded rectangle from (x0, y0) top-left to (x1, y1) bottom-right, radius r."""
        k = r * (1 - math.sqrt(0.5))
        self.line((x0 + r, y0), (x1 - r, y0), layer=layer, width=width)
        self.line((x1, y0 + r), (x1, y1 - r), layer=layer, width=width)
        self.line((x1 - r, y1), (x0 + r, y1), layer=layer, width=width)
        self.line((x0, y1 - r), (x0, y0 + r), layer=layer, width=width)
        self.arc((x1 - r, y0), (x1 - k, y0 + k), (x1, y0 + r), layer=layer, width=width)
        self.arc((x1, y1 - r), (x1 - k, y1 - k), (x1 - r, y1), layer=layer, width=width)
        self.arc((x0 + r, y1), (x0 + k, y1 - k), (x0, y1 - r), layer=layer, width=width)
        self.arc((x0, y0 + r), (x0 + k, y0 + k), (x0 + r, y0), layer=layer, width=width)

    def text(self, text: str, at: Point, *, layer: str = "F.SilkS", size: float = 1.0, thickness: float = 0.15, rot: float = 0, justify: tuple[str, ...] | None = None, bold: bool = False) -> None:
        font = S("font", S("size", size, size), S("thickness", thickness))
        if bold:
            font.append(S("bold", Sym("yes")))
        eff = S("effects", font)
        if justify:
            eff.append(S("justify", *[Sym(j) for j in justify]))
        self.items.append(S("gr_text", text, S("at", at[0], at[1], rot if rot else None), S("layer", layer), S("uuid", self.ids.make("gr_text", layer, text, at[0], at[1])), eff))

    # -- output ---------------------------------------------------------------------

    def build(self) -> Node:
        root = S("kicad_pcb", S("version", BOARD_FORMAT), S("generator", "kicad_layer"), S("generator_version", KICAD_RELEASE))
        root.append(S("general", S("thickness", 1.6), S("legacy_teardrops", Sym("no"))))
        root.append(S("paper", "A4"))
        tb = S("title_block")
        for k in ("title", "date", "rev", "company"):
            if self.title_block.get(k):
                tb.append(S(k, self.title_block[k]))
        if len(tb) > 1:
            root.append(tb)
        root.append(_layers_node(self.copper_layers))
        root.append(self.setup)
        root.extend(self.footprints)
        order = {"gr_line": 0, "gr_arc": 0, "gr_text": 1, "segment": 2, "via": 3, "zone": 4}
        root.extend(sorted(self.items, key=lambda n: order.get(str(n[0]), 9)))
        root.append(S("embedded_fonts", Sym("no")))
        return root

    def write(self, path: str) -> None:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(dumps(self.build()) + "\n")
