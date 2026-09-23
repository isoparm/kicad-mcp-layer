"""Circuits as descriptions: parts, pins and nets, and not one coordinate.

A :class:`Circuit` says what is connected to what. Which sheet signal a pin carries, which rail
feeds it, which pins are grounded, which pins share a private net, and which are deliberately
left open. Drawing it is somebody else's job (see ``render.py``), guided by placement hints
kept apart from the circuit.

The point of the split is that the circuit can be checked as a circuit: every pin of every part
must be on exactly one net or declared open, every signal the sheet is supposed to carry must
be used, and nothing else may be called a signal. A wrong pin number becomes a failed check
rather than a wire that lands somewhere plausible.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from kicad_layer.kicad_libs import load_symbol

from .parts import Part
from .signals import Signal


@dataclass(frozen=True)
class Pin:
    ref: str
    number: str
    name: str
    rotation: int  # library pin rotation: 0 leaves the body to the left, 180 to the right, 90 down, 270 up
    unit: int = 1  # the symbol unit the pin is drawn on (a dual opamp: 1, 2 and 3 for power); 0 is common to every unit

    def __str__(self) -> str:
        return f"{self.ref}.{self.number}" + (f" ({self.name})" if self.name else "")


class PartInst:
    """A part with a reference, its pins known from the library symbol, each with the unit it is drawn on."""

    def __init__(self, ref: str, part: Part, value_text: str | None = None, dnp: bool = False) -> None:
        self.ref = ref
        self.part = part
        self.value_text = value_text
        self.dnp = dnp
        sym = load_symbol(*part.symbol)
        self.pins = [Pin(ref, p.number, p.name, int(p.rotation), p.unit) for p in sym.pins]

    @property
    def units(self) -> list[int]:
        """The symbol units that carry pins, in order: ``[1]`` for a one-unit part, ``[1, 2, 3]`` for a dual opamp with a power unit."""
        return sorted({p.unit for p in self.pins if p.unit}) or [1]

    def unit_pins(self, unit: int) -> list[Pin]:
        """The pins drawn on ``unit``; pins common to every unit (unit 0) are drawn on the first."""
        first = self.units[0]
        return [p for p in self.pins if p.unit == unit or (p.unit == 0 and unit == first)]

    def pin(self, key: str) -> Pin:
        """By number first, then by name (which must then be unique on the part)."""
        for p in self.pins:
            if p.number == key:
                return p
        named = [p for p in self.pins if p.name == key]
        if len(named) == 1:
            return named[0]
        raise KeyError(f"{self.ref} has no pin {key!r}" if not named else f"{self.ref} has {len(named)} pins named {key!r}; use the number")

    def __getitem__(self, key: str) -> Pin:
        return self.pin(key)

    @property
    def two_pin(self) -> bool:
        return len(self.pins) == 2


@dataclass
class Net:
    kind: str  # signal | rail | gnd | local
    name: str | None
    pins: list[Pin] = field(default_factory=list)


class Circuit:
    """What is connected to what on one sheet, with no coordinates. ``signals`` are the sheet's signals from the
    project's table: the names it must carry and their label shapes. Build with ``part()``, then ``signal()``,
    ``rail()``, ``gnd()``, ``net()``, ``flag()``, ``nc()``; ``check()`` lists every violation; the renderer draws it."""

    def __init__(self, sheet: str, signals: Iterable[Signal]) -> None:
        self.flags: set[str] = set()  # nets fed by something ERC cannot see (a connector, a converter output): they get a PWR_FLAG
        self.sheet = sheet
        self.signals: dict[str, Signal] = {s.name: s for s in signals}
        self.parts: dict[str, PartInst] = {}
        self.nets: list[Net] = []
        self.open: set[Pin] = set()
        self.notes: list[tuple[str, float]] = []

    # -- building -------------------------------------------------------------------

    def part(self, ref: str, part: Part, *, value_text: str | None = None, dnp: bool = False) -> PartInst:
        """Add a catalogue part as reference ``ref``; ``value_text`` overrides the Value shown; ``dnp`` marks it do-not-populate
        (``(dnp yes)`` on every unit). Pins: ``inst["3"]`` by number or unique name."""
        if ref in self.parts:
            raise ValueError(f"{ref} placed twice")
        inst = PartInst(ref, part, value_text, dnp)
        self.parts[ref] = inst
        return inst

    def signal(self, name: str, *pins: Pin) -> Net:
        """A sheet signal (a hierarchical label matching a pin of this sheet's symbol on the root)."""
        return self._add(Net("signal", name, list(pins)))

    def rail(self, name: str, *pins: Pin) -> Net:
        """A power rail (+5V, +3V3 ...): a power symbol on every pin."""
        return self._add(Net("rail", name, list(pins)))

    def gnd(self, *pins: Pin) -> Net:
        """Ground: a GND power symbol on every pin."""
        return self._add(Net("gnd", "GND", list(pins)))

    def net(self, *pins: Pin, name: str | None = None) -> Net:
        """A private net of this sheet: named by KiCad, or by ``name`` when it is drawn with local labels."""
        return self._add(Net("local", name, list(pins)))

    def flag(self, *names: str) -> None:
        """Declare nets as power sources for ERC: a PWR_FLAG is drawn where each is first labelled."""
        self.flags.update(names)

    def nc(self, inst: PartInst, *numbers: str) -> None:
        """Pins deliberately left open (no-connect flags); every other pin must be on a net."""
        for n in numbers:
            self.open.add(inst.pin(n))

    def note(self, text: str, size: float = 1.5) -> None:
        """A free text on the sheet; the renderer stacks the notes under the drawing (or from ``Layout.note_at``)."""
        self.notes.append((text, size))

    def _add(self, net: Net) -> Net:
        for existing in self.nets:
            if net.name and existing.kind == net.kind and existing.name == net.name:
                existing.pins += [pin for pin in net.pins if pin not in existing.pins]  # named a second time with the same pin
                return existing
        self.nets.append(net)
        return net

    # -- querying -------------------------------------------------------------------

    def net_of(self, pin: Pin) -> Net | None:
        """The net a pin is on, or None."""
        return next((n for n in self.nets if pin in n.pins), None)

    def check(self) -> list[str]:
        """Everything a circuit must satisfy before anyone draws it."""
        problems: list[str] = []
        seen: dict[Pin, str] = {}
        for net in self.nets:
            for p in net.pins:
                if p.ref not in self.parts:
                    problems.append(f"{p}: not a part of this circuit")
                where = net.name or "a private net"
                if p in seen:
                    problems.append(f"{p}: on {seen[p]} and on {where}")
                if p in self.open:
                    problems.append(f"{p}: connected to {where} but declared open")
                seen[p] = where
        for inst in self.parts.values():
            for p in inst.pins:
                if p not in seen and p not in self.open:
                    problems.append(f"{p}: not connected and not declared open")
        expected = set(self.signals)
        used = {n.name for n in self.nets if n.kind == "signal"}
        for name in sorted(used - expected):
            problems.append(f"signal {name}: not a signal of the {self.sheet} sheet")
        for name in sorted(expected - used):
            problems.append(f"signal {name}: the {self.sheet} sheet must carry it and does not")
        for net in self.nets:
            if net.kind == "local" and len(net.pins) < 2:
                problems.append(f"private net with one pin: {net.pins[0] if net.pins else 'none'}")
        return problems
