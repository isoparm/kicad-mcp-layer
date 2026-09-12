"""Drawing helpers for schematic sheets: stubs, rails, labels and the passives that hang from them.

Coordinates here are millimetres on KiCad's 1.27 mm grid; ``g`` snaps to it. Symbols are placed
unrotated and unmirrored, so a pin's outward direction comes from its library rotation.
"""
from __future__ import annotations

from kicad_layer.sch_writer import Placed, SchematicBuilder

from .parts import Part, place_part

GRID = 1.27
R_FP = "Resistor_SMD:R_0603_1608Metric"


def g(v: float) -> float:
    """Snap to the 1.27 mm schematic grid."""
    return round(round(v / GRID) * GRID, 4)

def outward(placed: Placed, number: str) -> tuple[float, float, int]:
    """The pin's connection point and the unit vector pointing away from the symbol body.

    Library pin rotation 0 points right (body to the right of the pin, wire arrives from the
    left), 180 points left, 90 up, 270 down. Symbols here are placed unrotated and unmirrored.
    """
    if placed.rot or placed.mirror:
        raise ValueError("outward() expects an unrotated, unmirrored symbol")
    p = placed.symbol.pin(number)
    x, y = placed.pin(number)
    dx, dy = {0: (-1, 0), 180: (1, 0), 90: (0, 1), 270: (0, -1)}[int(p.rotation) % 360]
    return (x, y), (dx, dy)

def stub(sch: SchematicBuilder, placed: Placed, number: str, length: float) -> tuple[float, float]:
    """A wire from the pin end outward; returns its far end."""
    (x, y), (dx, dy) = outward(placed, number)
    end = (g(x + dx * length), g(y + dy * length))
    sch.wire((x, y), end)
    return end

def label_rot(placed: Placed, number: str) -> int:
    """Rotation for a label attached outward of this pin."""
    _, (dx, dy) = outward(placed, number)
    return {(-1, 0): 180, (1, 0): 0, (0, 1): 270, (0, -1): 90}[(dx, dy)]

def attach_label(sch: SchematicBuilder, placed: Placed, number: str, name: str, shape: str, length: float = 7.62) -> None:
    """A hierarchical label at the end of a short stub from the pin."""
    end = stub(sch, placed, number, length)
    sch.hier_label(name, end, shape=shape, rot=label_rot(placed, number))

def ground_rails(sch: SchematicBuilder, placed: Placed, gnd_pins: list[str] | None = None, offset: float = 5.08) -> None:
    """One GND rail per side of an unrotated symbol, its symbol below the lowest pin of that side.

    The rail's foot ends past every pin row of the side, so it can never land on another pin's
    stub. ``gnd_pins`` defaults to the pins named GND*.
    """
    pins = gnd_pins if gnd_pins is not None else [p.number for p in placed.symbol.pins if p.name.startswith("GND")]
    for rot in (0, 180):
        side = [p.number for p in placed.symbol.pins if int(p.rotation) == rot]
        mine = [n for n in pins if n in side]
        if not mine:
            continue
        top, bottom = rail(sch, placed, mine, offset)
        lowest = max(placed.pin(n)[1] for n in side)
        foot = (bottom[0], g(lowest + offset))
        if foot != bottom:
            sch.wire(bottom, foot)
        sch.power("GND", foot)

def rail(sch: SchematicBuilder, placed: Placed, numbers: list[str], offset: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """Join several pins on one side through stubs to a vertical rail ``offset`` mm outward.

    Returns the rail's top and bottom points. Stubs cross other rails without connecting, since
    KiCad joins wires only at endpoints and junctions.
    """
    ends = [stub(sch, placed, n, offset) for n in numbers]
    ys = sorted({e[1] for e in ends})
    x = ends[0][0]
    top, bottom = (x, ys[0]), (x, ys[-1])
    if top != bottom:
        sch.wire(top, bottom)
        for e in ends:
            if e not in (top, bottom):
                sch.junction(e)
    return top, bottom

R_FP = "Resistor_SMD:R_0603_1608Metric"

def place_two_pin(sch: SchematicBuilder, ref: str, part: Part, a: tuple[float, float], b: tuple[float, float], pin_at_a: str, *, prefer: int = 0,
                  **kwargs) -> Placed:
    """Place a two-pin part with ``pin_at_a`` at ``a`` and its other pin at ``b`` (7.62 mm apart), whichever way the
    symbol's pins natively point; rotations are tried from ``prefer``, so the usual case places first time."""
    centre = (g((a[0] + b[0]) / 2), g((a[1] + b[1]) / 2))
    for rot in (prefer, (prefer + 90) % 360, (prefer + 180) % 360, (prefer + 270) % 360):
        placed = place_part(sch, ref, part, centre, rot=rot, **kwargs)
        pins = {pin.number: placed.pin(pin.number) for pin in placed.symbol.pins}
        assert len(pins) == 2, f"{ref}: {part.symbol[1]} is not a two-pin symbol"
        other = next(at for number, at in pins.items() if number != pin_at_a)
        if pins[pin_at_a] == a and other == b:
            return placed
        sch.items.pop()  # the symbol went in the wrong way round: take it back and turn it
        sch.placed.pop()
    raise ValueError(f"{ref}: no rotation of {part.symbol[1]} puts pin {pin_at_a} at {a} and the other at {b}")


def vcap(sch: SchematicBuilder, ref: str, part: Part, top: tuple[float, float], *, junction: bool = True, gnd: bool = True, pin_at_top: str = "1") -> Placed:
    """A capacitor hanging from ``top`` (its pin 1, or ``pin_at_top``); the other pin goes to GND unless ``gnd`` is false."""
    c = place_two_pin(sch, ref, part, top, (top[0], g(top[1] + 7.62)), pin_at_top, prefer=0 if pin_at_top == "1" else 180,
                      ref_pos=(top[0] + 1.905, top[1] + 1.905), value_pos=(top[0] + 1.905, top[1] + 4.445), text_size=1.0)
    if junction:
        sch.junction(top)
    if gnd:
        sch.power("GND", c.pin("2" if pin_at_top == "1" else "1"))
    return c

def hres(sch: SchematicBuilder, ref: str, part: Part, left: tuple[float, float], *, pin_at_left: str = "1") -> tuple[Placed, tuple[float, float]]:
    """A two-pin part lying horizontally with ``pin_at_left`` at ``left``; returns it and the other pin, 7.62 mm to the right."""
    r = place_two_pin(sch, ref, part, left, (g(left[0] + 7.62), left[1]), pin_at_left, prefer=90 if pin_at_left == "1" else 270,
                      ref_pos=(left[0] + 3.81, left[1] - 2.54), value_pos=(left[0] + 3.81, left[1] + 2.54), text_size=1.0)
    ends = [r.pin("1"), r.pin("2")]
    return r, next(p for p in ends if p != left)

def label_stub(sch: SchematicBuilder, placed: Placed, pin: str, name: str, length: float = 7.62, *, local: bool = True, shape: str = "passive") -> None:
    """A local (or hierarchical) label at the end of a stub from the pin."""
    end = stub(sch, placed, pin, length)
    rot = label_rot(placed, pin)
    if local:
        sch.label(name, end, rot=rot)
    else:
        sch.hier_label(name, end, shape=shape, rot=rot)

def vres_up(sch: SchematicBuilder, ref: str, part: Part, bottom: tuple[float, float], *, text_side: int = 1, pin_at_bottom: str = "2") -> tuple[float, float]:
    """A resistor standing on ``bottom`` (its pin 2, or ``pin_at_bottom``); returns its top pin, 7.62 mm up. Texts on the right, or left with -1."""
    r = place_two_pin(sch, ref, part, bottom, (bottom[0], g(bottom[1] - 7.62)), pin_at_bottom, prefer=0 if pin_at_bottom == "2" else 180,
                      ref_pos=(bottom[0] + 3.81 * text_side, bottom[1] - 5.08), value_pos=(bottom[0] + 3.81 * text_side, bottom[1] - 2.54), text_size=1.0)
    return r.pin("1" if pin_at_bottom == "2" else "2")

def vres_down(sch: SchematicBuilder, ref: str, part: Part, top: tuple[float, float], *, text_side: int = 1, pin_at_top: str = "1") -> tuple[float, float]:
    """A resistor hanging from ``top`` (its pin 1, or ``pin_at_top``); returns its bottom pin, 7.62 mm down."""
    r = place_two_pin(sch, ref, part, top, (top[0], g(top[1] + 7.62)), pin_at_top, prefer=0 if pin_at_top == "1" else 180,
                      ref_pos=(top[0] + 3.81 * text_side, top[1] + 2.54), value_pos=(top[0] + 3.81 * text_side, top[1] + 5.08), text_size=1.0)
    return r.pin("2" if pin_at_top == "1" else "1")
