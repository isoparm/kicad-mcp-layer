"""Sheet signals: what crosses between a module sheet and its consumer sheets, and which module pin carries it."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Signal:
    name: str
    pin: str  # CM5 pin number, as a string
    direction: str  # out | in | bi | passive
    sheet: str  # consumer sheet name
    note: str = ""

    @property
    def cm5_shape(self) -> str:
        return {"out": "output", "in": "input", "bi": "bidirectional", "passive": "passive"}[self.direction]

    @property
    def consumer_shape(self) -> str:
        return {"out": "input", "in": "output", "bi": "bidirectional", "passive": "passive"}[self.direction]


def by_sheet(signals: list[Signal], sheet: str) -> list[Signal]:
    """The signals one sheet carries, in table order."""
    return [s for s in signals if s.sheet == sheet]


def check_table(signals: list[Signal], no_connect: dict[str, str], rails: list[str]) -> None:
    """Every module pin is accounted for exactly once: signal, no-connect or rail."""
    used: dict[str, str] = {}
    for s in signals:
        assert s.pin not in used, f"pin {s.pin} used twice: {used[s.pin]} and {s.name}"
        used[s.pin] = s.name
    for p in no_connect:
        assert p not in used, f"pin {p} is both {used[p]} and a no-connect"
        used[p] = "NC"
    for p in rails:
        assert p not in used, f"pin {p} is both {used[p]} and a rail"
        used[p] = "rail"
    names = [s.name for s in signals]
    assert len(names) == len(set(names)), "duplicate signal name"
