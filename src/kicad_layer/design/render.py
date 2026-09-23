"""Draw a Circuit as a KiCad sheet, guided by placement hints.

The circuit says what connects to what; a :class:`Layout` says where the anchor parts (ICs,
connectors, LEDs that carry a label) sit, and a few per-pin hints shape the drawing. Everything
else follows rules:

* a pin on a sheet signal gets a hierarchical label at the end of a stub; a pin on a named
  private net gets a local label;
* a pin on a rail or ground joins the other such pins of its side on a short rail, ending in a
  power symbol; decoupling capacitors on that rail hang from it beside the part;
* a two-pin part between a pin and something else is drawn in line with the pin, and whatever
  its far end connects to (a signal, a rail, ground) is drawn there; a pull resistor or a
  capacitor on a labelled net hangs from a tap on the stub, on a private node from the node;
* two anchor pins on one private net are wired straight when they sit on the same row, and
  get matching local labels otherwise;
* pins declared open get a no-connect flag.

A multi-unit symbol (a dual opamp and its power unit) is drawn unit by unit: every unit that carries
pins is placed, and each pin is drawn against its own unit. ``Layout.parts`` names a unit as
``"U4/2"`` (or ``"U4/B"``); plain ``"U4"`` is unit 1, and the flow places the units the layout does not.
PWR_FLAGs are placed last, on a short lead that touches nothing of another net. ``Circuit.note()``
texts are stacked under the drawing (or from ``Layout.note_at``).

Passives therefore need no hints at all; they are placed by what they connect to. Hints that
shape a drawing: ``detour`` bends a stub sideways before its label, ``stub_len`` overrides a
stub, ``tap`` moves where things hang off a labelled stub, ``hang`` puts decoupling on a named
private net at one pin, ``power_rot`` turns a lone power symbol, ``text_side`` flips a pull
resistor's texts.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from kicad_layer.sch_writer import Placed, SchematicBuilder
from kicad_layer.sexpr import child

from .circuit import Circuit, Net, PartInst, Pin
from .draw import GRID, g, hres, label_rot, stub, vcap, vres_down, vres_up
from .parts import place_part

Point = tuple[float, float]
Dir = tuple[int, int]
CAPS = ("C", "C_Polarized")


@dataclass
class At:
    x: float
    y: float
    ref: Point | None = None  # reference text, as an offset from (x, y)
    value: Point | None = None
    text_size: float | None = None


@dataclass
class Beside:
    """On the row of ``pin`` of anchor ``ref``, ``dx`` to the right of the anchor's position (or of the pin, with ``from_pin``)."""

    ref: str
    pin: str
    dx: float
    from_pin: bool = False
    ref_dy: float = -6.35
    value_dy: float = 7.62
    text_size: float | None = None


@dataclass
class Decouple:
    """Where an anchor's rail runs and its capacitors hang.

    On a vertical pin the rail runs to the ``side`` and ``first`` is the x offset of the first
    capacitor from the part position; on a horizontal pin the capacitors hang from the stub,
    ``first`` from the pin. ``stub`` is how far the rail pin's stub runs."""

    side: str = "right"
    first: float = 16.51
    pitch: float = 7.62
    stub: float = 2.54
    caps: tuple[str, ...] | None = None  # which capacitors decouple this part; None takes every undrawn one on the rail


PinKey = tuple[str, str]


def unit_key(ref: str, unit: int) -> str:
    """The layout key of one unit of a part: ``"U4"`` for unit 1, ``"U4/2"`` for unit 2."""
    return ref if unit == 1 else f"{ref}/{unit}"


def split_key(key: str) -> tuple[str, int]:
    """``"U4"`` -> ("U4", 1); ``"U4/2"`` and ``"U4/B"`` -> ("U4", 2)."""
    ref, sep, u = key.partition("/")
    if not sep:
        return key, 1
    if u.isdigit():
        return ref, int(u)
    if len(u) == 1 and u.isalpha():
        return ref, ord(u.upper()) - ord("A") + 1
    raise ValueError(f"layout key {key!r}: a unit is a number or a letter after the slash (\"U4/2\" or \"U4/B\")")


@dataclass
class Flow:
    """Where anchors without a place go: rows from ``origin``, ``width`` wide, in the circuit's part order.

    Each anchor gets its pin extent plus ``label_room`` on every side (stub and label text), rows are
    ``row_gap`` apart so hanging parts and their power symbols fit."""

    origin: Point = (38.1, 63.5)
    width: float = 330.2
    gap: float = 15.24
    row_gap: float = 30.48
    label_room: float = 27.94


@dataclass
class Described:
    """A sheet as a circuit and its layout: the sheet builder the project lists, and what the build's checks read."""

    circuit: Circuit
    layout: Layout

    def __call__(self, sch: SchematicBuilder) -> None:
        render(self.circuit, self.layout, sch)


@dataclass
class Layout:
    """How a circuit is drawn. Without ``parts`` the sheet is plain: anchors in rows, every pin a stub and a
    label, nothing to lint. With ``parts`` the sheet follows a plan and the build lints its geometry."""

    parts: dict[str, At | Beside] = field(default_factory=dict)  # by reference; one unit of a multi-unit part as "U4/2" or "U4/B" ("U4" is unit 1)
    flow: Flow | None = field(default_factory=Flow)  # anchors not in ``parts`` are laid out in rows; None demands a place for each
    notes: list[tuple[str, Point, float]] = field(default_factory=list)
    note_at: Point | None = None  # where the circuit's notes (Circuit.note) start; None stacks them under the drawing
    decouple: dict[str | tuple[str, str], Decouple] = field(default_factory=dict)  # per anchor ("U4", "U4/3") or per anchor and rail (("U4", "+9V")): its rail pins
    hang: dict[PinKey, Decouple] = field(default_factory=dict)  # per pin: decoupling on a named private net
    attach_to: dict[str, PinKey] = field(default_factory=dict)  # a two-pin part hangs from this anchor pin, not the first that reaches its net
    detour: dict[PinKey, Point] = field(default_factory=dict)  # after the stub, a bend before the label or series part
    stub_len: dict[PinKey, float] = field(default_factory=dict)
    tap: dict[PinKey, float] = field(default_factory=dict)  # distance back from a label toward the pin where parts hang
    text_side: dict[PinKey, int] = field(default_factory=dict)
    power_rot: dict[PinKey, int] = field(default_factory=dict)
    label_stub: float = 10.16
    local_stub: float = 5.08
    pin_stub: float = 2.54
    link_stub: float = 5.08
    rail_lead: float = 12.7  # from a leftward series part's far end to its power symbol
    tap_back: float = 2.54

    @property
    def placed(self) -> bool:
        """Drawn to a plan (explicit places), so geometry can go wrong and the build lints it."""
        return bool(self.parts)


# --------------------------------------------------------------------------------------


def render(c: Circuit, layout: Layout, sch: SchematicBuilder) -> None:
    """Draw the circuit onto ``sch``. Circuit problems and undrawable parts raise; geometry is the build's lint,
    run on placed sheets only."""
    problems = c.check()
    if problems:
        raise ValueError(f"{c.sheet} circuit:\n  " + "\n  ".join(problems))
    r = _Renderer(c, layout, sch)
    r.anchors()
    units = [unit_key(ref, u) for ref, inst in c.parts.items() if r.is_anchor(ref) for u in inst.units if unit_key(ref, u) not in r.placed]
    if units:
        raise ValueError(f"{c.sheet}: no place for {', '.join(units)}; give every unit a place in the layout (\"U4/2\") or keep the flow")
    for text, at, size in layout.notes:
        sch.text(text, at, size=size)
    for key in r.order:
        r.anchor_pins(key)
    missing = [ref for ref in c.parts if not r.is_anchor(ref) and ref not in r.drawn]
    if missing:
        raise ValueError(f"{c.sheet}: no rule drew {', '.join(missing)}; give them a place in the layout or connect them through an anchor")
    r.place_flags()
    r.circuit_notes()
    dnp = {ref for ref, inst in c.parts.items() if inst.dnp}
    if dnp:
        sch.mark_dnp(dnp)


class _Renderer:
    def __init__(self, c: Circuit, layout: Layout, sch: SchematicBuilder) -> None:
        self.c, self.layout, self.sch = c, layout, sch
        self.shapes = {name: s.consumer_shape for name, s in c.signals.items()}
        self.hints: dict[str, At | Beside] = {}  # the layout's places, by unit key ("U4", "U4/2")
        for key, hint in layout.parts.items():
            ref, unit = split_key(key)
            if ref not in c.parts:
                raise ValueError(f"{c.sheet}: the layout places {key}, which is not a part of the circuit")
            if unit not in c.parts[ref].units:
                raise ValueError(f"{c.sheet}: {ref} has no unit {unit} (its units: {', '.join(map(str, c.parts[ref].units))})")
            self.hints[unit_key(ref, unit)] = hint
        self.layout_refs = {split_key(key)[0] for key in self.hints}  # parts the layout places (any unit)
        self.placed: dict[str, Placed] = {}  # by unit key
        self.order: list[str] = list(self.hints)  # anchors in drawing order: the layout's, then the flow's
        self.drawn: set = set()  # two-pin parts drawn by a rule, and anchor-to-anchor links
        self.flag_requests: dict[str, list[tuple[Point, Dir, bool]]] = {}  # net -> where a flag could go, in drawing order

    # -- placement ------------------------------------------------------------------

    def place_unit(self, key: str, at: Point, **kw) -> Placed:
        ref, unit = split_key(key)
        inst = self.c.parts[ref]
        return place_part(self.sch, ref, inst.part, at, value_text=inst.value_text, unit=unit, **kw)

    def anchors(self) -> None:
        for key, hint in self.hints.items():
            if isinstance(hint, At):
                kw = {}
                if hint.ref:
                    kw["ref_pos"] = (hint.x + hint.ref[0], hint.y + hint.ref[1])
                if hint.value:
                    kw["value_pos"] = (hint.x + hint.value[0], hint.y + hint.value[1])
                if hint.text_size:
                    kw["text_size"] = hint.text_size
                self.placed[key] = self.place_unit(key, (hint.x, hint.y), **kw)
        for key, hint in self.hints.items():
            if isinstance(hint, Beside):
                pin = self.c.parts[split_key(hint.ref)[0]].pin(hint.pin)
                anchor = self.pl(pin)
                px, py = anchor.pin(pin.number)
                x = g((px if hint.from_pin else self.anchor_x(self.key_of(pin))) + hint.dx)
                kw = {"text_size": hint.text_size} if hint.text_size else {}
                self.placed[key] = self.place_unit(key, (x, py), ref_pos=(x, py + hint.ref_dy), value_pos=(x, py + hint.value_dy), **kw)
        if self.layout.flow is not None:
            self.flow(self.layout.flow)

    def flow(self, f: Flow) -> None:
        """Anchors the layout does not place, in rows: every unit of every part with other than two pins, in the circuit's order.
        A part without pins (a mounting hole) takes a nominal 5.08 mm box."""
        x, row_y, row_h = f.origin[0], f.origin[1], 0.0
        for ref, inst in self.c.parts.items():
            if inst.two_pin:
                continue
            for unit in inst.units:
                key = unit_key(ref, unit)
                if key in self.placed:
                    continue
                trial = self.place_unit(key, (0.0, 0.0))
                pts = [trial.pin(p.number) for p in inst.unit_pins(unit)]
                self.sch.items.pop()
                self.sch.placed.pop()
                room = f.label_room if pts else 2 * GRID  # no pins: no stubs or labels around it
                if not pts:
                    pts = [(-2 * GRID, -2 * GRID), (2 * GRID, 2 * GRID)]
                x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
                y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
                w, h = (x1 - x0) + 2 * room, (y1 - y0) + 2 * room
                if x > f.origin[0] and x + w > f.origin[0] + f.width:
                    x, row_y, row_h = f.origin[0], g(row_y + row_h + f.row_gap), 0.0
                at = (g(x + room - x0), g(row_y + room - y0))
                mid = g(at[0] + (x0 + x1) / 2)
                self.placed[key] = self.place_unit(key, at, ref_pos=(mid, g(at[1] + y0 - 2.54)), value_pos=(mid, g(at[1] + y1 + 2.54)))
                self.order.append(key)
                x, row_h = g(x + w + f.gap), max(row_h, h)

    def anchor_x(self, key: str) -> float:
        hint = self.hints.get(key)
        if isinstance(hint, At):
            return hint.x
        return self.placed[key].at[0]

    def key_of(self, pin: Pin) -> str:
        """The unit key of the anchor a pin is drawn on (a common pin: the part's first unit)."""
        return unit_key(pin.ref, pin.unit or self.c.parts[pin.ref].units[0])

    def pl(self, pin: Pin) -> Placed:
        """The placed unit a pin is drawn on."""
        return self.placed[self.key_of(pin)]

    def is_anchor(self, ref: str) -> bool:
        """The part is placed as an anchor (by the layout or the flow), not drawn by a rule."""
        return any(unit_key(ref, u) in self.placed for u in self.c.parts[ref].units)

    # -- one anchor's pins ------------------------------------------------------------

    def anchor_pins(self, key: str) -> None:
        ref, unit = split_key(key)
        inst, pl = self.c.parts[ref], self.placed[key]
        power: dict[tuple[str, str], list[Pin]] = {}
        handled: dict[Point, Net | None] = {}
        for pin in inst.unit_pins(unit):
            net = self.c.net_of(pin)
            pos = pl.pin(pin.number)
            if pos in handled and handled[pos] is net and net is not None and net.kind not in ("rail", "gnd"):
                continue  # a stacked pin: its twin's stub serves it
            handled[pos] = net
            if net is None:
                if pin in self.c.open:
                    self.sch.no_connect(pl.pin(pin.number))
                continue
            if net.kind in ("rail", "gnd"):
                power.setdefault((net.kind, net.name), []).append(pin)
            elif net.kind == "signal" or net.name:
                self.labelled(key, pin, net)
            else:
                self.private(key, pin, net)
        for (kind, name), pins in power.items():
            self.power(key, kind, name, pins)

    def outward(self, ref: str, number: str) -> Dir:
        p = self.placed[ref].symbol.pin(number)
        return {0: (-1, 0), 180: (1, 0), 90: (0, 1), 270: (0, -1)}[int(p.rotation) % 360]

    def lead(self, ref: str, pin: Pin, length: float) -> tuple[Point, Dir]:
        """A stub from the pin, bent by the pin's detour if it has one; returns the far point and the pin's direction."""
        pl = self.placed[ref]
        d = self.outward(ref, pin.number)
        key = (pin.ref, pin.number)
        if key in self.layout.detour:
            end = stub(self.sch, pl, pin.number, self.layout.stub_len.get(key, self.layout.pin_stub))
            dx, dy = self.layout.detour[key]
            far = (g(end[0] + dx), g(end[1] + dy))
            self.sch.wire(end, far)
            return far, d
        return stub(self.sch, pl, pin.number, self.layout.stub_len.get(key, length)), d

    def label_rotation(self, ref: str, pin: Pin) -> int:
        key = (pin.ref, pin.number)
        if key in self.layout.detour:
            dx, dy = self.layout.detour[key]
            if dy:
                return 90 if dy < 0 else 270
            return 0 if dx > 0 else 180
        return label_rot(self.placed[ref], pin.number)

    def hangers(self, net: Net, exclude: Pin) -> list[tuple[PartInst, Pin]]:
        """Two-pin parts on the net that no anchor rule places, with the pin they meet the net on."""
        out = []
        for p in net.pins:
            if p == exclude or self.is_anchor(p.ref):
                continue
            inst = self.c.parts[p.ref]
            if not inst.two_pin or inst.ref in self.drawn:
                continue
            owner = self.layout.attach_to.get(inst.ref)
            if owner is not None and owner != (exclude.ref, exclude.number):
                continue  # hangs elsewhere
            far = self.c.net_of(next(q for q in inst.pins if q != p))
            if far is not None and far.kind == "local" and not far.name:
                continue  # its other end is an anchor's private net: that anchor draws it in line
            out.append((inst, p))
        return out

    # -- rules ----------------------------------------------------------------------

    def labelled(self, ref: str, pin: Pin, net: Net) -> None:
        """A signal or a named private net: a label at the end of the stub, hangers at a tap."""
        key = (pin.ref, pin.number)
        hang = self.layout.hang.get(key)
        length = hang.stub if hang else (self.layout.label_stub if net.kind == "signal" else self.layout.local_stub)
        end, d = self.lead(ref, pin, length)
        rot = self.label_rotation(ref, pin)
        if net.kind == "signal":
            self.sch.hier_label(net.name, end, shape=self.shapes[net.name], rot=rot)
        else:
            self.sch.label(net.name, end, rot=rot)
            self.flag(net.name, end, self.last_direction(ref, pin, d), text=True)
        px, py = self.placed[ref].pin(pin.number)
        hangers = self.hangers(net, pin)
        across = d[0] == 0  # on a vertical wire the parts lie flat, or they would sit on it
        if hang:
            for i, (inst, near) in enumerate(hangers):
                off = hang.first + i * hang.pitch
                self.attach(inst, near, (g(px + d[0] * off), g(py + d[1] * off)), d, junction=True, inline=False, across=across, hanging=True)
            return
        if any((p.ref, p.number) in self.layout.hang for p in net.pins):
            return  # this net's hangers belong to the pin with the hang hint
        if hangers:
            back = self.layout.tap.get(key, self.layout.tap_back)
            along = self.last_direction(ref, pin, d)
            tap = (g(end[0] - along[0] * back), g(end[1] - along[1] * back))
            self.sch.junction(tap)
            for inst, near in hangers:
                self.attach(inst, near, tap, d, junction=False, inline=False, across=across)

    def last_direction(self, ref: str, pin: Pin, d: Dir) -> Dir:
        """The direction of the wire that ends at the pin's label: the detour's, or the pin's own."""
        key = (pin.ref, pin.number)
        if key in self.layout.detour:
            dx, dy = self.layout.detour[key]
            return ((dx > 0) - (dx < 0), (dy > 0) - (dy < 0))
        return d

    def private(self, ref: str, pin: Pin, net: Net) -> None:
        """An unnamed private net at an anchor pin: a link to another anchor, or a node with hangers."""
        others = [p for p in net.pins if p != pin]
        if len(others) == 1 and self.is_anchor(others[0].ref):
            self.link(ref, pin, others[0])
            return
        if any(self.is_anchor(p.ref) for p in others):
            raise ValueError(f"{self.c.sheet}: private net at {pin} mixes anchors and parts; the renderer draws either a link or a node")
        if all(self.c.parts[p.ref].ref in self.drawn for p in others):
            return
        node, d = self.lead(ref, pin, self.layout.pin_stub)
        hangers = self.hangers(net, pin)
        if len(hangers) >= 2:
            self.sch.junction(node)
        for inst, near in hangers:
            # one part alone on the pin's private net is a series element and lies in line; at a node, rails and grounds hang vertically
            self.attach(inst, near, node, d, junction=False, inline=len(hangers) == 1)

    def link(self, ref: str, pin: Pin, other: Pin) -> None:
        key = tuple(sorted((str(pin), str(other))))
        if key in self.drawn:
            return
        self.drawn.add(key)
        a, b = self.placed[ref].pin(pin.number), self.pl(other).pin(other.number)
        # straight only between two parts the layout places on one row; the flow's rows are not a plan
        if abs(a[1] - b[1]) < 1e-6 and pin.ref in self.layout_refs and other.ref in self.layout_refs:
            end = stub(self.sch, self.placed[ref], pin.number, self.layout.link_stub)
            self.sch.wire(end, b)
        else:
            name = f"{pin.ref}_{pin.name or pin.number}"
            for pl_, n_ in ((self.placed[ref], pin.number), (self.pl(other), other.number)):
                end = stub(self.sch, pl_, n_, self.layout.pin_stub)
                self.sch.label(name, end, rot=label_rot(pl_, n_))

    def attach(self, inst: PartInst, near: Pin, at: Point, d: Dir, *, junction: bool, inline: bool, across: bool = False, hanging: bool = False) -> None:
        """Draw a two-pin part with ``near`` at ``at``; what its far pin connects to decides the shape.

        ``inline`` says the part is a series element on the pin's own private net, so a rail or ground
        beyond it is drawn in line too; otherwise rails pull up and grounds pull down. ``hanging`` says the
        node is a spaced hang point, where a named net beyond hangs down to its label; ``across`` says the
        node is on a vertical wire, so the part lies flat to the right."""
        far_pin = next(p for p in inst.pins if p != near)
        far = self.c.net_of(far_pin)
        if far is None:
            raise ValueError(f"{self.c.sheet}: {far_pin} is on nothing")
        self.drawn.add(inst.ref)
        is_cap = inst.part.symbol[1] in CAPS
        n = near.number
        side = self.layout.text_side.get((inst.ref, n), 1)
        if junction and not (is_cap and far.kind == "gnd"):
            self.sch.junction(at)  # a pin meeting a wire away from its ends connects only through a junction (vcap draws its own)
        if across:
            _, right = hres(self.sch, inst.ref, inst.part, at, pin_at_left=n)
            self.far_end(far, right, (1, 0))
        elif far.kind == "gnd" and is_cap:
            vcap(self.sch, inst.ref, inst.part, at, junction=junction, pin_at_top=n)
        elif far.kind == "gnd" and not inline:
            self.sch.power("GND", vres_down(self.sch, inst.ref, inst.part, at, text_side=side, pin_at_top=n))
        elif far.kind == "rail" and (not inline or d[0] == 0):
            self.sch.power(far.name, vres_up(self.sch, inst.ref, inst.part, at, text_side=side, pin_at_bottom=n))
        elif hanging and far.name:
            if is_cap:
                bottom = vcap(self.sch, inst.ref, inst.part, at, junction=junction, gnd=False, pin_at_top=n).pin(far_pin.number)
            else:
                bottom = vres_down(self.sch, inst.ref, inst.part, at, text_side=side, pin_at_top=n)
            self.far_end(far, bottom, (0, 1))
        elif d[0] < 0:  # in line, leftward: the far pin at the far end
            left = (g(at[0] - 7.62), at[1])
            hres(self.sch, inst.ref, inst.part, left, pin_at_left=far_pin.number)
            self.far_end(far, left, d)
        else:  # in line, rightward
            _, right = hres(self.sch, inst.ref, inst.part, at, pin_at_left=n)
            self.far_end(far, right, d)

    def far_end(self, far: Net, at: Point, d: Dir) -> None:
        rot = 180 if d[0] < 0 else (270 if d[1] > 0 else 0)
        if far.kind == "signal":
            self.sch.hier_label(far.name, at, shape=self.shapes[far.name], rot=rot)
        elif far.kind == "local" and far.name:
            self.sch.label(far.name, at, rot=rot)
            self.flag(far.name, at, d, text=True)
        elif far.kind == "rail" and d[0] < 0:
            lead = (g(at[0] - self.layout.rail_lead), at[1])
            self.sch.wire(at, lead)
            self.sch.power(far.name, lead)
        elif far.kind in ("rail", "gnd"):
            self.sch.power(far.name, at)
        else:
            raise ValueError(f"{self.c.sheet}: a private net beyond an in-line part is not drawn")

    def power(self, ref: str, kind: str, name: str, pins: list[Pin]) -> None:
        """Rail or ground pins of one anchor, side by side: stubs, a rail, one power symbol; decoupling hangs off a rail."""
        pl, L = self.placed[ref], self.layout
        part = split_key(ref)[0]
        dec = next((L.decouple[k] for k in ((ref, name), (part, name), ref, part) if k in L.decouple), Decouple())
        caps = [inst for inst in self.c.parts.values() if inst.two_pin and not self.is_anchor(inst.ref) and inst.ref not in self.drawn
                and self.decouples(inst, kind, name) and (dec.caps is None or inst.ref in dec.caps)]
        for side in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            mine, at = [], set()
            for p in pins:
                if self.outward(ref, p.number) == side and pl.pin(p.number) not in at:  # stacked pins share a stub
                    mine.append(p)
                    at.add(pl.pin(p.number))
            if not mine:
                continue
            stub_len = dec.stub if kind == "rail" else L.pin_stub
            ends = [stub(self.sch, pl, p.number, L.stub_len.get((p.ref, p.number), stub_len)) for p in mine]
            if len(ends) == 1 and not (kind == "rail" and caps):
                self.sch.power(name, ends[0], rot=L.power_rot.get((mine[0].ref, mine[0].number), 0))
                self.flag(name, ends[0], side)
                continue
            if side[0] != 0:  # horizontal pins: the power symbol at the stub end, capacitors hanging from the first stub
                if len(ends) > 1:  # a column of pins on one side of a connector: a vertical rail through the stub ends
                    ends.sort(key=lambda e: e[1])
                    self.sch.wire(ends[0], ends[-1])
                    for e in ends[1:-1]:
                        self.sch.junction(e)
                    end = ends[-1] if kind == "gnd" else ends[0]
                    beyond = (end[0], g(end[1] + (L.pin_stub if kind == "gnd" else -L.pin_stub)))
                    self.sch.wire(end, beyond)
                    self.sch.power(name, beyond)
                    self.flag(name, beyond, (0, 1 if kind == "gnd" else -1))
                else:
                    self.sch.power(name, ends[0])
                    self.flag(name, ends[0], side)
                px, py = pl.pin(mine[0].number)
                if caps:
                    far = (g(px + side[0] * (dec.first + (len(caps) - 1) * dec.pitch)), py)
                    if side[0] * (far[0] - ends[0][0]) > 0:
                        self.sch.wire(ends[0], far)  # the stub is shorter than the row of capacitors: carry the wire on to the last one
                for i, cap in enumerate(caps):
                    vcap(self.sch, cap.ref, cap.part, (g(px + side[0] * (dec.first + i * dec.pitch)), py), junction=True)
                    self.drawn.add(cap.ref)
                continue
            ends.sort()
            y = ends[0][1]
            if kind == "rail":
                self.sch.power(name, ends[0])
                self.flag(name, ends[0], side)
                for e in ends[1:]:
                    self.sch.junction(e)
                if caps:
                    px = self.anchor_x(ref)
                    sign = 1 if dec.side == "right" else -1
                    xs = [g(px + sign * (dec.first + i * dec.pitch)) for i in range(len(caps))]
                    if len(ends) == 1:
                        self.sch.junction(ends[0])  # the power symbol sits on a through-wire
                    self.sch.wire(ends[0] if sign > 0 else ends[-1], (xs[-1], y))
                    for i, (cap, x) in enumerate(zip(caps, xs)):
                        vcap(self.sch, cap.ref, cap.part, (x, y), junction=i < len(caps) - 1)
                        self.drawn.add(cap.ref)
                elif len(ends) > 1:
                    self.sch.wire(ends[0], ends[-1])
            else:
                self.sch.wire(ends[0], ends[-1])
                for e in ends[1:-1]:
                    self.sch.junction(e)
                foot = (ends[-1][0], g(ends[-1][1] + L.pin_stub * side[1]))
                self.sch.wire(ends[-1], foot)
                self.sch.power(name, foot)
                self.flag(name, foot, side)

    def flag(self, name: str, at: Point, d: Dir = (1, 0), *, text: bool = False) -> None:
        """Note a place for the PWR_FLAG of a net the circuit declares a source that ERC cannot see: the end ``at`` of a
        wire running in direction ``d`` (``text``: a label reads on along it). ``place_flags`` draws one flag per net."""
        if name in self.c.flags:
            self.flag_requests.setdefault(name, []).append((at, d, text))

    def place_flags(self) -> None:
        """One PWR_FLAG per flagged net, once everything else is drawn, where the net is first drawn: on a short lead
        square to the wire, beside the label text; failing that, the other side, along the wire past a power symbol,
        or a half-grid lead. A lead whose end or run touches any other connection point is refused, so a flag never
        lands on the next pin of a connector column."""
        points, wires = _connections(self.sch)
        for name, requests in self.flag_requests.items():
            for at, d, text in requests:
                perp = (d[1], -d[0])
                steps = [(perp, 2), ((-perp[0], -perp[1]), 2)] + ([] if text else [(d, 2)]) + [(perp, 1), ((-perp[0], -perp[1]), 1)]
                side = next((b for b in ((g(at[0] + v[0] * n * GRID), g(at[1] + v[1] * n * GRID)) for v, n in steps) if _lead_free(at, b, points, wires)), None)
                if side is not None:
                    break
            else:
                raise ValueError(f"{self.c.sheet}: no free place for the PWR_FLAG of {name}: every short lead from where it is drawn touches another net")
            self.sch.wire(at, side)
            self.sch.pwr_flag(side)
            points.add(_pt(side))
            wires.append((_pt(at), _pt(side)))

    def circuit_notes(self) -> None:
        """The circuit's own notes, one under the other: from ``Layout.note_at``, or under everything drawn."""
        if not self.c.notes:
            return
        if self.layout.note_at is not None:
            x, y = self.layout.note_at
        else:
            pts = _extent(self.sch)
            if pts:
                x, y = g(min(p[0] for p in pts)), g(max(p[1] for p in pts) + 3 * 2 * GRID)
            else:
                x, y = self.layout.flow.origin if self.layout.flow is not None else (25.4, 25.4)
        for text, size in self.c.notes:
            self.sch.text(text, (x, y), size=size)
            y = g(y + max(2 * GRID, size * 2.5))

    def decouples(self, inst: PartInst, kind: str, name: str) -> bool:
        """A two-pin part with pin 1 on this rail and pin 2 on ground."""
        if kind != "rail":
            return False
        n1, n2 = self.c.net_of(inst.pin("1")), self.c.net_of(inst.pin("2"))
        return n1 is not None and n2 is not None and n1.kind == "rail" and n1.name == name and n2.kind == "gnd"


# -- geometry of what is drawn -----------------------------------------------------------


def _pt(p: Point) -> Point:
    return (round(p[0], 3), round(p[1], 3))


def _connections(sch: SchematicBuilder) -> tuple[set[Point], list[tuple[Point, Point]]]:
    """Every point something connects at (wire ends, junctions, no-connects, labels, pins of the placed units, power
    symbols) and every wire."""
    points: set[Point] = set()
    wires: list[tuple[Point, Point]] = []
    for node in sch.items:
        tag = str(node[0])
        if tag == "wire":
            pts = child(node, "pts")
            a, b = _pt((float(pts[1][1]), float(pts[1][2]))), _pt((float(pts[2][1]), float(pts[2][2])))
            wires.append((a, b))
            points.update((a, b))
        elif tag in ("junction", "no_connect", "label", "hierarchical_label", "global_label"):
            at = child(node, "at")
            points.add(_pt((float(at[1]), float(at[2]))))
    for pl in sch.placed:
        if pl.symbol.lib_id.startswith("power:"):
            points.add(_pt(pl.at))
        else:
            points.update(_pt(pl.pin(p.number)) for p in pl.symbol.pins if p.unit in (0, pl.unit))
    return points, wires


def _on_segment(p: Point, a: Point, b: Point) -> bool:
    """``p`` lies on the closed segment ``ab``."""
    if abs((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])) > 1e-6:
        return False
    return min(a[0], b[0]) - 1e-6 <= p[0] <= max(a[0], b[0]) + 1e-6 and min(a[1], b[1]) - 1e-6 <= p[1] <= max(a[1], b[1]) + 1e-6


def _lead_free(a: Point, b: Point, points: set[Point], wires: list[tuple[Point, Point]]) -> bool:
    """A lead from ``a`` to ``b`` touches nothing but ``a``: no connection point on it past ``a``, and ``b`` on no wire."""
    a, b = _pt(a), _pt(b)
    if b in points or any(_on_segment(b, w0, w1) for w0, w1 in wires):
        return False
    return not any(p != a and _on_segment(p, a, b) for p in points)


def _extent(sch: SchematicBuilder) -> list[Point]:
    """The points of everything drawn: wire ends, labels, texts, junctions, and the placed symbols with their pins."""
    out = list(_connections(sch)[0])
    for node in sch.items:
        if str(node[0]) == "text":
            at = child(node, "at")
            out.append((float(at[1]), float(at[2])))
    out += [pl.at for pl in sch.placed]
    return out
