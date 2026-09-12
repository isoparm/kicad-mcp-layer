"""Single-track clean-up router for the connections an autorouter leaves open.

FreeRouting gives up on a few connections of a dense board and the DRC lists them as unconnected
items: two copper features (pads or track ends) of one net. This module joins each such pair with one
track of the net's class: the board's other copper is the obstacle set (grown by the net's half width
plus the larger of the two class clearances), the net's own tracks and vias are passable while its pads
stay obstacles that the stubs below reach from outside (the centreline must not run through the pad row
it is leaving), layer changes cost a via, and the result is appended to the routes as plain segments and
vias. Wider classes go first and the nets ripped up to make room last; what one connection adds is an
obstacle for the next one of another net and part of the island for the same net.

Each end of a connection is an island of the net's copper (a pad, or a pad with the stub the autorouter
left on it). The search may start from any of the end's escape options, tried in this order:

* an island point already in open space (an end or vertex of the stub the autorouter left), nearest
  the other end first, dangling track ends before vertices;
* a straight stub out of a pad at the class width, away from the footprint centre first;
* a neck: a narrower stub (``NECK_WIDTHS``) that may sit off the pad's centre line, for a wide rail
  leaving a connector pad past a locating peg or along a board edge, the class width taking over at
  its end;
* failing those, a stub to the nearest open grid cell within ``FALLBACK_REACH``;
* an escape via next to the island when the free space on the end's own layer is a pocket: a region
  of fewer than ``POCKET_CELLS`` cells with no spot for a via, which a track cannot leave.

A stub or via is judged against the real geometry the way the plane stitcher checks its stubs (pads of
other nets with the larger of the two class clearances and the pad's own override, unplated holes with
``NPTH_GAP``, tracks and vias of other nets, ``HOLE_GAP`` drill edge to drill edge, keep-outs and the
board edge; a gap equal to the clearance passes, as in KiCad) before the grid has its say, so the
reasons an end cannot escape name the copper in the way, not just a blocked cell. The search itself is
``astar_single``, a plain cell-and-layer A*, because the pair router's heading-state search runs out of
nodes on a route across a whole board; it runs on the ``step`` grid first and on the ``fine_step`` grid
when that fails (two stubs 0.4 mm apart fit in reality but not on a 0.2 mm grid grown by half a step).
A connection neither grid can close is reported in ``CleanupResult.failed`` with the reasons; nothing
is raised.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .pairrouter import _merge_short, _pad_rect, _pull, astar_single, build_grid, simplify
from ..review import BoardModel, FpGeo, PadGeo, load_board
from ..routing import load_netclasses, netclass_for
from .ses import RouteSegment, Routes, RouteVia
from .stitch import _rect_dist, _seg_point_dist, _seg_seg_dist

NPTH_GAP = 0.25  # copper to an unplated hole, as the stitcher uses
EDGE_GAP = 0.3  # copper to the board edge
STUB_STEPS = (0.5, 0.8, 1.1, 1.5, 2.0, 2.6)  # stub end beyond the pad edge, mm
FALLBACK_REACH = 1.5  # a stub to the nearest free cell may be this long
HOLE_GAP = 0.5  # drill edge to drill edge between a new via and any other hole
NECK_WIDTHS = (0.4, 0.3, 0.25, 0.2)  # a wide class track may leave its pad through a narrower neck
POCKET_CELLS = 1500  # a free region smaller than this is a pocket the search cannot leave
VIA_RINGS = (0.15, 0.3, 0.45, 0.6, 0.9, 1.2)  # escape via positions around a trapped track end, mm


@dataclass
class OpenConnection:
    net: str
    a: tuple[float, float]
    b: tuple[float, float]
    layer_a: str | None = None  # None: a through-hole pad or unknown, any layer will do
    layer_b: str | None = None


@dataclass
class CleanupResult:
    routes: Routes
    routed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


_NET = re.compile(r"\[([^\]]+)\]")
_LAYER = re.compile(r"\bon (F\.Cu|B\.Cu|In\d\.Cu)\b")


def open_connections_from_drc(drc: dict) -> list[OpenConnection]:
    """The DRC report's unconnected items as (net, point, point) with the layer of each end when stated."""
    out = []
    for u in drc.get("unconnected_items", []):
        items = u.get("items", [])
        if len(items) < 2:
            continue
        d0, d1 = items[0]["description"], items[1]["description"]
        m = _NET.search(d0)
        if not m:
            continue
        net = m.group(1)
        la = _LAYER.search(d0)
        lb = _LAYER.search(d1)
        out.append(OpenConnection(net, (items[0]["pos"]["x"], items[0]["pos"]["y"]), (items[1]["pos"]["x"], items[1]["pos"]["y"]),
                                  la.group(1) if la else None, lb.group(1) if lb else None))
    return out


# --------------------------------------------------------------------------------------
# clearance against the real geometry
# --------------------------------------------------------------------------------------


def _pad_layers(p: PadGeo, layers: list[str]) -> list[str]:
    if p.kind in ("thru_hole", "np_thru_hole"):
        return list(layers)
    return [l for l in layers if l in p.layers or "*.Cu" in p.layers or "F&B.Cu" in p.layers]


def _seg_rect_gap(a: tuple[float, float], b: tuple[float, float], r: tuple[float, float, float, float]) -> float:
    """Exact distance between segment ab and an axis-aligned rectangle (0 when they touch)."""
    x0, y0, x1, y1 = r
    if x0 <= a[0] <= x1 and y0 <= a[1] <= y1:
        return 0.0
    if x0 <= b[0] <= x1 and y0 <= b[1] <= y1:
        return 0.0
    edges = (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0)))
    return min(_seg_seg_dist(a, b, c, d) for c, d in edges)


def _bbox(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float, float, float]:
    return min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])


def _apart(bb: tuple[float, float, float, float], r: tuple[float, float, float, float], gap: float) -> bool:
    """True when rectangle r lies more than ``gap`` from bounding box bb along one axis, so nothing inside bb
    can come within ``gap`` of it: the cheap test before an exact distance."""
    return r[0] - gap > bb[2] or r[2] + gap < bb[0] or r[1] - gap > bb[3] or r[3] + gap < bb[1]


class Clearance:
    """The board's copper as the stitcher sees it: pads, tracks and vias with their nets, for checking a
    straight stub that the grid cannot judge (it is grown for the class width and blocks the pad itself)."""

    def __init__(self, bm: BoardModel, classes: dict, assignments: list, layers: list[str], keepouts: list[tuple[float, float, float, float]] = ()):
        self.layers = layers
        self.pads: list[tuple[PadGeo, tuple[float, float, float, float], FpGeo]] = [(p, _pad_rect(p), f) for f in bm.footprints for p in f.pads]
        self.segs: list[tuple[str, tuple[float, float], tuple[float, float], float, str | None, tuple[float, float, float, float]]] = \
            [(s.layer, (s.x1, s.y1), (s.x2, s.y2), s.width, s.net, _bbox((s.x1, s.y1), (s.x2, s.y2))) for s in bm.segments]
        self.vias: list[tuple[tuple[float, float], float, float, str | None]] = [((v.x, v.y), v.size, v.drill, v.net) for v in bm.vias]
        self.outline = bm.outline
        self.keepouts = list(keepouts)
        self._classes, self._assignments = classes, assignments
        self._clr: dict[str, float] = {}

    def add(self, routes: Routes) -> None:
        self.segs += [(s.layer, (s.x1, s.y1), (s.x2, s.y2), s.width, s.net, _bbox((s.x1, s.y1), (s.x2, s.y2))) for s in routes.segments]
        self.vias += [((v.x, v.y), v.size, v.drill, v.net) for v in routes.vias]

    def net_clearance(self, net: str | None) -> float:
        """KiCad applies the larger of the two nets' class clearances."""
        if not net or net.startswith("__"):
            return 0.0
        if net not in self._clr:
            _, c = netclass_for(net, self._classes, self._assignments)
            self._clr[net] = float(c.get("clearance", 0.125))
        return self._clr[net]

    def stub_clear(self, net: str, layer: str, a: tuple[float, float], b: tuple[float, float], width: float, clearance: float,
                   own: PadGeo | None = None) -> str | None:
        """None when a track of ``width`` from a to b on ``layer`` violates nothing; else what it hits.
        ``own`` is the pad the stub leaves: its paste windows and copper pieces (net-less pads of the same
        footprint under it) do not count. A gap equal to the required clearance passes, as in KiCad."""
        half = width / 2
        bb = _bbox(a, b)
        if self.outline is not None:
            x0, y0, x1, y1 = self.outline
            for x, y in (a, b):
                if not (x0 + EDGE_GAP + half <= x <= x1 - EDGE_GAP - half and y0 + EDGE_GAP + half <= y <= y1 - EDGE_GAP - half):
                    return "board edge"
        for kx0, ky0, kx1, ky1 in self.keepouts:
            if _seg_rect_gap(a, b, (kx0, ky0, kx1, ky1)) < half + clearance:
                return "keep-out"
        for q, r, f in self.pads:
            if q is own:
                continue
            if q.kind == "np_thru_hole":
                gap = half + NPTH_GAP
                if not _apart(bb, r, gap) and _seg_rect_gap(a, b, r) < gap - 1e-6:
                    return f"NPTH hole of {f.ref}"
                continue
            if q.net == net:
                continue
            if not q.net and q.kind == "smd" and own is not None and f.ref == own.ref and _rect_dist(own.x, own.y, r) <= max(own.size) / 2:
                continue
            if layer not in _pad_layers(q, self.layers):
                continue
            gap = half + max(clearance, self.net_clearance(q.net), q.clearance or 0.0)
            if not _apart(bb, r, gap) and _seg_rect_gap(a, b, r) < gap - 1e-6:
                return f"pad {f.ref}-{q.number}"
        for sl, sa, sb, sw, snet, sbb in self.segs:
            if snet == net or sl != layer:
                continue
            gap = half + sw / 2 + max(clearance, self.net_clearance(snet))
            if not _apart(bb, sbb, gap) and _seg_seg_dist(sa, sb, a, b) < gap - 1e-6:
                return f"{snet} track"
        for (vx, vy), vs, _, vnet in self.vias:
            if vnet == net:
                continue
            gap = half + vs / 2 + max(clearance, self.net_clearance(vnet))
            if not _apart(bb, (vx, vy, vx, vy), gap) and _seg_point_dist(a, b, (vx, vy)) < gap - 1e-6:
                return f"{vnet} via"
        return None

    def via_clear(self, net: str, x: float, y: float, size: float, drill: float, clearance: float) -> str | None:
        """None when a via of ``net`` at (x, y) violates nothing on any layer; else what it hits. Holes keep
        ``HOLE_GAP`` between their edges whatever the net."""
        r = size / 2
        if self.outline is not None:
            x0, y0, x1, y1 = self.outline
            if not (x0 + EDGE_GAP + r <= x <= x1 - EDGE_GAP - r and y0 + EDGE_GAP + r <= y <= y1 - EDGE_GAP - r):
                return "board edge"
        for kx0, ky0, kx1, ky1 in self.keepouts:
            if _rect_dist(x, y, (kx0, ky0, kx1, ky1)) < r + clearance:
                return "keep-out"
        for q, rect, f in self.pads:
            if q.drill and math.dist((q.x, q.y), (x, y)) < drill / 2 + q.drill / 2 + HOLE_GAP - 1e-6:
                return f"hole of {f.ref}-{q.number}" if q.number else f"NPTH hole of {f.ref}"
            if q.kind == "np_thru_hole":
                if _rect_dist(x, y, rect) < r + NPTH_GAP - 1e-6:
                    return f"NPTH hole of {f.ref}"
                continue
            if q.net == net:
                continue
            if _rect_dist(x, y, rect) < r + max(clearance, self.net_clearance(q.net), q.clearance or 0.0) - 1e-6:
                return f"pad {f.ref}-{q.number}"
        for _, sa, sb, sw, snet, sbb in self.segs:
            if snet == net:
                continue
            gap = r + sw / 2 + max(clearance, self.net_clearance(snet))
            if not _apart((x, y, x, y), sbb, gap) and _seg_point_dist(sa, sb, (x, y)) < gap - 1e-6:
                return f"{snet} track"
        for (vx, vy), vs, vd, vnet in self.vias:
            d = math.dist((vx, vy), (x, y))
            if d < drill / 2 + vd / 2 + HOLE_GAP - 1e-6:
                return f"{vnet} via hole"
            if vnet != net and d < r + vs / 2 + max(clearance, self.net_clearance(vnet)) - 1e-6:
                return f"{vnet} via"
        return None


# --------------------------------------------------------------------------------------
# the island an open connection starts from
# --------------------------------------------------------------------------------------


def _key(x: float, y: float) -> tuple[float, float]:
    return round(x, 3), round(y, 3)


def island_points(bm: BoardModel, extra: Routes, net: str, at: tuple[float, float], layers: list[str]) -> list[tuple[tuple[float, float], list[str], bool]]:
    """Every copper point of ``net`` reachable from ``at`` over the net's own segments, vias and pads: (point,
    layers it exists on, whether it is a dangling track end). Segments touch where an end lies on another's
    end (vias are the same point on every layer); a pad joins every end inside its rectangle."""
    segs = [s for s in bm.segments if s.net == net] + [s for s in extra.segments if s.net == net]
    vias = [(v.x, v.y) for v in bm.vias if v.net == net] + [(v.x, v.y) for v in extra.vias if v.net == net]
    pads = [p for f in bm.footprints for p in f.pads if p.net == net]
    nodes: dict[tuple[float, float], set[str]] = {}
    adj: dict[tuple[float, float], set[tuple[float, float]]] = {}
    degree: Counter = Counter()

    def node(k, lays):
        nodes.setdefault(k, set()).update(lays)
        adj.setdefault(k, set())

    for s in segs:
        k1, k2 = _key(s.x1, s.y1), _key(s.x2, s.y2)
        node(k1, [s.layer])
        node(k2, [s.layer])
        adj[k1].add(k2)
        adj[k2].add(k1)
        degree[k1] += 1
        degree[k2] += 1
    for x, y in vias:
        node(_key(x, y), layers)
    pad_nodes = []
    for p in pads:
        k = _key(p.x, p.y)
        node(k, _pad_layers(p, layers))
        pad_nodes.append(k)
        r = _pad_rect(p)
        for other in list(nodes):
            if other != k and r[0] - 1e-6 <= other[0] <= r[2] + 1e-6 and r[1] - 1e-6 <= other[1] <= r[3] + 1e-6:
                adj[k].add(other)
                adj[other].add(k)
    if not nodes:
        return []
    start = min(nodes, key=lambda k: math.dist(k, at))
    if math.dist(start, at) > 0.3:
        return []
    seen = {start}
    todo = [start]
    while todo:
        k = todo.pop()
        for n in adj[k]:
            if n not in seen:
                seen.add(n)
                todo.append(n)
    out = []
    for k in seen:
        dangling = degree[k] == 1 and k not in pad_nodes and k not in {_key(x, y) for x, y in vias}
        out.append((k, sorted(nodes[k]), dangling))
    return out


def _pad_at(bm: BoardModel, net: str, pt: tuple[float, float]) -> tuple[PadGeo, FpGeo] | None:
    for f in bm.footprints:
        for p in f.pads:
            if p.net == net and abs(p.x - pt[0]) < 0.01 and abs(p.y - pt[1]) < 0.01:
                return p, f
    return None


# --------------------------------------------------------------------------------------
# the router
# --------------------------------------------------------------------------------------


def _open(grid, layer: str, i: int, j: int, vias: bool = False, limit: int = POCKET_CELLS) -> bool:
    """True when the search can leave the free region around cell (i, j) on ``layer``: the region has at least
    ``limit`` cells, or (with ``vias``) one of its cells takes a via by the grid's via mask. A pocket between a
    pad row and its neighbours' stubs is smaller and, without a via spot, a dead end."""
    if grid.blocked(layer, i, j):
        return False
    seen = {(i, j)}
    todo = [(i, j)]
    while todo and len(seen) < limit:
        ci, cj = todo.pop()
        if vias and len(grid.layers) > 1 and grid.via_fits(ci, cj, 0):
            return True
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                n = (ci + di, cj + dj)
                if n not in seen and not grid.blocked(layer, *n):
                    seen.add(n)
                    todo.append(n)
    return len(seen) >= limit


@dataclass
class Escape:
    """Where a search may start for one end of a connection, and the copper that leads there."""
    point: tuple[float, float]
    layer: str
    stubs: list[tuple[str, float, tuple[float, float], tuple[float, float]]] = field(default_factory=list)  # layer, width, from, to
    vias: list[tuple[float, float]] = field(default_factory=list)
    kind: str = "free"


class _Escaper:
    """The escape options of one end of a connection, for one net's grid."""

    def __init__(self, bm: BoardModel, grid, clear: Clearance, done: Routes, net: str, width: float, clearance: float, via: tuple[float, float], layers: list[str]):
        self.bm, self.grid, self.clear, self.done, self.net = bm, grid, clear, done, net
        self.width, self.clearance, self.via, self.layers = width, clearance, via, layers
        self.reasons: Counter = Counter()

    def free(self, layer: str, q: tuple[float, float]) -> bool:
        return layer in self.layers and not self.grid.blocked(layer, *self.grid.idx(*q))

    def usable(self, layer: str, q: tuple[float, float]) -> str | None:
        """None when the search may start at q on ``layer``; else why not: the grid cell is blocked, or it lies
        in a pocket the search could not leave. The caller counts the reason, after the real geometry has had
        the chance to name the copper in the way."""
        if not self.free(layer, q):
            return "end not free"
        if not _open(self.grid, layer, *self.grid.idx(*q), vias=True):
            return "end in a pocket"
        return None

    def _pad_axes(self, pad: PadGeo, fp: FpGeo):
        w_, h_ = pad.size
        if abs((pad.angle % 180) - 90) < 1e-6:
            w_, h_ = h_, w_
        along = [(1, 0), (-1, 0)] if w_ >= h_ else [(0, 1), (0, -1)]
        across = [(0, 1), (0, -1)] if w_ >= h_ else [(1, 0), (-1, 0)]
        for dirs in (along, across):
            dirs.sort(key=lambda d: d[0] * (fp.x - pad.x) + d[1] * (fp.y - pad.y))  # away from the footprint centre first
        return w_, h_, along + across

    def options(self, pt: tuple[float, float], layer_hint: str | None, toward: tuple[float, float]) -> list[Escape]:
        """In order of preference: island points already in open space (nearest the far end first, dangling
        ends before vertices), a straight stub out of a pad at the class width, a narrower neck that may sit
        off the pad's centre line (a wide rail leaving a connector pad past a locating peg), a stub to the
        nearest open cell, and a via out of a pocket a track cannot leave."""
        island = island_points(self.bm, self.done, self.net, pt, self.layers)
        if not island:
            island = [(pt, [layer_hint] if layer_hint in self.layers else self.layers, False)]
        out: list[Escape] = []
        ranked = sorted(island, key=lambda e: (0 if e[2] else 1, math.dist(e[0], toward)))
        for q, lays, _ in ranked:
            for l in lays:
                bad = self.usable(l, q)
                if bad is None:
                    out.append(Escape(q, l))
                    break
                self.reasons[bad] += 1
            if len(out) >= 2:
                break
        pads = [(q, lays) for q, lays, _ in ranked if _pad_at(self.bm, self.net, q) is not None]
        pads.sort(key=lambda e: math.dist(e[0], toward))
        stub = self._stub_option(pads)
        if stub is not None:
            out.append(stub)
        else:
            near = self._nearest_option(ranked)
            if near is not None:
                out.append(near)
        out += self._via_options(ranked, pads)[:2]
        return out

    def _stub_option(self, pads) -> Escape | None:
        for q, lays in pads:
            pad, fp = _pad_at(self.bm, self.net, q)
            w_, h_, dirs = self._pad_axes(pad, fp)
            full = min(self.width, min(w_, h_))
            for sw in [full] + [w for w in NECK_WIDTHS if w < full]:
                for d in dirs:
                    half = w_ / 2 if d[0] else h_ / 2
                    span = (h_ if d[0] else w_) / 2 - sw / 2  # room to shift a neck across the pad
                    shifts = [0.0] if sw == full or span <= 1e-6 else [0.0, span, -span, span / 2, -span / 2]
                    for shift in shifts:
                        s = (q[0] + (0.0 if d[0] else shift), q[1] + (0.0 if d[1] else shift))
                        for t in STUB_STEPS:
                            e = (s[0] + d[0] * (half + t), s[1] + d[1] * (half + t))
                            for l in lays:
                                why = self.clear.stub_clear(self.net, l, s, e, sw, self.clearance, own=pad)
                                if why is not None:
                                    self.reasons[f"stub too close to {why}"] += 1
                                    continue
                                bad = self.usable(l, e)  # the end must also be a cell the class-width search can start from
                                if bad is not None:
                                    self.reasons[bad] += 1
                                    continue
                                return Escape(e, l, stubs=[(l, sw, s, e)], kind="stub" if sw == full else "neck")
        return None

    def _nearest_option(self, ranked) -> Escape | None:
        best = None
        reach = int(math.ceil(FALLBACK_REACH / self.grid.step))
        for q, lays, _ in ranked:
            i0, j0 = self.grid.idx(*q)
            pad = _pad_at(self.bm, self.net, q)
            sw = min(self.width, min(pad[0].size)) if pad else self.width
            for l in lays:
                for di in range(-reach, reach + 1):
                    for dj in range(-reach, reach + 1):
                        e = self.grid.xy(i0 + di, j0 + dj)
                        dd = math.dist(e, q)
                        if dd > FALLBACK_REACH or (best is not None and dd >= best[0]) or not self.free(l, e):
                            continue
                        if not _open(self.grid, l, i0 + di, j0 + dj, vias=True):
                            continue
                        why = self.clear.stub_clear(self.net, l, q, e, sw, self.clearance, own=pad[0] if pad else None)
                        if why is None:
                            best = (dd, q, e, l, sw)
                        else:
                            self.reasons[f"stub too close to {why}"] += 1
        if best is None:
            return None
        _, q, e, l, sw = best
        return Escape(e, l, stubs=[(l, sw, q, e)], kind="stub")

    def _via_options(self, ranked, pads) -> list[Escape]:
        """Escape vias next to the island's points, checked against the real geometry: the grid's own via test
        wants a clear disk of a full track clearance around the via and refuses every tight spot."""
        v_size, v_drill = self.via
        out: list[Escape] = []
        pad_keys = {q for q, _ in pads}
        for q, lays, _ in ranked:
            if not lays:
                continue
            others = [l for l in self.layers if l not in lays] or [l for l in self.layers if l != lays[0]]
            if not others:
                continue
            pad = _pad_at(self.bm, self.net, q) if q in pad_keys else None
            if pad is not None:
                w_, h_, dirs = self._pad_axes(*pad)
                sw = min(self.width, min(w_, h_))
                cands = [(q[0] + d[0] * ((w_ / 2 if d[0] else h_ / 2) + t), q[1] + d[1] * ((w_ / 2 if d[0] else h_ / 2) + t))
                         for d in dirs for t in (0.5, 0.65, 0.8, 1.0, 1.3, 1.6, 2.0)]
                own = pad[0]
            else:
                sw = self.width
                cands = [q] + [(q[0] + r * math.cos(k * math.pi / 4), q[1] + r * math.sin(k * math.pi / 4)) for r in VIA_RINGS for k in range(8)]
                own = None
            found = None
            for c in cands:
                why = self.clear.via_clear(self.net, c[0], c[1], v_size, v_drill, self.clearance)
                if why is None and math.dist(c, q) > 1e-6:
                    why = self.clear.stub_clear(self.net, lays[0], q, c, sw, self.clearance, own=own)
                if why is not None:
                    self.reasons[f"via too close to {why}"] += 1
                    continue
                for lo in others:
                    bad = self.usable(lo, c)  # the other layer must be open there for the search to go on
                    if bad is not None:
                        self.reasons[bad] += 1
                        continue
                    stubs = [(lays[0], sw, q, c)] if math.dist(c, q) > 1e-6 else []
                    found = Escape(c, lo, stubs=stubs, vias=[c], kind="via")
                    break
                if found is not None:
                    break
            if found is not None:
                out.append(found)
            if len(out) >= 2:
                break
        return out


def route_open_connections(board: Path | BoardModel, project: Path | None, connections: list[OpenConnection], *, layers: tuple[str, ...] = ("F.Cu", "B.Cu"),
                           step: float = 0.2, fine_step: float | None = 0.1, keepouts: list[tuple[float, float, float, float]] = (), via_cost: float = 25.0,
                           max_nodes: int = 3_000_000, max_tries: int = 6, last: set[str] = frozenset()) -> CleanupResult:
    """Route every open connection with one track: the escape options of both ends (``_Escaper.options``) are
    tried in order until ``astar_single`` finds a path, on the ``step`` grid first and, when that fails, on the
    ``fine_step`` grid (two stubs 0.4 mm apart fit in reality but not on a 0.2 mm grid grown by half a step).
    Wider classes go first and the nets in ``last`` (routes ripped up to make room) after everything else; what
    one connection adds is an obstacle for the next one of another net and part of the island for the same net."""
    bm = board if isinstance(board, BoardModel) else load_board(board)
    if project is None and not isinstance(board, BoardModel):
        cand = board.with_suffix(".kicad_pro")
        project = cand if cand.is_file() else None
    classes, assignments = load_netclasses(project)
    layer_list = list(layers)
    result = CleanupResult(routes=Routes())
    done = Routes()  # what this pass has added so far
    clear = Clearance(bm, classes, assignments, layer_list, keepouts)

    def class_of(oc: OpenConnection) -> tuple[float, float, float, float]:
        _, cls = netclass_for(oc.net, classes, assignments)
        return (float(cls.get("track_width", 0.15)), float(cls.get("clearance", 0.125)), float(cls.get("via_diameter", 0.6)), float(cls.get("via_drill", 0.3)))

    def attempt(oc: OpenConnection, st: float) -> tuple[Routes | None, str]:
        """The copper for one connection on a grid of ``st``, or None and why not."""
        width, clearance, via_size, via_drill = class_of(oc)
        grow = width / 2 + clearance + st / 2
        obstacles = Routes(segments=[s for s in done.segments if s.net != oc.net], vias=[v for v in done.vias if v.net != oc.net], nets=set())
        grid = build_grid(bm, obstacles, layer_list, st, grow, skip_nets={oc.net}, keepouts=list(keepouts), clearance=clearance,
                          net_clearance=clear.net_clearance, via=(via_size, via_drill), hole_gap=HOLE_GAP, npth_gap=NPTH_GAP - clearance)
        esc = _Escaper(bm, grid, clear, done, oc.net, width, clearance, (via_size, via_drill), layer_list)
        starts = esc.options(oc.a, oc.layer_a, oc.b)
        goals = esc.options(oc.b, oc.layer_b, oc.a) if starts else []
        if not starts or not goals:
            end = oc.a if not starts else oc.b
            top = "; ".join(f"{r} x{n}" for r, n in esc.reasons.most_common(3))
            return None, f"no escape from {end} ({top})"
        path, chosen, tries = None, None, 0
        for s in starts:
            for g in goals:
                if tries >= max_tries:
                    break
                tries += 1
                path = astar_single(grid, (*grid.idx(*s.point), s.layer), (*grid.idx(*g.point), g.layer), via_cost=via_cost * step / st,
                                    no_via_cells=2, max_nodes=max_nodes)
                if path:
                    chosen = (s, g)
                    break
            if path:
                break
        if not path:
            tried = ", ".join(f"{e.kind}@{e.point[0]:.2f},{e.point[1]:.2f}/{e.layer}" for e in starts) + " -> " + \
                    ", ".join(f"{e.kind}@{e.point[0]:.2f},{e.point[1]:.2f}/{e.layer}" for e in goals)
            return None, f"no path ({tried})"
        s, g = chosen
        a, b = s.point, g.point
        path = simplify(path)
        pieces: list[tuple[str, list[tuple[float, float]]]] = []
        cur_layer, cur = path[0][2], [a]
        for (i, j, l) in path[1:]:
            xy = grid.xy(i, j)
            if l != cur_layer:
                pieces.append((cur_layer, cur))
                cur_layer, cur = l, [cur[-1]]
                continue
            cur.append(xy)
        if len(cur) >= 2:
            cur[-1] = b
        else:
            cur.append(b)  # both ends in one cell: a direct link
        pieces.append((cur_layer, cur))
        new = Routes(nets={oc.net})
        for l, sw, p0, p1 in s.stubs + g.stubs:
            new.segments.append(RouteSegment(oc.net, l, sw, round(p0[0], 4), round(p0[1], 4), round(p1[0], 4), round(p1[1], 4)))
        for vx, vy in s.vias + g.vias:
            new.vias.append(RouteVia(oc.net, round(vx, 4), round(vy, 4), via_size, via_drill))
        for n, (l, pts) in enumerate(pieces):
            pts = _merge_short(_pull(grid, l, pts), 0.4, grid, l)
            for p0, p1 in zip(pts, pts[1:]):
                if math.dist(p0, p1) > 0.005:
                    new.segments.append(RouteSegment(oc.net, l, width, round(p0[0], 4), round(p0[1], 4), round(p1[0], 4), round(p1[1], 4)))
            if n < len(pieces) - 1:
                vx, vy = pts[-1]
                new.vias.append(RouteVia(oc.net, round(vx, 4), round(vy, 4), via_size, via_drill))
        how = "".join(f" [{e.kind} escape at {e.point[0]:.2f},{e.point[1]:.2f}]" for e in (s, g) if e.kind != "free")
        return new, f"{sum(math.dist((q.x1, q.y1), (q.x2, q.y2)) for q in new.segments):.1f} mm, {len(new.vias)} via(s){how}"

    for k in sorted(range(len(connections)), key=lambda k: (connections[k].net in last, -class_of(connections[k])[0], k)):
        oc = connections[k]
        new, note = attempt(oc, step)
        if new is None and fine_step and fine_step < step:
            new, fine_note = attempt(oc, fine_step)
            note = f"{fine_note} on the {fine_step} mm grid" if new is not None else f"{note}; on the {fine_step} mm grid: {fine_note}"
        if new is None:
            result.failed.append(f"{oc.net}: {note}")
            continue
        result.routes.segments += new.segments
        result.routes.vias += new.vias
        result.routes.nets |= new.nets
        done.segments += new.segments
        done.vias += new.vias
        clear.add(new)
        result.routed.append(f"{oc.net}: {note}")
    return result
