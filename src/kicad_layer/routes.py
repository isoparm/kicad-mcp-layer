"""Routes as data: saved next to a generated design and re-applied at every build.

A generated board is rebuilt from scratch each time, so routing must live outside the board file.
``Routes`` (segments and vias in mm, by net name) serialises to JSON; ``apply()`` writes them into a
BoardBuilder after the footprints. Nets are matched by name, which is how KiCad 10 boards reference
them, so a route survives a rebuild as long as its net still exists and the pads it reaches have
not moved. ``stale()`` reports the routes whose ends no longer touch any pad or other route of the
same net, which is what a placement change leaves behind.

The model (``RouteSegment``, ``RouteVia``, ``Routes``) lives here so that a design needs nothing from
the routers to re-apply its copper; the routers import it from here.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from .pcb_writer import BoardBuilder


@dataclass
class RouteSegment:
    net: str
    layer: str
    width: float
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass
class RouteVia:
    net: str
    x: float
    y: float
    size: float
    drill: float


@dataclass
class Routes:
    segments: list[RouteSegment] = field(default_factory=list)
    vias: list[RouteVia] = field(default_factory=list)
    nets: set[str] = field(default_factory=set)

    def to_json(self) -> dict:
        return {
            "segments": [[s.net, s.layer, s.width, s.x1, s.y1, s.x2, s.y2] for s in self.segments],
            "vias": [[v.net, v.x, v.y, v.size, v.drill] for v in self.vias],
        }

    @classmethod
    def from_json(cls, data: dict) -> "Routes":
        r = cls()
        for net, layer, w, x1, y1, x2, y2 in data.get("segments", []):
            r.segments.append(RouteSegment(net, layer, w, x1, y1, x2, y2))
            r.nets.add(net)
        for net, x, y, size, drill in data.get("vias", []):
            r.vias.append(RouteVia(net, x, y, size, drill))
            r.nets.add(net)
        return r


def load(path: Path) -> Routes:
    if not path.is_file():
        return Routes()
    return Routes.from_json(json.loads(path.read_text(encoding="utf-8")))


def save(routes: Routes, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(routes.to_json(), indent=0), encoding="utf-8", newline='\n')


def merge(base: Routes, extra: Routes, *, replace_nets: bool = True) -> Routes:
    """``extra`` on top of ``base``; with ``replace_nets`` the base's routes on the extra's nets are dropped first."""
    out = Routes()
    drop = extra.nets if replace_nets else set()
    out.segments = [s for s in base.segments if s.net not in drop] + list(extra.segments)
    out.vias = [v for v in base.vias if v.net not in drop] + list(extra.vias)
    out.nets = {s.net for s in out.segments} | {v.net for v in out.vias}
    return out


def apply(pcb: BoardBuilder, routes: Routes, *, copper_layers: int = 4) -> tuple[int, int]:
    outer = ("F.Cu", "B.Cu")
    for s in routes.segments:
        pcb.segment((s.x1, s.y1), (s.x2, s.y2), width=s.width, layer=s.layer, net=s.net)
    for v in routes.vias:
        pcb.via((v.x, v.y), net=v.net, size=v.size, drill=v.drill, layers=outer)
    return len(routes.segments), len(routes.vias)


def stale(routes: Routes, pad_points: dict[str, list[tuple[float, float]]], tol: float = 0.05) -> list[str]:
    """Nets whose routed copper touches none of the net's pads any more."""
    bad = []
    for net in sorted(routes.nets):
        pads = pad_points.get(net, [])
        if not pads:
            bad.append(net)
            continue
        ends = [(s.x1, s.y1) for s in routes.segments if s.net == net] + [(s.x2, s.y2) for s in routes.segments if s.net == net]
        ends += [(v.x, v.y) for v in routes.vias if v.net == net]
        if not any(math.dist(e, p) <= tol + 0.6 for e in ends for p in pads):
            bad.append(net)
    return bad
