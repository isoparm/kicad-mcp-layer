"""A module sheet from the signal table: the module's connector symbols, a hierarchical label on
every signal pin, no-connect flags, ground rails, and every power group brought to a rail with
its decoupling and a power symbol.

A module is whatever the board plugs into or breaks out: a compute module on two mezzanine
connectors, a signal header, a cable connector. A pin is named by number (``"37"``) when no other
connector of the module has that number, or with its connector (``"J2.37"``), in the signal table,
``no_connect``, ``gnd_pins`` and the power groups alike; a number two connectors share must name one.

Layout convention on each side of a connector symbol: ground rail ``gnd_rail`` outward, power
rails ``PowerGroup.offset`` outward, label stubs ``label_stub`` outward. A power group's run
leaves one end of its rail horizontally, carries the decoupling capacitors near its far end and
ends in the power symbol. Wires cross without connecting; KiCad joins only endpoints and junctions.
Only the data differs between boards; each board's module file supplies it.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from kicad_layer.sch_writer import Placed, SchematicBuilder

from .draw import attach_label, g, ground_rails, outward, rail, vcap
from .parts import Part, place_part
from .signals import Signal, split_pin

# ground by name, whatever the symbol's author called it: GND, GNDA, AGND, PGND, SIG_GND, VSS, VSSA, 0V
GROUND_NAME = re.compile(r"^(GND\w*|\w*_GND|[ADPS]GND\w*|VSS\w*|0V)$", re.IGNORECASE)


@dataclass(frozen=True)
class PowerGroup:
    net: str
    pins: list[str]  # module pins ("3" or "J2.3"), all on one side of one connector symbol
    caps: tuple[tuple[str, Part], ...] = ()  # decoupling along the run, the one nearest the power symbol first
    offset: float = 7.62  # the rail's distance outward from the pins
    reach: float = 27.94  # the run from the rail to the power symbol
    from_end: str = "top"  # the rail end the run leaves from: "top" or "bottom"
    pitch: float = 5.08  # capacitor spacing along the run


@dataclass(frozen=True)
class ModuleSheet:
    """A module sheet as data. A pin is "37" when no other connector of the module has that number, else "J2.37",
    in ``signals``, ``no_connect``, ``gnd_pins`` and the power groups alike; a shared number left bare is refused."""

    parts: tuple[tuple[str, Part, tuple[float, float], tuple[float, float], tuple[float, float]], ...]  # ref, part, at, ref_pos, value_pos
    signals: list[Signal]
    no_connect: list[str]
    power: tuple[PowerGroup, ...]
    texts: tuple[tuple[str, tuple[float, float], float], ...] = ()  # text, at, size
    gnd_rail: float = 5.08
    label_stub: float = 10.16
    gnd_prefix: str = "GND"
    gnd_pins: tuple[str, ...] = ()  # ground pins ("2" or "J1.2") whatever their name; pins named GND*, AGND, *_GND, VSS* or 0V are ground anyway


@dataclass(frozen=True)
class DescribedModule:
    """A module sheet as data: the sheet builder the project lists, and what the build's checks read."""

    module: ModuleSheet

    def __call__(self, sch: SchematicBuilder) -> dict[str, Placed]:
        return build_module_sheet(self.module, sch)


def locate(numbers: Mapping[str, Iterable[str]], key: str, what: str, ref: str = "") -> tuple[str, str]:
    """The ``(reference, number)`` a module pin key names, given each connector's pin numbers. A bare number
    must be on exactly one connector; ``"J2.37"`` (or ``ref``) picks one."""
    r, n = split_pin(key, ref)
    if r:
        if r not in numbers:
            raise ValueError(f"{what}: pin {key} names {r}, which is not a connector of the module ({', '.join(numbers)})")
        if n not in set(numbers[r]):
            raise ValueError(f"{what}: {r} has no pin {n}")
        return r, n
    owners = [ref_ for ref_, ns in numbers.items() if n in set(ns)]
    if not owners:
        raise ValueError(f"{what}: no connector of the module has pin {n}")
    if len(owners) > 1:
        raise ValueError(f"{what}: pin {n} is on {' and '.join(owners)}; name the connector ({owners[0]}.{n})")
    return owners[0], n


def is_ground(name: str, prefix: str = "GND") -> bool:
    """A ground pin by its name: ``prefix`` as before, or any common ground name (``GROUND_NAME``)."""
    return name.startswith(prefix) or bool(GROUND_NAME.match(name))


def ground_pins(m: ModuleSheet, pins_of: Mapping[str, Iterable]) -> set[tuple[str, str]]:
    """Every ground pin of the module, ``(reference, number)``: ``gnd_pins`` plus the pins named as ground.
    ``pins_of`` maps each connector to its library pins (``number`` and ``name``)."""
    pins_of = {r: list(ps) for r, ps in pins_of.items()}
    numbers = {r: [p.number for p in ps] for r, ps in pins_of.items()}
    out = {locate(numbers, k, "gnd_pins") for k in m.gnd_pins}
    out |= {(r, p.number) for r, ps in pins_of.items() for p in ps if is_ground(p.name, m.gnd_prefix)}
    return out


def group_pins(grp: PowerGroup, numbers: Mapping[str, Iterable[str]]) -> tuple[str, list[str]]:
    """The connector a power group sits on and its pin numbers there."""
    keys = [locate(numbers, k, f"power group {grp.net}") for k in grp.pins]
    refs = {r for r, _ in keys}
    if len(refs) != 1:
        raise ValueError(f"power group {grp.net}: pins {grp.pins} must all be on one connector, not on {', '.join(sorted(refs))}")
    return refs.pop(), [n for _, n in keys]


@dataclass(frozen=True)
class PinRoles:
    """What each module pin is, ``(reference, number)`` keyed: a signal's, a no-connect, a power group's, ground."""

    signals: dict[tuple[str, str], Signal]
    no_connect: set[tuple[str, str]]
    groups: list[tuple[PowerGroup, str, list[str]]]  # group, its connector, its pin numbers there
    ground: set[tuple[str, str]]  # ground pins that are none of the above: the ground rails


def pin_roles(m: ModuleSheet, pins_of: Mapping[str, Iterable]) -> PinRoles:
    """Every pin key of the module resolved against its connectors' library pins (``number`` and ``name``)."""
    pins_of = {r: list(ps) for r, ps in pins_of.items()}
    numbers = {r: [p.number for p in ps] for r, ps in pins_of.items()}
    signals = {locate(numbers, s.pin, f"signal {s.name}", s.ref): s for s in m.signals if s.on_module}
    no_connect = {locate(numbers, k, "no_connect") for k in m.no_connect}
    groups = [(grp, *group_pins(grp, numbers)) for grp in m.power]
    taken = set(signals) | no_connect | {(r, n) for _, r, ns in groups for n in ns}
    return PinRoles(signals, no_connect, groups, ground_pins(m, pins_of) - taken)


def build_module_sheet(m: ModuleSheet, sch: SchematicBuilder) -> dict[str, Placed]:
    mods = {ref: place_part(sch, ref, part, at, ref_pos=ref_pos, value_pos=value_pos) for ref, part, at, ref_pos, value_pos in m.parts}
    for text, at, size in m.texts:
        sch.text(text, at, size=size)
    roles = pin_roles(m, {ref: mod.symbol.pins for ref, mod in mods.items()})
    power_pins = {(ref, n) for _, ref, ns in roles.groups for n in ns}
    for ref, mod in mods.items():
        rails: list[str] = []  # this connector's ground pins, in symbol order
        for p in mod.symbol.pins:
            key = (ref, p.number)
            if key in roles.signals:
                s = roles.signals[key]
                attach_label(sch, mod, p.number, s.name, s.cm5_shape, m.label_stub)
            elif key in roles.no_connect:
                sch.no_connect(mod.pin(p.number))
            elif key in power_pins:
                continue
            elif key in roles.ground:
                rails.append(p.number)
            else:
                raise AssertionError(f"pin {p.number} {p.name} of {ref} is neither a signal, a no-connect, ground nor in a power group")
        ground_rails(sch, mod, gnd_pins=rails, offset=m.gnd_rail)
    for grp, ref, pins in roles.groups:
        mod = mods[ref]
        top, bottom = rail(sch, mod, pins, grp.offset)
        start = top if grp.from_end == "top" else bottom
        _, (dx, _dy) = outward(mod, pins[0])
        far = (g(start[0] + dx * grp.reach), start[1])
        sch.wire(start, far)
        sch.power(grp.net, far)
        for i, (cref, part) in enumerate(grp.caps):
            vcap(sch, cref, part, (g(far[0] - dx * (i + 1) * grp.pitch), far[1]))
    return mods
