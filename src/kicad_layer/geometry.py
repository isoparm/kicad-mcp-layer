"""Plane geometry for board checks: zone fills as point sets, distances, sampling.

KiCad stores a zone fill as one or more ``filled_polygon`` outlines per layer. Holes are fractured
into the outline (a slit joins each hole to the outer ring), so the even-odd rule on a single ring
answers "is this point copper". ``FillIndex`` makes that question fast on boards whose fills run to
tens of thousands of vertices: edges are bucketed in horizontal strips and a ray cast only looks at
the edges of the point's strip.

Nothing here knows about nets or rules; the checks in ``layout_rules`` and the tools in
``board_tools`` bring those.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

Point = tuple[float, float]

STRIP = 0.5  # mm per strip of the edge index


@dataclass
class Ring:
    """One filled polygon: its points, bounding box and an edge index by horizontal strip."""

    points: list[Point]
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    strips: dict[int, list[tuple[float, float, float, float]]] = field(default_factory=dict)
    area: float = 0.0

    @classmethod
    def build(cls, points: list[Point]) -> "Ring":
        r = cls(points=list(points))
        if len(points) < 3:
            return r
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        r.bbox = (min(xs), min(ys), max(xs), max(ys))
        strips: dict[int, list[tuple[float, float, float, float]]] = defaultdict(list)
        n = len(points)
        a2 = 0.0
        for i in range(n):
            x1, y1 = points[i]
            x2, y2 = points[(i + 1) % n]
            a2 += x1 * y2 - x2 * y1
            if y1 == y2:
                continue
            lo, hi = (y1, y2) if y1 < y2 else (y2, y1)
            for k in range(int(math.floor(lo / STRIP)), int(math.floor(hi / STRIP)) + 1):
                strips[k].append((x1, y1, x2, y2))
        r.strips = dict(strips)
        r.area = abs(a2) / 2
        return r

    def contains(self, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self.bbox
        if x < x0 or x > x1 or y < y0 or y > y1:
            return False
        inside = False
        for ax, ay, bx, by in self.strips.get(int(math.floor(y / STRIP)), ()):
            if (ay > y) != (by > y):
                xi = ax + (y - ay) * (bx - ax) / (by - ay)
                if xi > x:
                    inside = not inside
        return inside


class FillIndex:
    """Filled copper of zones, by (net, layer), with point queries."""

    def __init__(self) -> None:
        self.rings: dict[tuple[str | None, str], list[Ring]] = defaultdict(list)

    @classmethod
    def from_zones(cls, zones, nets: set[str] | None = None) -> "FillIndex":
        idx = cls()
        for z in zones:
            if getattr(z, "rule_area", False):
                continue
            if nets is not None and z.net not in nets:
                continue
            for layer, polys in (z.fills or {}).items():
                for pts in polys:
                    if len(pts) >= 3:
                        idx.rings[(z.net, layer)].append(Ring.build(pts))
        return idx

    @property
    def empty(self) -> bool:
        return not any(self.rings.values())

    def layers_of(self, net: str | None) -> set[str]:
        return {l for (n, l), rs in self.rings.items() if n == net and rs}

    def nets_on(self, layer: str) -> set[str | None]:
        return {n for (n, l), rs in self.rings.items() if l == layer and rs}

    def covered(self, x: float, y: float, layer: str, nets: set[str | None] | None = None) -> str | None:
        """The net whose fill covers (x, y) on ``layer`` (one of ``nets`` when given), or None."""
        for (n, l), rings in self.rings.items():
            if l != layer or (nets is not None and n not in nets):
                continue
            for r in rings:
                if r.contains(x, y):
                    return n
        return None

    def ring_at(self, x: float, y: float, layer: str, net: str | None) -> Ring | None:
        for r in self.rings.get((net, layer), ()):
            if r.contains(x, y):
                return r
        return None


def sample_segment(a: Point, b: Point, step: float) -> list[Point]:
    """Points every ``step`` mm along a-b, both ends included."""
    length = math.dist(a, b)
    n = max(1, int(math.ceil(length / step)))
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]


def runs(flags: list[bool], step: float) -> list[float]:
    """Lengths of the consecutive True stretches of ``flags`` sampled every ``step`` mm."""
    out: list[float] = []
    cur = 0
    for f in flags:
        if f:
            cur += 1
        elif cur:
            out.append(cur * step)
            cur = 0
    if cur:
        out.append(cur * step)
    return out


# ---------------------------------------------------------------- IPC-2221 conductor ampacity
OZ_TO_MIL = 1.378  # one ounce of copper per square foot is 1.378 mil (35 um) thick


def ampacity(width_mm: float, *, oz: float = 1.0, rise_c: float = 10.0, external: bool = True) -> float:
    """Current in A a conductor carries at ``rise_c`` above ambient, from IPC-2221's chart fit
    I = k * dT^0.44 * A^0.725 (A in square mils; k 0.048 outer, 0.024 inner). IPC-2152 is less
    pessimistic for most boards, so this errs on the safe side."""
    area_mil2 = (width_mm / 0.0254) * (oz * OZ_TO_MIL)
    k = 0.048 if external else 0.024
    return k * (rise_c ** 0.44) * (area_mil2 ** 0.725)


def width_for(current_a: float, *, oz: float = 1.0, rise_c: float = 10.0, external: bool = True) -> float:
    """The narrowest width in mm that carries ``current_a`` by ``ampacity``."""
    k = 0.048 if external else 0.024
    area_mil2 = (current_a / (k * rise_c ** 0.44)) ** (1 / 0.725)
    return area_mil2 / (oz * OZ_TO_MIL) * 0.0254


def via_ampacity(drill_mm: float, *, plating_um: float = 25.0, rise_c: float = 10.0) -> float:
    """A via barrel as an inner conductor: circumference times plating thickness."""
    width_equiv_mm = math.pi * drill_mm
    oz = plating_um / 35.0
    return ampacity(width_equiv_mm, oz=oz, rise_c=rise_c, external=False)
