"""Sheet signals: what crosses between sheets, and which module pin carries it.

A signal usually runs between the module sheet and one consumer sheet (``sheet``), on one module pin
(``pin``). The pin is a number (``"37"``) when it is unique across the module's connectors, or names its
connector: ``"J2.37"``, or ``pin="37", ref="J2"``. A signal between two consumer sheets has no module pin:
``pin=""`` and ``to`` names the other sheet. ``direction`` is seen from the module, or from ``to`` for a
sheet-to-sheet signal: ``out`` means that side drives it into ``sheet``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

_REF_PIN = re.compile(r"([A-Za-z#][A-Za-z0-9_#]*)\.(.+)")
_FLIP = {"out": "in", "in": "out", "bi": "bi", "passive": "passive"}


def split_pin(key: str, ref: str = "") -> tuple[str, str]:
    """``(reference, number)`` of a module pin written ``"37"``, ``"J2.37"`` or ``"37"`` with ``ref``; the
    reference is ``""`` when the pin does not name one."""
    m = _REF_PIN.fullmatch(key)
    if m and not ref:
        return m.group(1), m.group(2)
    return ref, key


@dataclass(frozen=True)
class Signal:
    name: str
    pin: str  # the module pin: "37", or "J2.37" on a module with several connectors; "" for a sheet-to-sheet signal
    direction: str  # out | in | bi | passive, seen from the module (or from ``to``)
    sheet: str  # consumer sheet name
    note: str = ""
    ref: str = ""  # the module connector the pin is on, when ``pin`` does not say it
    to: str = ""  # the other consumer sheet of a signal that does not touch the module

    def __post_init__(self) -> None:
        if self.direction not in _FLIP:
            raise ValueError(f"signal {self.name}: direction {self.direction!r} is not one of {tuple(_FLIP)}")
        if self.to and self.pin:
            raise ValueError(f"signal {self.name}: a sheet-to-sheet signal ({self.sheet} - {self.to}) has no module pin")
        if not self.to and not self.pin:
            raise ValueError(f"signal {self.name}: give the module pin, or the other sheet in to=")
        if self.to == self.sheet:
            raise ValueError(f"signal {self.name}: both ends on {self.sheet}")

    @property
    def on_module(self) -> bool:
        """Whether a module pin carries it (False for a sheet-to-sheet signal)."""
        return not self.to

    @property
    def module_pin(self) -> tuple[str, str]:
        """``(reference, number)`` of the module pin; the reference is ``""`` when the table gives the number alone."""
        return split_pin(self.pin, self.ref)

    @property
    def cm5_shape(self) -> str:
        """The label shape on the module side (or on ``to``)."""
        return {"out": "output", "in": "input", "bi": "bidirectional", "passive": "passive"}[self.direction]

    @property
    def consumer_shape(self) -> str:
        return {"out": "input", "in": "output", "bi": "bidirectional", "passive": "passive"}[self.direction]

    def seen_from(self, sheet: str) -> Signal:
        """The signal as ``sheet`` carries it: itself on its consumer sheet; on the ``to`` end of a sheet-to-sheet
        signal, the mirror (``sheet`` and ``to`` swapped, the direction turned), so ``consumer_shape`` fits there too."""
        if self.to and sheet == self.to:
            return replace(self, sheet=self.to, to=self.sheet, direction=_FLIP[self.direction])
        return self


def by_sheet(signals: list[Signal], sheet: str) -> list[Signal]:
    """The signals one sheet carries, in table order, each as that sheet sees it (``Signal.seen_from``)."""
    return [s.seen_from(sheet) for s in signals if sheet in (s.sheet, s.to)]


def check_table(signals: list[Signal], no_connect: dict[str, str], rails: list[str]) -> None:
    """Every module pin is accounted for exactly once: signal, no-connect or rail. Pins are keys as in
    ``Signal.pin`` (``"37"`` or ``"J2.37"``); sheet-to-sheet signals take no pin."""
    used: dict[tuple[str, str], str] = {}

    def show(key: tuple[str, str]) -> str:
        return f"{key[0]}.{key[1]}" if key[0] else key[1]

    for s in signals:
        if not s.on_module:
            continue
        key = s.module_pin
        assert key not in used, f"pin {show(key)} used twice: {used[key]} and {s.name}"
        used[key] = s.name
    for p in no_connect:
        key = split_pin(p)
        assert key not in used, f"pin {show(key)} is both {used[key]} and a no-connect"
        used[key] = "NC"
    for p in rails:
        key = split_pin(p)
        assert key not in used, f"pin {show(key)} is both {used[key]} and a rail"
        used[key] = "rail"
    names = [s.name for s in signals]
    assert len(names) == len(set(names)), "duplicate signal name"
