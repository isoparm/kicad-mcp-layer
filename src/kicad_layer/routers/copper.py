"""Operations on saved routes: the copper surgery that used to live in throwaway scripts.

Everything here works on :class:`~kicad_layer.ses.Routes` (segments and vias by net name, mm)
and returns a new ``Routes``; nothing touches a board file. The operations:

* :func:`find_staircases` reports runs of short segments that alternate between diagonal and
  axis-aligned, the shape a grid router or a row of tiny tuning bumps leaves behind.
* :func:`remove_bumps` collapses trapezoid tuning bumps (flank, top, flank) onto the straight
  line they were inserted into, then merges the collinear pieces. A bump is only removed when
  the straightened segment keeps its clearance from every other net's copper, which is the
  check that would have stopped a hand-written script from shorting an Ethernet pair.
* :func:`prune_dangling` removes segments with a free end, repeatedly, keeping any end that
  lands on a pad of the net, a via, or another segment.
* :func:`from_board` lifts a board's copper into ``Routes`` so a board edited by hand in KiCad
  can become the saved routes a generated project is rebuilt from.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from .pairrouter import _seg_seg_gap
from ..review import BoardModel
from .ses import RouteSegment, Routes, RouteVia

Point = tuple[float, float]


def _key(x: float, y: float) -> tuple[float, float]:
    return (round(x, 3), round(y, 3))


def _len(a: Point, b: Point) -> float:
    return math.dist(a, b)


def _angle(a: Point, b: Point) -> float:
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0


def _parallel(a1: Point, b1: Point, a2: Point, b2: Point, tol: float = 3.0) -> bool:
    return abs((_angle(a1, b1) - _angle(a2, b2) + 90.0) % 180.0 - 90.0) < tol


def _diagonal(a: Point, b: Point) -> bool:
    return round(_angle(a, b) / 45.0) * 45 % 90 == 45


def _dist_to_line(p: Point, a: Point, b: Point) -> float:
    (ax, ay), (bx, by), (px, py) = a, b, p
    dx, dy = bx - ax, by - ay
    return abs(dy * px - dx * py + bx * ay - by * ax) / (math.hypot(dx, dy) or 1.0)


# --------------------------------------------------------------------------------------
# chains
# --------------------------------------------------------------------------------------

Chain = list[tuple[Point, Point]]


def chains(segments: list[RouteSegment]) -> list[Chain]:
    """Segments of one net, layer and width joined end to end into oriented polylines.

    A chain stops at any point where more or fewer than two segments meet, so junctions and
    free ends are chain boundaries."""
    by_pt: dict[tuple[float, float], list[int]] = defaultdict(list)
    for k, s in enumerate(segments):
        by_pt[_key(s.x1, s.y1)].append(k)
        by_pt[_key(s.x2, s.y2)].append(k)
    used: set[int] = set()
    out: list[Chain] = []
    for k, s in enumerate(segments):
        if k in used:
            continue
        used.add(k)
        chain: Chain = [((s.x1, s.y1), (s.x2, s.y2))]
        for forward in (True, False):
            pt = _key(*chain[-1][1]) if forward else _key(*chain[0][0])
            while len(by_pt[pt]) == 2:
                cand = [j for j in by_pt[pt] if j not in used]
                if len(cand) != 1:
                    break
                j = cand[0]
                used.add(j)
                t = segments[j]
                a, b = (t.x1, t.y1), (t.x2, t.y2)
                if _key(*a) != pt:
                    a, b = b, a
                if forward:
                    chain.append((a, b))
                    pt = _key(*b)
                else:
                    chain.insert(0, (b, a))
                    pt = _key(*b)
        out.append(chain)
    return out


def _grouped(routes: Routes) -> dict[tuple[str, str, float], list[RouteSegment]]:
    groups: dict[tuple[str, str, float], list[RouteSegment]] = defaultdict(list)
    for s in routes.segments:
        groups[(s.net, s.layer, s.width)].append(s)
    return groups


# --------------------------------------------------------------------------------------
# staircases
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Staircase:
    net: str
    layer: str
    steps: int
    length: float  # mm along the run
    bbox: tuple[float, float, float, float]


def find_staircases(routes: Routes, *, min_steps: int = 4, max_step: float = 0.9) -> list[Staircase]:
    """Runs of at least ``min_steps`` consecutive segments shorter than ``max_step`` mm that alternate
    between diagonal and axis-aligned, longest first."""
    found: list[Staircase] = []
    for (net, layer, _w), segs in _grouped(routes).items():
        for chain in chains(segs):
            run: list[tuple[Point, Point]] = []

            def flush() -> None:
                if len(run) >= min_steps:
                    xs = [p[0] for a, b in run for p in (a, b)]
                    ys = [p[1] for a, b in run for p in (a, b)]
                    found.append(Staircase(net, layer, len(run), round(sum(_len(a, b) for a, b in run), 3), (min(xs), min(ys), max(xs), max(ys))))

            for a, b in chain:
                if _len(a, b) < max_step and (not run or _diagonal(a, b) != _diagonal(*run[-1])):
                    run.append((a, b))
                else:
                    flush()
                    run = [(a, b)] if _len(a, b) < max_step else []
            flush()
    found.sort(key=lambda s: -s.steps)
    return found


# --------------------------------------------------------------------------------------
# tuning bumps
# --------------------------------------------------------------------------------------


@dataclass
class BumpReport:
    removed: dict[str, int] = field(default_factory=dict)  # net -> bumps removed
    length_removed: dict[str, float] = field(default_factory=dict)  # net -> mm
    kept_for_clearance: int = 0  # bumps left alone because the straight line would crowd another net


def _clear_of_others(a: Point, b: Point, width: float, net: str, layer: str, routes: Routes, clearance: float) -> bool:
    """True when segment ab keeps ``clearance`` from every other net's copper on the same layer."""
    x0, x1 = min(a[0], b[0]) - 2.0, max(a[0], b[0]) + 2.0
    y0, y1 = min(a[1], b[1]) - 2.0, max(a[1], b[1]) + 2.0
    for s in routes.segments:
        if s.net == net or s.layer != layer:
            continue
        if max(s.x1, s.x2) < x0 or min(s.x1, s.x2) > x1 or max(s.y1, s.y2) < y0 or min(s.y1, s.y2) > y1:
            continue
        if _seg_seg_gap(a, b, (s.x1, s.y1), (s.x2, s.y2)) < width / 2 + s.width / 2 + clearance - 1e-6:
            return False
    for v in routes.vias:
        if v.net == net or not (x0 <= v.x <= x1 and y0 <= v.y <= y1):
            continue
        d = _dist_to_line((v.x, v.y), a, b) if _between((v.x, v.y), a, b) else min(_len((v.x, v.y), a), _len((v.x, v.y), b))
        if d < width / 2 + v.size / 2 + clearance - 1e-6:
            return False
    return True


def _between(p: Point, a: Point, b: Point) -> bool:
    ab = (b[0] - a[0], b[1] - a[1])
    t = ((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / ((ab[0] ** 2 + ab[1] ** 2) or 1.0)
    return 0.0 <= t <= 1.0


def remove_bumps(routes: Routes, *, nets: set[str] | None = None, max_step: float = 0.9, min_amp: float = 0.2, max_amp: float = 0.65,
                 clearance: float = 0.15) -> tuple[Routes, BumpReport]:
    """Collapse trapezoid tuning bumps on ``nets`` (all nets when None) onto their base line.

    A bump is three consecutive short segments whose middle runs parallel to the line joining the
    outer ends, standing ``min_amp`` to ``max_amp`` mm off it, with mirror-image flanks. The base
    line replaces the three only when it stays ``clearance`` clear of other nets' copper."""
    report = BumpReport()
    out = Routes(nets=set(routes.nets))
    out.vias = list(routes.vias)
    for (net, layer, width), segs in _grouped(routes).items():
        if nets is not None and net not in nets:
            out.segments += segs
            continue
        for chain in chains(segs):
            poly = list(chain)
            changed = True
            while changed:
                changed = False
                for i in range(len(poly) - 2):
                    (a1, b1), (a2, b2), (a3, b3) = poly[i], poly[i + 1], poly[i + 2]
                    if max(_len(a1, b1), _len(a2, b2), _len(a3, b3)) >= max_step:
                        continue
                    a, b = a1, b3
                    if _len(a, b) < 0.3 or not _parallel(a2, b2, a, b):
                        continue
                    amp = (_dist_to_line(a2, a, b) + _dist_to_line(b2, a, b)) / 2
                    if not (min_amp <= amp <= max_amp):
                        continue
                    if abs(_len(a1, b1) - _len(a3, b3)) > 0.25 * max(_len(a1, b1), _len(a3, b3)):
                        continue
                    if not _clear_of_others(a, b, width, net, layer, routes, clearance):
                        report.kept_for_clearance += 1
                        continue
                    extra = _len(a1, b1) + _len(a2, b2) + _len(a3, b3) - _len(a, b)
                    poly[i:i + 3] = [(a, b)]
                    report.removed[net] = report.removed.get(net, 0) + 1
                    report.length_removed[net] = round(report.length_removed.get(net, 0.0) + extra, 4)
                    changed = True
                    break
            merged: list[tuple[Point, Point]] = [poly[0]]
            for a, b in poly[1:]:
                pa, pb = merged[-1]
                if _key(*pb) == _key(*a) and _parallel(pa, pb, a, b, tol=1.0):
                    merged[-1] = (pa, b)
                else:
                    merged.append((a, b))
            for a, b in merged:
                out.segments.append(RouteSegment(net, layer, width, round(a[0], 4), round(a[1], 4), round(b[0], 4), round(b[1], 4)))
    return out, report


# --------------------------------------------------------------------------------------
# dangling copper
# --------------------------------------------------------------------------------------


def prune_dangling(routes: Routes, board: BoardModel, *, tol: float = 0.01) -> tuple[Routes, list[RouteSegment]]:
    """Drop segments with an end that touches nothing of their net, until none are left.

    An end is connected when a pad of the net (any hole, or a pad on the segment's layer) covers
    it, a via of the net sits on it, or another segment of the net on the same layer ends there."""
    pads: dict[str, list[tuple[Point, tuple[float, float], list[str], bool]]] = defaultdict(list)
    for f in board.footprints:
        for p in f.pads:
            if p.net:
                pads[p.net].append(((p.x, p.y), p.size, list(p.layers), p.drill is not None))
    removed: list[RouteSegment] = []
    segs = list(routes.segments)
    while True:
        ends: dict[tuple[str, str, tuple[float, float]], int] = defaultdict(int)
        for s in segs:
            ends[(s.net, s.layer, _key(s.x1, s.y1))] += 1
            ends[(s.net, s.layer, _key(s.x2, s.y2))] += 1
        vias = {(v.net, _key(v.x, v.y)) for v in routes.vias}

        def held(s: RouteSegment, pt: Point) -> bool:
            if ends[(s.net, s.layer, _key(*pt))] > 1 or (s.net, _key(*pt)) in vias:
                return True
            for (px, py), (w, h), layers, hole in pads.get(s.net, []):
                if not (hole or s.layer in layers or "*.Cu" in layers):
                    continue
                if abs(pt[0] - px) <= w / 2 + tol and abs(pt[1] - py) <= h / 2 + tol:
                    return True
            return False

        drop = [s for s in segs if not held(s, (s.x1, s.y1)) or not held(s, (s.x2, s.y2))]
        if not drop:
            break
        removed += drop
        ids = {id(s) for s in drop}
        segs = [s for s in segs if id(s) not in ids]
    out = Routes(segments=segs, vias=list(routes.vias))
    out.nets = {s.net for s in segs} | {v.net for v in out.vias}
    return out, removed


# --------------------------------------------------------------------------------------
# board -> routes
# --------------------------------------------------------------------------------------


def from_board(board: BoardModel) -> Routes:
    """The board's tracks and vias as saved routes (arcs are not carried; KiCad writes none for our generators)."""
    out = Routes()
    for s in board.segments:
        if s.net:
            out.segments.append(RouteSegment(s.net, s.layer, s.width, s.x1, s.y1, s.x2, s.y2))
    for v in board.vias:
        if v.net:
            out.vias.append(RouteVia(v.net, v.x, v.y, v.size, v.drill))
    out.nets = {s.net for s in out.segments} | {v.net for v in out.vias}
    return out
