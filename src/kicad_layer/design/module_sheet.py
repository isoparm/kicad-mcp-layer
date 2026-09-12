"""A module sheet from the signal table: the module's connector symbols, a hierarchical label on
every signal pin, no-connect flags, ground rails, and every power group brought to a rail with
its decoupling and a power symbol.

Layout convention on each side of a connector symbol: ground rail ``gnd_rail`` outward, power
rails ``PowerGroup.offset`` outward, label stubs ``label_stub`` outward. A power group's run
leaves one end of its rail horizontally, carries the decoupling capacitors near its far end and
ends in the power symbol. Wires cross without connecting; KiCad joins only endpoints and junctions.
Only the data differs between boards; each board's module file supplies it.
"""
from __future__ import annotations

from dataclasses import dataclass

from kicad_layer.sch_writer import Placed, SchematicBuilder

from .draw import attach_label, g, ground_rails, outward, rail, vcap
from .parts import Part, place_part
from .signals import Signal


@dataclass(frozen=True)
class PowerGroup:
    net: str
    pins: list[str]  # module pin numbers, all on one side of one connector symbol
    caps: tuple[tuple[str, Part], ...] = ()  # decoupling along the run, the one nearest the power symbol first
    offset: float = 7.62  # the rail's distance outward from the pins
    reach: float = 27.94  # the run from the rail to the power symbol
    from_end: str = "top"  # the rail end the run leaves from: "top" or "bottom"
    pitch: float = 5.08  # capacitor spacing along the run


@dataclass(frozen=True)
class ModuleSheet:
    parts: tuple[tuple[str, Part, tuple[float, float], tuple[float, float], tuple[float, float]], ...]  # ref, part, at, ref_pos, value_pos
    signals: list[Signal]
    no_connect: list[str]
    power: tuple[PowerGroup, ...]
    texts: tuple[tuple[str, tuple[float, float], float], ...] = ()  # text, at, size
    gnd_rail: float = 5.08
    label_stub: float = 10.16
    gnd_prefix: str = "GND"


@dataclass(frozen=True)
class DescribedModule:
    """A module sheet as data: the sheet builder the project lists, and what the build's checks read."""

    module: ModuleSheet

    def __call__(self, sch: SchematicBuilder) -> dict[str, Placed]:
        return build_module_sheet(self.module, sch)


def build_module_sheet(m: ModuleSheet, sch: SchematicBuilder) -> dict[str, Placed]:
    mods = {ref: place_part(sch, ref, part, at, ref_pos=ref_pos, value_pos=value_pos) for ref, part, at, ref_pos, value_pos in m.parts}
    for text, at, size in m.texts:
        sch.text(text, at, size=size)
    by_pin = {s.pin: s for s in m.signals}
    power_pins = {n for grp in m.power for n in grp.pins}
    for ref, mod in mods.items():
        for p in mod.symbol.pins:
            n = p.number
            if n in by_pin:
                attach_label(sch, mod, n, by_pin[n].name, by_pin[n].cm5_shape, m.label_stub)
            elif n in m.no_connect:
                sch.no_connect(mod.pin(n))
            elif p.name.startswith(m.gnd_prefix) or n in power_pins:
                continue
            else:
                raise AssertionError(f"pin {n} {p.name} of {ref} is neither a signal, a no-connect, ground nor in a power group")
        ground_rails(sch, mod, offset=m.gnd_rail)
    for grp in m.power:
        owners = [mod for mod in mods.values() if all(any(p.number == n for p in mod.symbol.pins) for n in grp.pins)]
        assert len(owners) == 1, f"power group {grp.net}: pins {grp.pins} must all be on one connector"
        mod = owners[0]
        top, bottom = rail(sch, mod, grp.pins, grp.offset)
        start = top if grp.from_end == "top" else bottom
        _, (dx, _dy) = outward(mod, grp.pins[0])
        far = (g(start[0] + dx * grp.reach), start[1])
        sch.wire(start, far)
        sch.power(grp.net, far)
        for i, (ref, part) in enumerate(grp.caps):
            vcap(sch, ref, part, (g(far[0] - dx * (i + 1) * grp.pitch), far[1]))
    return mods
