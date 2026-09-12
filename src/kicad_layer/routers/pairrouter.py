"""A router for differential pairs, the part FreeRouting does not do.

The idea: route the pair's centreline once, as a single fat track that stands for both halves plus
their gap, then offset it into the P and N tracks. In order:

1. **Endpoints.** The two pads of each half at both ends. Where a half has more than two pads (a
   USB-C receptacle doubles its data pads) the routed pair is chosen so P and N keep their sides,
   and the extra pads are joined with a straight stub, a hook round the neighbouring pad, or a via
   jumper on the other layer.
2. **Escapes, planned for every pair first.** Each pad gets a straight stub along its long axis,
   on the side whose corridor is clear of the footprint's other pads (a stub may squeeze past a
   staggered row when both tracks fit) and points at the far end; the stubs converge to the class
   pitch, and the centreline starts 1.6 mm further out. Every pair's stubs and the trapezoid they
   converge in are reserved as obstacles for the other pairs before anything is routed.
3. **Search.** A* on a 0.2 mm grid with the heading in the state: 45 degrees per step at most,
   two straight cells between turns, a small cost per turn, the first step along the escape and the
   last along the far end's approach. The search starts and ends on the pad layers; a layer change
   costs about 5 mm and needs room for a via pair. Obstacles are every pad not on the pair's nets,
   existing copper, other pairs' reservations, keep-outs, plated holes and the board edge, grown by
   the pair's half-extent plus the class clearance and half a grid step.
4. **Tracks.** The grid path is string-pulled without creating sharp turns, offset by plus and
   minus half the pitch with mitred corners, and both halves splay apart for the via pair at each
   layer change. When P would arrive on the wrong side, P crosses under N on the other layer through
   two vias, on the longest straight run with room for them.
5. **Matching.** Trapezoid bumps (1.6, 1.2 or 0.8 mm tall, whichever fits) on the shorter half's straight runs, leaning away from the partner
   and checked against the grid, until the skew is inside the interface limit or no run has room.
   Vias count as board thickness, as KiCad measures them.

Everything routed is returned as ``Routes`` and, once written to the board or exported as protected
wiring in the DSN, becomes an obstacle for what follows. ``route_check`` measures the result.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from pathlib import Path

from ..review import BoardModel, PadGeo, load_board
from ..routing import find_pairs, load_netclasses, netclass_for, pair_name, rule_for
from .ses import RouteSegment, Routes, RouteVia

SQRT2 = math.sqrt(2.0)


@dataclass
class PairSpec:
    name: str
    p_net: str
    n_net: str
    width: float
    gap: float
    clearance: float
    via_size: float
    via_drill: float
    via_gap: float
    skew_limit: float | None
    target_ohm: float | None

    @property
    def pitch(self) -> float:
        return self.width + self.gap

    @property
    def half_extent(self) -> float:
        return self.width + self.gap / 2.0


@dataclass
class PairResult:
    name: str
    status: str  # routed | failed | skipped
    p_length: float = 0.0
    n_length: float = 0.0
    skew: float = 0.0
    layers: list[str] = field(default_factory=list)
    vias: int = 0
    notes: list[str] = field(default_factory=list)
    debug: dict = field(default_factory=dict)  # the attempted geometry when the pair is rejected


# --------------------------------------------------------------------------------------
# obstacle grid
# --------------------------------------------------------------------------------------


class Grid:
    """One bytearray per layer; 1 marks a cell the pair's centreline may not occupy."""

    def __init__(self, x0: float, y0: float, x1: float, y1: float, step: float, layers: list[str]):
        self.x0, self.y0, self.step = x0, y0, step
        self.w = int(math.ceil((x1 - x0) / step)) + 1
        self.h = int(math.ceil((y1 - y0) / step)) + 1
        self.layers = layers
        self.cells = {l: bytearray(self.w * self.h) for l in layers}
        self.via_mask: bytearray | None = None  # 1 where a via centre may not go (every layer's copper, grown for the via)

    def idx(self, x: float, y: float) -> tuple[int, int]:
        return int(round((x - self.x0) / self.step)), int(round((y - self.y0) / self.step))

    def xy(self, i: int, j: int) -> tuple[float, float]:
        return self.x0 + i * self.step, self.y0 + j * self.step

    def blocked(self, layer: str, i: int, j: int) -> bool:
        if i < 0 or j < 0 or i >= self.w or j >= self.h:
            return True
        return self.cells[layer][j * self.w + i] != 0

    def mark_disk(self, layer: str, cx: float, cy: float, r: float) -> None:
        i0, j0 = self.idx(cx - r, cy - r)
        i1, j1 = self.idx(cx + r, cy + r)
        cells = self.cells[layer]
        r2 = r * r
        for j in range(max(0, j0), min(self.h - 1, j1) + 1):
            y = self.y0 + j * self.step
            for i in range(max(0, i0), min(self.w - 1, i1) + 1):
                x = self.x0 + i * self.step
                if (x - cx) ** 2 + (y - cy) ** 2 <= r2:
                    cells[j * self.w + i] = 1

    def mark_rect(self, layer: str, x0: float, y0: float, x1: float, y1: float, grow: float) -> None:
        """An axis-aligned rectangle grown by ``grow`` with rounded corners (a Minkowski sum with a disk)."""
        i0, j0 = self.idx(x0 - grow, y0 - grow)
        i1, j1 = self.idx(x1 + grow, y1 + grow)
        cells = self.cells[layer]
        g2 = grow * grow
        for j in range(max(0, j0), min(self.h - 1, j1) + 1):
            y = self.y0 + j * self.step
            dy = 0.0 if y0 <= y <= y1 else min(abs(y - y0), abs(y - y1))
            for i in range(max(0, i0), min(self.w - 1, i1) + 1):
                x = self.x0 + i * self.step
                dx = 0.0 if x0 <= x <= x1 else min(abs(x - x0), abs(x - x1))
                if dx * dx + dy * dy <= g2:
                    cells[j * self.w + i] = 1

    def mark_segment(self, layer: str, x1: float, y1: float, x2: float, y2: float, r: float) -> None:
        n = max(1, int(math.dist((x1, y1), (x2, y2)) / (self.step / 2)))
        for k in range(n + 1):
            t = k / n
            self.mark_disk(layer, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, r)

    def mark_outside(self, layer: str, x0: float, y0: float, x1: float, y1: float) -> None:
        cells = self.cells[layer]
        for j in range(self.h):
            y = self.y0 + j * self.step
            for i in range(self.w):
                x = self.x0 + i * self.step
                if x < x0 or x > x1 or y < y0 or y > y1:
                    cells[j * self.w + i] = 1

    def via_fits(self, i: int, j: int, r_cells: int) -> bool:
        """True when a via may go at (i, j): its centre is off the via mask when the grid has one, else every cell
        within ``r_cells`` of (i, j) is free on every layer."""
        if i < 0 or j < 0 or i >= self.w or j >= self.h:
            return False
        if self.via_mask is not None:
            return self.via_mask[j * self.w + i] == 0 and all(c[j * self.w + i] == 0 for c in self.cells.values())
        rr = r_cells * r_cells
        for c in self.cells.values():
            for dj in range(-r_cells, r_cells + 1):
                for di in range(-r_cells, r_cells + 1):
                    if di * di + dj * dj > rr:
                        continue
                    ii, jj = i + di, j + dj
                    if ii < 0 or jj < 0 or ii >= self.w or jj >= self.h or c[jj * self.w + ii]:
                        return False
        return True

    def clear_disk(self, layer: str, cx: float, cy: float, r: float) -> None:
        i0, j0 = self.idx(cx - r, cy - r)
        i1, j1 = self.idx(cx + r, cy + r)
        cells = self.cells[layer]
        for j in range(max(0, j0), min(self.h - 1, j1) + 1):
            for i in range(max(0, i0), min(self.w - 1, i1) + 1):
                x, y = self.xy(i, j)
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    cells[j * self.w + i] = 0


def _pad_rect(p: PadGeo) -> tuple[float, float, float, float]:
    w, h = p.size
    if abs((p.angle % 180) - 90) < 1e-6:
        w, h = h, w
    if p.shape == "circle":
        w = h = max(w, h)
    return p.x - w / 2, p.y - h / 2, p.x + w / 2, p.y + h / 2


def build_grid(bm: BoardModel, existing: Routes, layers: list[str], step: float, grow: float, *, skip_nets: set[str], edge_margin: float = 0.3,
               keepouts: list[tuple[float, float, float, float]] = (), clearance: float = 0.125, net_clearance=None,
               via: tuple[float, float] | None = None, hole_gap: float = 0.45, npth_gap: float = 0.0) -> Grid:
    """The obstacle grid for a track of the class whose ``grow`` (half width plus clearance plus half a step) and
    ``clearance`` are given. ``net_clearance(net)`` supplies the other nets' class clearances, KiCad applying the
    larger of the two; ``via`` (diameter, drill) adds a mask of where that via's centre may not go, holes kept
    ``hole_gap`` apart; ``npth_gap`` is the extra an unplated hole gets over the class clearance."""
    assert bm.outline is not None
    x0, y0, x1, y1 = bm.outline
    g = Grid(x0, y0, x1, y1, step, layers)
    vg = None  # growth for the via mask, on top of the copper's own radius
    if via is not None:
        g.via_mask = bytearray(g.w * g.h)
        vg = via[0] / 2 + clearance + step / 2
        vl = "__via__"
        g.cells[vl] = g.via_mask
        g.mark_outside(vl, x0 + edge_margin + vg, y0 + edge_margin + vg, x1 - edge_margin - vg, y1 - edge_margin - vg)
        for k in keepouts:
            g.mark_rect(vl, k[0], k[1], k[2], k[3], edge_margin + vg)

    def extra_for(net) -> float:
        return max(0.0, net_clearance(net) - clearance) if net_clearance is not None and net else 0.0

    for l in layers:
        g.mark_outside(l, x0 + edge_margin + grow, y0 + edge_margin + grow, x1 - edge_margin - grow, y1 - edge_margin - grow)
        for k in keepouts:
            g.mark_rect(l, k[0], k[1], k[2], k[3], edge_margin + grow)
    for f in bm.footprints:
        for p in f.pads:
            # the pair's own pads stay obstacles too: the centreline must not run through the pad row it
            # is leaving; the escape stubs reach the pads from outside
            r = _pad_rect(p)
            if p.kind in ("thru_hole", "np_thru_hole"):
                pad_layers = layers
            else:
                pad_layers = [l for l in layers if l in p.layers or "*.Cu" in p.layers or "F&B.Cu" in p.layers]
            extra = max(0.0, p.clearance - clearance) if p.clearance else 0.0  # a pad's own clearance beats the class's
            extra = max(extra, extra_for(p.net) if p.net not in skip_nets else 0.0, npth_gap if p.kind == "np_thru_hole" else 0.0)
            for l in pad_layers:
                g.mark_rect(l, r[0], r[1], r[2], r[3], grow + extra)
            if vg is not None:
                if p.net in skip_nets and p.kind != "np_thru_hole":
                    hole_only = True  # the net's own pad: only its hole keeps the via's drill away
                else:
                    hole_only = False
                    g.mark_rect(vl, r[0], r[1], r[2], r[3], vg + extra)
                if p.drill:
                    g.mark_disk(vl, p.x, p.y, p.drill / 2 + via[1] / 2 + hole_gap + step / 2)
    for group in (existing, bm):
        for s in group.segments:
            if s.layer in layers and s.net not in skip_nets:
                g.mark_segment(s.layer, s.x1, s.y1, s.x2, s.y2, s.width / 2 + grow + extra_for(s.net))
                if vg is not None:
                    g.mark_segment(vl, s.x1, s.y1, s.x2, s.y2, s.width / 2 + vg + extra_for(s.net))
        for v in group.vias:
            if v.net not in skip_nets:
                for l in layers:
                    g.mark_disk(l, v.x, v.y, v.size / 2 + grow + extra_for(v.net))
                if vg is not None:
                    g.mark_disk(vl, v.x, v.y, v.size / 2 + vg + extra_for(v.net))
            if vg is not None:
                g.mark_disk(vl, v.x, v.y, v.drill / 2 + via[1] / 2 + hole_gap + step / 2)
    if vg is not None:
        del g.cells[vl]
    return g


# --------------------------------------------------------------------------------------
# A*
# --------------------------------------------------------------------------------------

MOVES = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0), (1, 1, SQRT2), (1, -1, SQRT2), (-1, 1, SQRT2), (-1, -1, SQRT2)]


# heading index -> (di, dj); the 45 degree neighbours of each heading
HEADINGS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
NO_HEADING = 8
TURN_COST = 0.15  # cells per 45 degree turn: straight runs win ties, so far fewer states are explored


def _heading_index(d: tuple[int, int]) -> int:
    sx, sy = (d[0] > 0) - (d[0] < 0), (d[1] > 0) - (d[1] < 0)
    return HEADINGS.index((sx, sy))


def astar(g: Grid, start: tuple[int, int, str], goal: tuple[int, int, str], *, via_cost: float = 25.0, via_clear_cells: int = 4, max_nodes: int = 2_500_000, no_via_cells: int = 10,
          start_dir: tuple[int, int] | None = None, goal_dir: tuple[int, int] | None = None) -> list[tuple[int, int, str]] | None:
    """Shortest 8-connected path in grid cells with the heading in the state: each step turns 45 degrees at
    most, so a differential pair never folds back on itself; the first step follows ``start_dir`` and the
    last arrives within 45 degrees of ``goal_dir``. A layer change costs ``via_cost`` cells, needs a clear
    disk on both layers and keeps the heading."""
    (si, sj, sl), (gi, gj, gl) = start, goal
    layers = g.layers
    nl = len(layers)
    layer_index = {l: k for k, l in enumerate(layers)}
    W, H = g.w, g.h
    cells = [g.cells[l] for l in layers]
    step_cost = [1.0, SQRT2, 1.0, SQRT2, 1.0, SQRT2, 1.0, SQRT2]
    allowed = [[h, (h + 1) % 8, (h - 1) % 8] for h in range(8)]
    if start_dir is not None:
        h0 = _heading_index(start_dir)
        allowed.append([h0, (h0 + 1) % 8, (h0 - 1) % 8])
    else:
        allowed.append(list(range(8)))
    goal_ok = [True] * 9
    if goal_dir is not None:
        hg = _heading_index(goal_dir)
        goal_ok = [h in (hg, (hg + 1) % 8, (hg - 1) % 8) for h in range(8)] + [False]

    MIN_STRAIGHT = 2  # cells between two turns: a U-turn then spans four cells per 45 degrees, wide enough for the pair

    def key(i: int, j: int, li: int, h: int, run: int) -> int:
        return (((i * H + j) * nl + li) * 9 + h) * (MIN_STRAIGHT + 1) + run

    def unkey(k: int) -> tuple[int, int, int, int, int]:
        run = k % (MIN_STRAIGHT + 1)
        k //= MIN_STRAIGHT + 1
        h = k % 9
        k //= 9
        li = k % nl
        k //= nl
        return k // H, k % H, li, h, run

    def heur(i: int, j: int, li: int) -> float:
        dx, dy = abs(i - gi), abs(j - gj)
        return max(dx, dy) + (SQRT2 - 1) * min(dx, dy) + (0.0 if layers[li] == gl else via_cost)

    via_cache: dict[int, bool] = {}

    def via_ok(i: int, j: int) -> bool:
        k = i * H + j
        r = via_cache.get(k)
        if r is not None:
            return r
        ok = True
        rr = via_clear_cells * via_clear_cells
        for c in cells:
            for dj in range(-via_clear_cells, via_clear_cells + 1):
                for di in range(-via_clear_cells, via_clear_cells + 1):
                    if di * di + dj * dj > rr:
                        continue
                    ii, jj = i + di, j + dj
                    if ii < 0 or jj < 0 or ii >= W or jj >= H or c[jj * W + ii]:
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                break
        via_cache[k] = ok
        return ok

    sli, gli = layer_index[sl], layer_index[gl]
    start_key = key(si, sj, sli, NO_HEADING, MIN_STRAIGHT)
    best: dict[int, float] = {start_key: 0.0}
    came: dict[int, int] = {}
    pq = [(heur(si, sj, sli), 0.0, start_key)]
    seen = 0
    while pq:
        _, cost, cur = heapq.heappop(pq)
        if cost > best.get(cur, float("inf")):
            continue
        i, j, li, h, run = unkey(cur)
        if i == gi and j == gj and li == gli and goal_ok[h]:
            path = [(i, j, layers[li])]
            k = cur
            while k in came:
                k = came[k]
                ii, jj, ll, _, _ = unkey(k)
                if (ii, jj, layers[ll]) != path[-1]:
                    path.append((ii, jj, layers[ll]))
            return path[::-1]
        seen += 1
        if seen > max_nodes:
            return None
        c = cells[li]
        for nh in allowed[h]:
            if nh != h and h != NO_HEADING and run < MIN_STRAIGHT:
                continue  # too soon after the last turn
            di, dj = HEADINGS[nh]
            ni, nj = i + di, j + dj
            if ni < 0 or nj < 0 or ni >= W or nj >= H or c[nj * W + ni]:
                continue
            if di and dj and c[j * W + ni] and c[nj * W + i]:
                continue  # no squeezing diagonally between two blocked cells
            nrun = min(MIN_STRAIGHT, run + 1) if nh == h else 0
            nk = key(ni, nj, li, nh, nrun)
            nc = cost + step_cost[nh] + (0.0 if nh == h or h == NO_HEADING else TURN_COST)
            if nc < best.get(nk, float("inf")):
                best[nk] = nc
                came[nk] = cur
                heapq.heappush(pq, (nc + heur(ni, nj, li), nc, nk))
        if nl > 1 and h != NO_HEADING and max(abs(i - si), abs(j - sj)) >= no_via_cells and max(abs(i - gi), abs(j - gj)) >= no_via_cells and via_ok(i, j):
            for oli in range(nl):
                if oli == li:
                    continue
                nk = key(i, j, oli, h, run)
                nc = cost + via_cost
                if nc < best.get(nk, float("inf")):
                    best[nk] = nc
                    came[nk] = cur
                    heapq.heappush(pq, (nc + heur(i, j, oli), nc, nk))
    return None


def astar_single(g: Grid, start: tuple[int, int, str], goal: tuple[int, int, str], *, via_cost: float = 25.0, via_clear_cells: int = 4,
                 max_nodes: int = 3_000_000, no_via_cells: int = 2, turn_cost: float = 0.15) -> list[tuple[int, int, str]] | None:
    """Shortest 8-connected path for a single track: the state is the cell and the layer only, so a route
    across a whole board stays within a few hundred thousand states where ``astar``'s heading state runs
    into millions. Turns cost ``turn_cost`` cells, judged against the direction the cell was reached from,
    which keeps runs straight without widening the state; a layer change costs ``via_cost`` cells and needs a
    clear disk of ``via_clear_cells`` on every layer, at least ``no_via_cells`` from either end."""
    (si, sj, sl), (gi, gj, gl) = start, goal
    layers = g.layers
    nl = len(layers)
    layer_index = {l: k for k, l in enumerate(layers)}
    W, H = g.w, g.h
    cells = [g.cells[l] for l in layers]

    def key(i: int, j: int, li: int) -> int:
        return (i * H + j) * nl + li

    def unkey(k: int) -> tuple[int, int, int]:
        li = k % nl
        k //= nl
        return k // H, k % H, li

    def heur(i: int, j: int, li: int) -> float:
        dx, dy = abs(i - gi), abs(j - gj)
        return max(dx, dy) + (SQRT2 - 1) * min(dx, dy) + (0.0 if layers[li] == gl else via_cost)

    via_cache: dict[int, bool] = {}
    rr = via_clear_cells * via_clear_cells
    mask = g.via_mask

    def via_ok(i: int, j: int) -> bool:
        if mask is not None:
            return mask[j * W + i] == 0 and all(c[j * W + i] == 0 for c in cells)
        k = i * H + j
        r = via_cache.get(k)
        if r is not None:
            return r
        ok = True
        for c in cells:
            for dj in range(-via_clear_cells, via_clear_cells + 1):
                for di in range(-via_clear_cells, via_clear_cells + 1):
                    if di * di + dj * dj > rr:
                        continue
                    ii, jj = i + di, j + dj
                    if ii < 0 or jj < 0 or ii >= W or jj >= H or c[jj * W + ii]:
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                break
        via_cache[k] = ok
        return ok

    sli, gli = layer_index[sl], layer_index[gl]
    start_key = key(si, sj, sli)
    goal_key = key(gi, gj, gli)
    best: dict[int, float] = {start_key: 0.0}
    came: dict[int, int] = {}
    pq = [(heur(si, sj, sli), 0.0, start_key)]
    seen = 0
    while pq:
        _, cost, cur = heapq.heappop(pq)
        if cost > best.get(cur, float("inf")):
            continue
        if cur == goal_key:
            path = []
            k = cur
            while True:
                i, j, li = unkey(k)
                if not path or (i, j, layers[li]) != path[-1]:
                    path.append((i, j, layers[li]))
                if k not in came:
                    break
                k = came[k]
            return path[::-1]
        seen += 1
        if seen > max_nodes:
            return None
        i, j, li = unkey(cur)
        pdir = None
        if cur in came:
            pi, pj, pli = unkey(came[cur])
            if pli == li:
                pdir = (i - pi, j - pj)
        c = cells[li]
        for di, dj, step in MOVES:
            ni, nj = i + di, j + dj
            if ni < 0 or nj < 0 or ni >= W or nj >= H or c[nj * W + ni]:
                continue
            if di and dj and c[j * W + ni] and c[nj * W + i]:
                continue  # no squeezing diagonally between two blocked cells
            nk = key(ni, nj, li)
            nc = cost + step + (turn_cost if pdir is not None and pdir != (di, dj) else 0.0)
            if nc < best.get(nk, float("inf")):
                best[nk] = nc
                came[nk] = cur
                heapq.heappush(pq, (nc + heur(ni, nj, li), nc, nk))
        if nl > 1 and max(abs(i - si), abs(j - sj)) >= no_via_cells and max(abs(i - gi), abs(j - gj)) >= no_via_cells and via_ok(i, j):
            for oli in range(nl):
                if oli == li:
                    continue
                nk = key(i, j, oli)
                nc = cost + via_cost
                if nc < best.get(nk, float("inf")):
                    best[nk] = nc
                    came[nk] = cur
                    heapq.heappush(pq, (nc + heur(i, j, oli), nc, nk))
    return None


def simplify(path: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Drop collinear intermediate cells; keep layer-change points."""
    if len(path) < 3:
        return path
    out = [path[0]]
    for a, b, c in zip(path, path[1:], path[2:]):
        if a[2] != b[2] or b[2] != c[2]:
            out.append(b)
            continue
        d1 = (b[0] - a[0], b[1] - a[1])
        d2 = (c[0] - b[0], c[1] - b[1])
        if d1 != d2:
            out.append(b)
    out.append(path[-1])
    return out


# --------------------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------------------


def _offset_polyline(pts: list[tuple[float, float]], d: float) -> list[tuple[float, float]]:
    """Offset a polyline to its left by ``d`` (negative: right), mitring the corners."""
    if len(pts) < 2:
        return pts
    n = len(pts)
    normals = []
    for a, b in zip(pts, pts[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        ln = math.hypot(dx, dy) or 1.0
        normals.append((-dy / ln, dx / ln))
    out = [(pts[0][0] + normals[0][0] * d, pts[0][1] + normals[0][1] * d)]
    for k in range(1, n - 1):
        n1, n2 = normals[k - 1], normals[k]
        bx, by = n1[0] + n2[0], n1[1] + n2[1]
        bl = math.hypot(bx, by)
        if bl < 1e-9:
            out.append((pts[k][0] + n2[0] * d, pts[k][1] + n2[1] * d))
            continue
        cos_half = bl / 2.0
        m = d / max(cos_half, 0.2)
        out.append((pts[k][0] + bx / bl * m, pts[k][1] + by / bl * m))
    out.append((pts[-1][0] + normals[-1][0] * d, pts[-1][1] + normals[-1][1] * d))
    return out


def _length(pts: list[tuple[float, float]]) -> float:
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def _segments(net: str, layer: str, width: float, pts: list[tuple[float, float]]) -> list[RouteSegment]:
    out = []
    for a, b in zip(pts, pts[1:]):
        if math.dist(a, b) > 1e-6:
            out.append(RouteSegment(net, layer, width, round(a[0], 4), round(a[1], 4), round(b[0], 4), round(b[1], 4)))
    return out


def _dogleg(a: tuple[float, float], b: tuple[float, float]) -> list[tuple[float, float]]:
    """a to b with one 45 degree bend: the diagonal first, then the straight remainder."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    if abs(dx) < 1e-6 or abs(dy) < 1e-6:
        return [a, b]
    m = min(abs(dx), abs(dy))
    mid = (a[0] + math.copysign(m, dx), a[1] + math.copysign(m, dy))
    return [a, mid, b]


def _snap_dir(v: tuple[float, float]) -> tuple[int, int]:
    return (int(math.copysign(1, v[0])), 0) if abs(v[0]) >= abs(v[1]) else (0, int(math.copysign(1, v[1])))


def _bump(pts: list[tuple[float, float]], extra: float, side: int, min_run: float = 1.5) -> list[tuple[float, float]] | None:
    """Insert a 45 degree trapezoid bump on the longest straight run to add ``extra`` length.

    The bump has amplitude a and a flat top t; its extra length is 2a(sqrt2 - 1). ``side`` picks the
    side (+1 left of the run's direction, -1 right) so the bump leans away from the partner."""
    if extra <= 0:
        return pts
    a = extra / (2 * (SQRT2 - 1))
    t = 0.4
    k = max(range(len(pts) - 1), key=lambda i: math.dist(pts[i], pts[i + 1]))
    p, q = pts[k], pts[k + 1]
    run = math.dist(p, q)
    if run < 2 * a + t + min_run:
        return None
    ux, uy = (q[0] - p[0]) / run, (q[1] - p[1]) / run
    nx, ny = -uy * side, ux * side
    s0 = (run - (2 * a + t)) / 2
    b1 = (p[0] + ux * s0, p[1] + uy * s0)
    b2 = (b1[0] + ux * a + nx * a, b1[1] + uy * a + ny * a)
    b3 = (b2[0] + ux * t, b2[1] + uy * t)
    b4 = (b3[0] + ux * a - nx * a, b3[1] + uy * a - ny * a)
    return pts[: k + 1] + [b1, b2, b3, b4] + pts[k + 1 :]


def _line_free(grid: Grid, layer: str, a: tuple[float, float], b: tuple[float, float]) -> bool:
    n = max(1, int(math.dist(a, b) / (grid.step / 2)))
    for k in range(n + 1):
        t = k / n
        i, j = grid.idx(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        if grid.blocked(layer, i, j):
            return False
    return True


def _pull(grid: Grid, layer: str, pts: list[tuple[float, float]], prev: tuple[float, float] | None = None, nxt: tuple[float, float] | None = None) -> list[tuple[float, float]]:
    """String pulling: replace runs of grid steps by the longest straight line that stays clear. ``prev`` and
    ``nxt`` are the points the polyline continues from and to, so the turns at its ends stay gentle too."""
    if len(pts) < 3:
        return pts
    def gentle(a, b, c) -> bool:
        ux, uy = b[0] - a[0], b[1] - a[1]
        vx, vy = c[0] - b[0], c[1] - b[1]
        lu, lv = math.hypot(ux, uy), math.hypot(vx, vy)
        if lu < 1e-9 or lv < 1e-9:
            return True
        return (ux * vx + uy * vy) / (lu * lv) >= -0.18  # turn of 100 degrees at most

    out = [pts[0]]
    i = 0
    while i < len(pts) - 1:
        k = len(pts) - 1
        while k > i + 1:
            before = out[-2] if len(out) >= 2 else prev
            ok = _line_free(grid, layer, pts[i], pts[k]) and (before is None or gentle(before, out[-1], pts[k]))
            if ok and k == len(pts) - 1 and nxt is not None:
                ok = gentle(out[-1], pts[k], nxt)
            if ok:
                break
            k -= 1
        out.append(pts[k])
        i = k
    return out


def _merge_short(pts: list[tuple[float, float]], min_len: float, grid: "Grid | None" = None, layer: str | None = None, prev: tuple[float, float] | None = None) -> list[tuple[float, float]]:
    """Drop interior vertices that make segments shorter than ``min_len``; the ends stay. With a grid, a
    vertex is only dropped when the segment that replaces it stays in free space."""
    if len(pts) < 3:
        return pts
    def sharp(a, b, c) -> bool:
        ux, uy = b[0] - a[0], b[1] - a[1]
        vx, vy = c[0] - b[0], c[1] - b[1]
        lu, lv = math.hypot(ux, uy), math.hypot(vx, vy)
        return lu > 1e-9 and lv > 1e-9 and (ux * vx + uy * vy) / (lu * lv) < -0.18

    out = [pts[0]]
    for k, q in enumerate(pts[1:-1], start=1):
        if math.dist(out[-1], q) >= min_len:
            out.append(q)
        elif grid is not None and not _line_free(grid, layer, out[-1], pts[k + 1]):
            out.append(q)
        elif (len(out) >= 2 and sharp(out[-2], out[-1], pts[k + 1])) or (len(out) == 1 and prev is not None and sharp(prev, out[-1], pts[k + 1])):
            out.append(q)  # dropping q would fold the line back on itself
    out.append(pts[-1])
    return out


def _nearest_on_polyline(m: tuple[float, float], pts: list[tuple[float, float]]) -> tuple[float, tuple[float, float]]:
    """Distance from m to the polyline and the closest point on it."""
    best = (float("inf"), pts[0])
    for a, b in zip(pts, pts[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 < 1e-12 else max(0.0, min(1.0, ((m[0] - a[0]) * dx + (m[1] - a[1]) * dy) / l2))
        c = (a[0] + dx * t, a[1] + dy * t)
        d = math.dist(m, c)
        if d < best[0]:
            best = (d, c)
    if len(pts) == 1:
        best = (math.dist(m, pts[0]), pts[0])
    return best


def _polyline_hits_rect(pts: list[tuple[float, float]], rect: tuple[float, float, float, float], margin: float) -> bool:
    """Does the polyline come within ``margin`` of the axis-aligned rectangle?"""
    x0, y0, x1, y1 = rect[0] - margin, rect[1] - margin, rect[2] + margin, rect[3] + margin
    for a, b in zip(pts, pts[1:]):
        n = max(1, int(math.dist(a, b) / 0.05))
        for k in range(n + 1):
            t = k / n
            x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            if x0 <= x <= x1 and y0 <= y <= y1:
                return True
    return False


def _seg_cross(a, b, c, d) -> bool:
    """Proper intersection of segments ab and cd (shared endpoints do not count)."""
    def orient(p, q, r):
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)
    if max(a[0], b[0]) < min(c[0], d[0]) or max(c[0], d[0]) < min(a[0], b[0]) or max(a[1], b[1]) < min(c[1], d[1]) or max(c[1], d[1]) < min(a[1], b[1]):
        return False
    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return o1 != o2 and o3 != o4 and 0 not in (o1, o2, o3, o4)


def _seg_seg_gap(a, b, c, d) -> float:
    """Distance between the centrelines of segments ab and cd (0 when they cross)."""
    if _seg_cross(a, b, c, d):
        return 0.0
    return min(_nearest_on_polyline(a, [c, d])[0], _nearest_on_polyline(b, [c, d])[0], _nearest_on_polyline(c, [a, b])[0], _nearest_on_polyline(d, [a, b])[0])


def _crossings(p_route, n_route) -> list[tuple[float, float]]:
    """Where the P and N polylines cross on the same layer (midpoints of the crossing P segments)."""
    where = []
    for lp, pts_p in p_route:
        for ln, pts_n in n_route:
            if lp != ln:
                continue
            for a, b in zip(pts_p, pts_p[1:]):
                for c, d in zip(pts_n, pts_n[1:]):
                    if _seg_cross(a, b, c, d):
                        where.append(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
    return where


# --------------------------------------------------------------------------------------
# the router
# --------------------------------------------------------------------------------------


def pair_specs(bm: BoardModel, project: Path | None, only: list[str] | None = None) -> list[PairSpec]:
    classes, assignments = load_netclasses(project)
    nets = sorted({p.net for f in bm.footprints for p in f.pads if p.net})
    pairs, _ = find_pairs(nets)
    specs = []
    for name, p, n in pairs:
        if only and name not in only:
            continue
        cls_name, c = netclass_for(p, classes, assignments)
        rule = rule_for(name)
        if rule is None and not only:
            continue  # power taps that happen to be named _P/_N are not signal pairs
        specs.append(PairSpec(
            name=name, p_net=p, n_net=n,
            width=float(c.get("diff_pair_width") or c.get("track_width") or 0.15),
            gap=float(c.get("diff_pair_gap") or 0.15),
            clearance=float(c.get("clearance") or 0.125),
            via_size=float(c.get("via_diameter") or 0.6), via_drill=float(c.get("via_drill") or 0.3), via_gap=float(c.get("diff_pair_via_gap") or 0.25),
            skew_limit=rule[1] if rule else None, target_ohm=rule[0] if rule else None,
        ))
    return specs


def _pads_of(bm: BoardModel, net: str) -> list[tuple[PadGeo, tuple[float, float]]]:
    """(pad, footprint centre) for every pad on the net."""
    out = []
    for f in bm.footprints:
        for p in f.pads:
            if p.net == net:
                out.append((p, (f.x, f.y)))
    return out


def _free_run(grid: Grid, layer: str, x: float, y: float, d: tuple[int, int], limit: float = 6.0) -> float:
    """How far a ray from (x, y) in direction d stays clear on the grid, up to ``limit`` mm."""
    run = 0.0
    while run < limit:
        run += grid.step
        i, j = grid.idx(x + d[0] * run, y + d[1] * run)
        if grid.blocked(layer, i, j):
            return run - grid.step
    return limit


def _escape_plan(bm: BoardModel, spec: PairSpec, grid: Grid, layer: str, keep_narrow_first: bool = True):
    """Ends, escape directions, stub ends and centreline start points for one pair, or None with a reason."""
    p_pads = _pads_of(bm, spec.p_net)
    n_pads = _pads_of(bm, spec.n_net)
    if len(p_pads) < 2 or len(n_pads) < 2:
        return None, "a half has fewer than two pads"
    ends = []
    used_n: set[int] = set()
    all_pair_pads = [q for q, _ in p_pads] + [q for q, _ in n_pads]

    def between(a: PadGeo, b: PadGeo) -> int:
        """Pads of the pair whose centre lies on the segment ab (doubled connector pads interleave)."""
        n = 0
        for q in all_pair_pads:
            if q is a or q is b:
                continue
            d_, c = _nearest_on_polyline((q.x, q.y), [(a.x, a.y), (b.x, b.y)])
            if d_ < 0.05 and 1e-6 < math.dist(c, (a.x, a.y)) < math.dist((a.x, a.y), (b.x, b.y)) - 1e-6:
                n += 1
        return n

    def outside_min(a: PadGeo, b: PadGeo) -> int:
        """Pair pads beyond either end of segment ab along its direction: the smaller count (0 at a row's end)."""
        ux, uy = b.x - a.x, b.y - a.y
        ln = math.hypot(ux, uy) or 1.0
        ux, uy = ux / ln, uy / ln
        before = after = 0
        for q in all_pair_pads:
            if q is a or q is b:
                continue
            t = (q.x - a.x) * ux + (q.y - a.y) * uy
            off = abs(-(q.x - a.x) * uy + (q.y - a.y) * ux)
            if off > 0.3:
                continue
            if t < 0:
                before += 1
            elif t > ln:
                after += 1
        return min(before, after)

    for pp, pc in p_pads:
        k = min((i for i in range(len(n_pads)) if i not in used_n), key=lambda i: (round(math.dist((pp.x, pp.y), (n_pads[i][0].x, n_pads[i][0].y)), 3), between(pp, n_pads[i][0]), outside_min(pp, n_pads[i][0])), default=None)
        if k is None:
            break
        used_n.add(k)
        ends.append(((pp, pc), n_pads[k]))
    if len(ends) < 2:
        return None, "could not pair the pads at both ends"
    end_a, end_b = max(((a, b) for i, a in enumerate(ends) for b in ends[i + 1:]), key=lambda ab: math.dist((ab[0][0][0].x, ab[0][0][0].y), (ab[1][0][0].x, ab[1][0][0].y)))
    extras = [e for e in ends if e is not end_a and e is not end_b]
    pitch_a = math.dist((end_a[0][0].x, end_a[0][0].y), (end_a[1][0].x, end_a[1][0].y))
    pitch_b = math.dist((end_b[0][0].x, end_b[0][0].y), (end_b[1][0].x, end_b[1][0].y))
    if keep_narrow_first and pitch_b < pitch_a:
        end_a, end_b = end_b, end_a
    grow = spec.half_extent + spec.clearance

    def corridor_ok(pp: PadGeo, np_: PadGeo, d: tuple[int, int]) -> tuple[bool, float]:
        """Can both stubs run straight out in direction d past the footprint's other pads? Returns (ok, reach)."""
        f = next(f for f in bm.footprints if f.ref == pp.ref)
        reach = 0.0
        for q in f.pads:
            if q is pp or q is np_ or q.number == "" and q.kind == "np_thru_hole":
                continue
            r = _pad_rect(q)
            for stub in (pp, np_):
                if d[0]:
                    ahead = (r[0] - stub.x) * d[0] > -1e-6 or (r[2] - stub.x) * d[0] > -1e-6
                    lateral = 0.0 if r[1] <= stub.y <= r[3] else min(abs(stub.y - r[1]), abs(stub.y - r[3]))
                    edge = r[2] if d[0] > 0 else r[0]
                    along = (edge - stub.x) * d[0]
                else:
                    ahead = (r[1] - stub.y) * d[1] > -1e-6 or (r[3] - stub.y) * d[1] > -1e-6
                    lateral = 0.0 if r[0] <= stub.x <= r[2] else min(abs(stub.x - r[0]), abs(stub.x - r[2]))
                    edge = r[3] if d[1] > 0 else r[1]
                    along = (edge - stub.y) * d[1]
                if not ahead or along <= 0:
                    continue
                if lateral < spec.width / 2 + spec.clearance:
                    if lateral < 1e-6 and (q.net == stub.net):
                        continue
                    return False, 0.0  # the stub would run into or too close to this pad
                if lateral < spec.width / 2 + spec.clearance + 0.3:
                    reach = max(reach, along)  # squeezing past: the stub must clear the pad before converging
        return True, reach

    def escape_group(end, other):
        (pp, pc), (np_, nc) = end
        goal = (other[0][0].x, other[0][0].y)
        w, h = pp.size
        if abs((pp.angle % 180) - 90) < 1e-6:
            w, h = h, w
        if abs(w - h) < 0.05:
            cands = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        elif w > h:
            cands = [(1, 0), (-1, 0)]
        else:
            cands = [(0, 1), (0, -1)]
        away = (pp.x - pc[0], pp.y - pc[1])
        toward = (goal[0] - pp.x, goal[1] - pp.y)
        scored = []
        for d in cands:
            ok, reach = corridor_ok(pp, np_, d)
            if not ok:
                continue
            off = (w / 2 if d[0] else h / 2) + max(0.6, reach + 0.2)
            room = min(_free_run(grid, layer, pp.x + d[0] * off, pp.y + d[1] * off, d, limit=12.0), 3.0)
            tw = d[0] * toward[0] + d[1] * toward[1]
            scored.append(((room >= 1.5, tw > 0, round(room, 1), tw, d[0] * away[0] + d[1] * away[1]), d, reach))
        if not scored:
            return None
        scored.sort(reverse=True)
        _, d, reach = scored[0]
        length = reach + grow + 0.6
        p_end = (pp.x + d[0] * length, pp.y + d[1] * length)
        n_end = (np_.x + d[0] * length, np_.y + d[1] * length)
        mid = ((p_end[0] + n_end[0]) / 2, (p_end[1] + n_end[1]) / 2)
        start = (mid[0] + d[0] * 1.6, mid[1] + d[1] * 1.6)
        return pp, np_, p_end, n_end, start, d, [sc for sc in scored]

    ga = escape_group(end_a, end_b)
    gb = escape_group(end_b, end_a)
    if ga is None or gb is None:
        return None, "no escape direction clears the footprint's other pads"

    def handed(p_end, n_end, d):
        n = (-d[1], d[0])
        return 1.0 if (p_end[0] - n_end[0]) * n[0] + (p_end[1] - n_end[1]) * n[1] >= 0 else -1.0

    def mirrored(ga_, gb_):
        return handed(ga_[2], ga_[3], ga_[5]) != handed(gb_[2], gb_[3], (-gb_[5][0], -gb_[5][1]))

    if extras and mirrored(ga, gb):
        # a connector with doubled pads offers other P/N pad choices at that end; one of them may keep the sides
        for which, end in (("a", end_a), ("b", end_b)):
            (pp, pc), (np_, nc) = end
            same_ref = [e for e in extras if e[0][0].ref == pp.ref]
            for (ep, ec), (en, enc) in same_ref:
                for alt in ((((ep, ec), (np_, nc)), ((pp, pc), (en, enc))), (((ep, ec), (en, enc)),)):
                    for alt_end in alt:
                        g_alt = escape_group(alt_end, end_b if which == "a" else end_a)
                        if g_alt is None:
                            continue
                        ga2, gb2 = (g_alt, gb) if which == "a" else (ga, g_alt)
                        if not mirrored(ga2, gb2):
                            used = {id(alt_end[0][0]), id(alt_end[1][0])}
                            new_extras = [e for e in ends if id(e[0][0]) not in used and id(e[1][0]) not in used and e is not (end_b if which == "a" else end_a)]
                            # rebuild extras from the pads not used by either end
                            end_other = end_b if which == "a" else end_a
                            used |= {id(end_other[0][0]), id(end_other[1][0])}
                            ex_p = [(q, c) for q, c in p_pads if id(q) not in used]
                            ex_n = [(q, c) for q, c in n_pads if id(q) not in used]
                            new_extras = list(zip(ex_p, ex_n))
                            if which == "a":
                                return {"p_pads": p_pads, "n_pads": n_pads, "extras": new_extras, "a": ga2, "b": gb2, "end_a": alt_end, "end_b": end_b, "grow": grow}, ""
                            return {"p_pads": p_pads, "n_pads": n_pads, "extras": new_extras, "a": ga2, "b": gb2, "end_a": end_a, "end_b": alt_end, "grow": grow}, ""
    return {"p_pads": p_pads, "n_pads": n_pads, "extras": extras, "a": ga, "b": gb, "end_a": end_a, "end_b": end_b, "grow": grow}, ""


def route_pairs(board: Path, project: Path | None = None, *, existing: Routes | None = None, only: list[str] | None = None,
                layers: tuple[str, ...] = ("F.Cu", "B.Cu"), step: float = 0.2, keepouts: list[tuple[float, float, float, float]] = (),
                order: list[str] | None = None, exclude: list[str] | None = None) -> tuple[Routes, list[PairResult]]:
    bm = load_board(board)
    if project is None:
        cand = board.with_suffix(".kicad_pro")
        project = cand if cand.is_file() else None
    existing = existing or Routes()
    specs = pair_specs(bm, project, only)
    if exclude:
        specs = [sp for sp in specs if sp.name not in exclude]

    def span(spec: PairSpec) -> float:
        pads = [(p.x, p.y) for p, _ in _pads_of(bm, spec.p_net)]
        return max((math.dist(a, b) for a in pads for b in pads), default=0.0)

    specs.sort(key=span)  # short, constrained pairs first; long ones have more ways round
    layer_list = list(layers)
    margin = step / 2  # grid sampling error
    # ---- phase 1: plan every escape on a copper-free grid and reserve the corridors as obstacles for the others
    plans: dict[str, dict] = {}
    results: list[PairResult] = []
    base_grow = max((sp.half_extent + sp.clearance for sp in specs), default=0.4) + margin
    base_grid = build_grid(bm, existing, layer_list, step, base_grow, skip_nets=set(), keepouts=list(keepouts), clearance=0.125)
    reserved = Routes()  # stubs of every pair (real copper) plus fat axis segments (obstacles only), by net
    funnels: dict[str, list[RouteSegment]] = {}  # each pair's own stubs and convergence trapezoid, obstacles to its own centreline
    for spec in specs:
        plan, why = _escape_plan(bm, spec, base_grid, layer_list[0])
        if plan is None:
            r = PairResult(name=spec.name, status="skipped")
            r.notes.append(why)
            results.append(r)
            continue
        plans[spec.name] = plan
        for group in (plan["a"], plan["b"]):
            pp, np_, p_end, n_end, start, d, _ = group
            axis_net = f"__axis__{spec.name}"
            own = funnels.setdefault(spec.name, [])
            for pad, end, net in ((pp, p_end, spec.p_net), (np_, n_end, spec.n_net)):
                reserved.segments.append(RouteSegment(net, "*", spec.width, pad.x, pad.y, end[0], end[1]))
                own.append(RouteSegment(f"__funnel__{spec.name}", "*", spec.width, pad.x, pad.y, end[0], end[1]))
            mid = ((p_end[0] + n_end[0]) / 2, (p_end[1] + n_end[1]) / 2)
            stub_pitch = math.dist(p_end, n_end)
            wide = max(0.4, (stub_pitch - spec.pitch) / 2 + 0.3)
            knee = (mid[0] + d[0] * wide, mid[1] + d[1] * wide)
            reserved.segments.append(RouteSegment(axis_net, "*", stub_pitch + spec.width, mid[0], mid[1], knee[0], knee[1]))
            own.append(RouteSegment(f"__funnel__{spec.name}", "*", stub_pitch + spec.width, mid[0], mid[1], knee[0], knee[1]))
            reserved.segments.append(RouteSegment(axis_net, "*", spec.pitch + spec.width, knee[0], knee[1], start[0] + d[0] * 1.2, start[1] + d[1] * 1.2))
            reserved.nets |= {spec.p_net, spec.n_net, axis_net}
    def handed(p_end, n_end, d):
        n = (-d[1], d[0])
        return 1.0 if (p_end[0] - n_end[0]) * n[0] + (p_end[1] - n_end[1]) * n[1] >= 0 else -1.0

    def mirrored_plan(spec: PairSpec) -> bool:
        plan = plans.get(spec.name)
        if plan is None:
            return False
        _, _, pa_e, na_e, _, da, _ = plan["a"]
        _, _, pb_e, nb_e, _, db, _ = plan["b"]
        return handed(pa_e, na_e, da) != handed(pb_e, nb_e, (-db[0], -db[1]))

    # pairs that will need a crossover go first so the crossover finds room; then short pairs before long ones
    specs.sort(key=lambda sp: (not mirrored_plan(sp), span(sp)))
    if order:
        rank = {n: i for i, n in enumerate(order)}
        specs.sort(key=lambda s: rank.get(s.name, len(rank)))
    done = Routes(segments=list(existing.segments), vias=list(existing.vias), nets=set(existing.nets))
    # ---- phase 2: route
    for spec in specs:
        if spec.name not in plans:
            continue
        plan = plans[spec.name]
        res = PairResult(name=spec.name, status="failed")
        results.append(res)
        p_pads, n_pads, extras = plan["p_pads"], plan["n_pads"], plan["extras"]
        grow = plan["grow"] + margin
        others = Routes()
        others.segments = [sg for sg in reserved.segments if sg.net not in (spec.p_net, spec.n_net, f"__axis__{spec.name}")]
        others.nets = {sg.net for sg in others.segments}
        obstacles = Routes(segments=done.segments + [RouteSegment(sg.net, l, sg.width, sg.x1, sg.y1, sg.x2, sg.y2) for sg in others.segments for l in layer_list], vias=done.vias, nets=done.nets | others.nets)
        own_funnel = funnels.get(spec.name, [])
        obstacles.segments += [RouteSegment(sg.net, l, sg.width, sg.x1, sg.y1, sg.x2, sg.y2) for sg in own_funnel for l in layer_list]
        obstacles.nets |= {sg.net for sg in own_funnel}
        grid = build_grid(bm, obstacles, layer_list, step, grow, skip_nets={spec.p_net, spec.n_net}, keepouts=list(keepouts), clearance=spec.clearance)
        pa, na, pa_end, na_end, start_a, dir_a, _ = plan["a"]
        pb, nb, pb_end, nb_end, start_b, dir_b, scored_b = plan["b"]

        def handedness(p_end, n_end, d):
            n = (-d[1], d[0])
            return 1.0 if (p_end[0] - n_end[0]) * n[0] + (p_end[1] - n_end[1]) * n[1] >= 0 else -1.0

        # approach points: the search runs between points a little further out so the first and last
        # centreline segments are straight along the escape directions; handedness is then well defined
        lead = 1.2
        ap_a = (start_a[0] + dir_a[0] * lead, start_a[1] + dir_a[1] * lead)
        ap_b = (start_b[0] + dir_b[0] * lead, start_b[1] + dir_b[1] * lead)
        via_pitch = spec.via_size + spec.via_gap
        for l in layer_list:
            for pt in (ap_a, ap_b, start_a, start_b):
                grid.clear_disk(l, pt[0], pt[1], step * 1.01)
        si, sj = grid.idx(*ap_a)
        gi, gj = grid.idx(*ap_b)

        def pad_layer(pad: PadGeo, route_layer: str) -> str:
            if pad.kind != "smd" or route_layer in pad.layers or "*.Cu" in pad.layers or "F&B.Cu" in pad.layers:
                return route_layer
            return next((l for l in layer_list if l in pad.layers), layer_list[0])

        def pad_layers(pad: PadGeo) -> list[str]:
            if pad.kind != "smd" or "*.Cu" in pad.layers or "F&B.Cu" in pad.layers:
                return list(layer_list)
            return [l for l in layer_list if l in pad.layers] or [layer_list[0]]

        # the route leaves and arrives on the pad layers so no via pair is needed at the escapes
        def via_pair_fits(c, d):
            """Both vias of a pair centred at c across direction d clear every foreign pad, track and via."""
            n = (-d[1], d[0])
            need_v = spec.via_size / 2 + spec.clearance
            for sgn in (1, -1):
                vx, vy = c[0] + n[0] * via_pitch / 2 * sgn, c[1] + n[1] * via_pitch / 2 * sgn
                for sg in obstacles.segments:
                    if sg.net in (spec.p_net, spec.n_net) or sg.net.startswith("__funnel__"):
                        continue
                    if _nearest_on_polyline((vx, vy), [(sg.x1, sg.y1), (sg.x2, sg.y2)])[0] < need_v + sg.width / 2:
                        return False
                for v in obstacles.vias:
                    if v.net not in (spec.p_net, spec.n_net) and math.dist((vx, vy), (v.x, v.y)) < need_v + v.size / 2:
                        return False
                for f_ in bm.footprints:
                    for q in f_.pads:
                        if q.net in (spec.p_net, spec.n_net):
                            continue
                        r = _pad_rect(q)
                        dx = 0.0 if r[0] <= vx <= r[2] else min(abs(vx - r[0]), abs(vx - r[2]))
                        dy = 0.0 if r[1] <= vy <= r[3] else min(abs(vy - r[1]), abs(vy - r[3]))
                        if math.hypot(dx, dy) < need_v:
                            return False
            return True

        def lead_slot(start, d):
            """Distance along the lead where a via pair fits, staggered so neighbours pick different slots."""
            for t in (0.45, 1.2, 0.6, 1.05, 0.75, 0.9):
                if t > lead - 0.05:
                    continue
                if via_pair_fits((start[0] + d[0] * t, start[1] + d[1] * t), d):
                    return t
            return None

        combos = [(pad_layers(pa)[0], pad_layers(pb)[0])]  # pad layers first: no via pair on the leads
        combos += [(l_, l_) for l_ in layer_list if (l_, l_) not in combos]  # then the other layer at both ends: two predictable lead via pairs
        combos += [(la_, lb_) for la_ in layer_list for lb_ in layer_list if (la_, lb_) not in combos]  # mixed last
        path = None
        for la_, lb_ in combos:
            # a lead that must change layers needs a slot for its via pair; without one the combo is pointless
            if (la_ != pad_layers(pa)[0] and lead_slot(start_a, dir_a) is None) or (lb_ != pad_layers(pb)[0] and lead_slot(start_b, dir_b) is None):
                continue
            path = astar(grid, (si, sj, la_), (gi, gj, lb_), via_cost=25.0, via_clear_cells=int(math.ceil((via_pitch / 2 + spec.via_size / 2 + spec.clearance) / step)), start_dir=dir_a, goal_dir=(-dir_b[0], -dir_b[1]))
            if path:
                break
        if not path and step > 0.1:
            fine = build_grid(bm, obstacles, layer_list, 0.1, grow, skip_nets={spec.p_net, spec.n_net}, keepouts=list(keepouts), clearance=spec.clearance)
            for l in layer_list:
                for pt in (ap_a, ap_b, start_a, start_b):
                    fine.clear_disk(l, pt[0], pt[1], 0.101)
            si, sj = fine.idx(*ap_a)
            gi, gj = fine.idx(*ap_b)
            for la_, lb_ in combos:
                path = astar(fine, (si, sj, la_), (gi, gj, lb_), via_cost=50.0, via_clear_cells=int(math.ceil((via_pitch / 2 + spec.via_size / 2 + spec.clearance) / 0.1)), max_nodes=1_500_000, start_dir=dir_a, goal_dir=(-dir_b[0], -dir_b[1]))
                if path:
                    grid = fine
                    res.notes.append("routed on the fine 0.1 mm grid")
                    break
        if not path:
            res.notes.append("no path found on the routable layers")
            continue
        path = simplify(path)
        # centreline in mm, split at layer changes; string-pulled and freed of tiny segments per piece
        pieces: list[tuple[str, list[tuple[float, float]]]] = []
        cur_layer = path[0][2]
        cur_pts: list[tuple[float, float]] = [ap_a]
        for (i, j, l) in path[1:]:
            xy = grid.xy(i, j)
            if l != cur_layer:
                pieces.append((cur_layer, cur_pts))
                cur_layer = l
                cur_pts = [cur_pts[-1]]
                continue
            cur_pts.append(xy)
        cur_pts[-1] = ap_b
        pieces.append((cur_layer, cur_pts))
        pulled = []
        for k, (l, pts) in enumerate(pieces):
            prev = start_a if k == 0 else (pulled[-1][1][-2] if len(pulled[-1][1]) >= 2 else None)
            nxt = start_b if k == len(pieces) - 1 else (pieces[k + 1][1][1] if len(pieces[k + 1][1]) >= 2 else None)
            pulled.append((l, _merge_short(_pull(grid, l, pts, prev, nxt), 0.6, grid, l, prev)))
        for k, (l, pts) in enumerate(pulled):
            # a stub of a segment right before the piece end (the approach point) is a mitre trap: shortcut it
            while len(pts) >= 3 and math.dist(pts[-2], pts[-1]) < 0.6 and _line_free(grid, l, pts[-3], pts[-1]):
                pts = pts[:-2] + [pts[-1]]
            pulled[k] = (l, pts)
        pieces = pulled
        for k in range(len(pieces) - 1):
            l_in, pts_in = pieces[k]
            l_out, pts_out = pieces[k + 1]
            if len(pts_in) < 2:
                continue
            a, c = pts_in[-2], pts_in[-1]
            seg = math.dist(a, c)
            back = min(0.8, seg * 0.5)
            u = ((c[0] - a[0]) / seg, (c[1] - a[1]) / seg)
            c2 = (c[0] - u[0] * back, c[1] - u[1] * back)
            if _line_free(grid, l_out, c2, c):
                pieces[k] = (l_in, pts_in[:-1] + [c2])
                pieces[k + 1] = (l_out, [c2, c] + pts_out[1:])
                continue
            if len(pts_out) >= 2:  # forward instead: the via sits on the outgoing straight run
                d0 = pts_out[1]
                seg2 = math.dist(c, d0)
                fwd = min(0.8, seg2 * 0.5)
                u2 = ((d0[0] - c[0]) / seg2, (d0[1] - c[1]) / seg2)
                c3 = (c[0] + u2[0] * fwd, c[1] + u2[1] * fwd)
                if _line_free(grid, l_in, c, c3):
                    pieces[k] = (l_in, pts_in + [c3])
                    pieces[k + 1] = (l_out, [c3] + pts_out[1:])
        pieces[0] = (pieces[0][0], [start_a] + pieces[0][1])
        pieces[-1] = (pieces[-1][0], pieces[-1][1] + [start_b])
        # a pad on another layer than the route: change layers on the straight lead, 0.6 mm from the start
        # point, through the same via-pair splay every mid-route layer change uses
        la0, lb0 = pad_layer(pa, pieces[0][0]), pad_layer(pb, pieces[-1][0])

        lead_failed = False
        if la0 != pieces[0][0]:
            t = lead_slot(start_a, dir_a)
            if t is None:
                res.notes.append(f"{pa.ref}-{pa.number} is on {la0} and the route on {pieces[0][0]}, but no via pair fits on the lead")
                lead_failed = True
            else:
                xa = (start_a[0] + dir_a[0] * t, start_a[1] + dir_a[1] * t)
                first_layer0, pts0 = pieces[0]
                rest = pts0[1:] if math.dist(pts0[1], xa) > 0.05 else pts0[2:]
                pieces = [(la0, [start_a, xa]), (first_layer0, [xa] + rest)] + pieces[1:]
                res.notes.append(f"{pa.ref}-{pa.number} is on {la0}, the route runs on {first_layer0}: via pair on the lead at {t:.2f} mm")
        if lb0 != pieces[-1][0]:
            t = lead_slot(start_b, dir_b)
            if t is None:
                res.notes.append(f"{pb.ref}-{pb.number} is on {lb0} and the route on {pieces[-1][0]}, but no via pair fits on the lead")
                lead_failed = True
            else:
                xb = (start_b[0] + dir_b[0] * t, start_b[1] + dir_b[1] * t)
                last_layer0, pts1 = pieces[-1]
                head = pts1[:-1] if math.dist(pts1[-2], xb) > 0.05 else pts1[:-2]
                pieces = pieces[:-1] + [(last_layer0, head + [xb]), (lb0, [xb, start_b])]
                res.notes.append(f"{pb.ref}-{pb.number} is on {lb0}, the route runs on {last_layer0}: via pair on the lead at {t:.2f} mm")
        if lead_failed:
            continue
        half = spec.pitch / 2

        def normal(u):
            ln = math.hypot(*u) or 1.0
            return (-u[1] / ln, u[0] / ln)

        def side(v, n):
            return 1.0 if v[0] * n[0] + v[1] * n[1] >= 0 else -1.0

        # handedness at the start: which side of the travel direction is P on
        n_a = normal(dir_a)
        mid_a = ((pa_end[0] + na_end[0]) / 2, (pa_end[1] + na_end[1]) / 2)
        s_p = side((pa_end[0] - mid_a[0], pa_end[1] - mid_a[1]), n_a)
        # at the far end the travel direction is -dir_b; P arrives on side s_p of that
        n_b_travel = normal((-dir_b[0], -dir_b[1]))
        mid_b = ((pb_end[0] + nb_end[0]) / 2, (pb_end[1] + nb_end[1]) / 2)
        s_pb = side((pb_end[0] - mid_b[0], pb_end[1] - mid_b[1]), n_b_travel)
        mirrored = s_pb != s_p
        # P must arrive on the side it left on. When the far end mirrors it, P changes sides mid-route: two
        # vias on P, its other-layer segment crossing under N, on the longest straight run with room for it.
        xo_a, xo_b = 1.0, 0.7
        if mirrored:
            best = None
            for k, (layer, pts) in enumerate(pieces):
                other = next((l for l in layer_list if l != layer), None)
                if other is None:
                    continue
                for i in range(len(pts) - 1):
                    pq0, pq1 = pts[i], pts[i + 1]
                    seg = math.dist(pq0, pq1)
                    if seg < 2 * xo_a + 0.8 or (best is not None and seg <= best[0]):
                        continue
                    u = ((pq1[0] - pq0[0]) / seg, (pq1[1] - pq0[1]) / seg)
                    n = normal(u)
                    for t in (0.5, 0.4, 0.6, 0.3, 0.7, 0.2, 0.8):
                        if min(seg * t, seg * (1 - t)) < xo_a + 0.4:
                            continue
                        cm = (pq0[0] + u[0] * seg * t, pq0[1] + u[1] * seg * t)
                        v1 = (cm[0] - u[0] * xo_b + n[0] * (via_pitch / 2 + 0.08) * s_p, cm[1] - u[1] * xo_b + n[1] * (via_pitch / 2 + 0.08) * s_p)
                        v2 = (cm[0] + u[0] * xo_b - n[0] * (via_pitch / 2 + 0.08) * s_p, cm[1] + u[1] * xo_b - n[1] * (via_pitch / 2 + 0.08) * s_p)
                        if all(not grid.blocked(l, *grid.idx(*v)) for v in (v1, v2) for l in (layer, other)) and _line_free(grid, other, v1, v2):
                            best = (seg, k, i, cm, u, other)
                            break
            if best is None:
                res.notes.append("P and N arrive mirrored and no straight run has room for a crossover")
                continue
            seg, k, i, cm, u, other = best
            c0 = (cm[0] - u[0] * xo_a, cm[1] - u[1] * xo_a)
            c1 = (cm[0] + u[0] * xo_a, cm[1] + u[1] * xo_a)
            layer, pts = pieces[k]
            pieces = pieces[:k] + [(layer, pts[: i + 1] + [c0]), ("X", [c0, c1]), (layer, [c1] + pts[i + 1 :])] + pieces[k + 1 :]
            xover_other = other
            res.notes.append(f"P and N arrive mirrored: crossover with two vias on P at ({cm[0]:.1f}, {cm[1]:.1f})")

        # offset every piece; splay the halves apart for the via pair at each layer change; swap sides at a crossover
        p_lines: list[tuple[str, list[tuple[float, float]]]] = []
        n_lines: list[tuple[str, list[tuple[float, float]]]] = []
        vias: list[RouteVia] = []
        in_dir, in_normal = (float(dir_a[0]), float(dir_a[1])), normal(dir_a)
        s_cur = s_p
        prev_kind = None
        no_bump: set[int] = set()
        last_vias = None
        for k, (layer, pts) in enumerate(pieces):
            if layer == "X":
                c0, c1 = pts
                ln = math.dist(c0, c1)
                u = ((c1[0] - c0[0]) / ln, (c1[1] - c0[1]) / ln)
                n = normal(u)
                L, O = pieces[k - 1][0], xover_other
                cm = ((c0[0] + c1[0]) / 2, (c0[1] + c1[1]) / 2)
                p_in = (c0[0] + n[0] * half * s_cur, c0[1] + n[1] * half * s_cur)
                n_in = (c0[0] - n[0] * half * s_cur, c0[1] - n[1] * half * s_cur)
                p_out = (c1[0] - n[0] * half * s_cur, c1[1] - n[1] * half * s_cur)
                n_out = (c1[0] + n[0] * half * s_cur, c1[1] + n[1] * half * s_cur)
                vh = via_pitch / 2 + 0.08  # P's via must clear N's diagonal by the class clearance
                v1 = (cm[0] - u[0] * xo_b + n[0] * vh * s_cur, cm[1] - u[1] * xo_b + n[1] * vh * s_cur)
                v2 = (cm[0] + u[0] * xo_b - n[0] * vh * s_cur, cm[1] + u[1] * xo_b - n[1] * vh * s_cur)
                delta = 0.12  # N stays on its line this far past c0 and before c1: its diagonal then starts after P has moved outward
                n_mid = [(n_in[0] + u[0] * delta, n_in[1] + u[1] * delta), (n_out[0] - u[0] * delta, n_out[1] - u[1] * delta)]
                xo_pieces = [(L, [p_in, v1]), (O, [v1, v2]), (L, [v2, p_out]), (L, [n_in, *n_mid, n_out])]
                no_bump.update(id(pts_) for _, pts_ in xo_pieces)
                p_lines += xo_pieces[:3]
                n_lines.append(xo_pieces[3])
                vias.append(RouteVia(spec.p_net, round(v1[0], 4), round(v1[1], 4), spec.via_size, spec.via_drill))
                vias.append(RouteVia(spec.p_net, round(v2[0], 4), round(v2[1], 4), spec.via_size, spec.via_drill))
                s_cur = -s_cur
                prev_kind = "X"
                continue
            p_off = _offset_polyline(pts, half * s_cur)
            n_off = _offset_polyline(pts, -half * s_cur)
            if prev_kind == "layer":  # start of this piece: from the via pair (placed by the previous piece's end) back to the pitch
                pv, nv = last_vias
                if len(pts) > 1:
                    u0 = (pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
                    l0 = math.hypot(*u0) or 1.0
                    u, n = (u0[0] / l0, u0[1] / l0), normal(u0)
                    run = min(0.6, l0 * 0.5)
                else:
                    u, n, run = in_dir, in_normal, 0.6
                p_off = [pv, (pts[0][0] + u[0] * run + n[0] * half * s_cur, pts[0][1] + u[1] * run + n[1] * half * s_cur)] + p_off[1:]
                n_off = [nv, (pts[0][0] + u[0] * run - n[0] * half * s_cur, pts[0][1] + u[1] * run - n[1] * half * s_cur)] + n_off[1:]
            if k < len(pieces) - 1 and pieces[k + 1][0] != "X":  # end of this piece: splay out to the via pair
                u = (pts[-1][0] - pts[-2][0], pts[-1][1] - pts[-2][1]) if len(pts) > 1 else dir_a
                n = normal(u)
                ln = math.hypot(*u) or 1.0
                run = min(0.6, ln * 0.5)
                pv = (pts[-1][0] + n[0] * via_pitch / 2 * s_cur, pts[-1][1] + n[1] * via_pitch / 2 * s_cur)
                nv = (pts[-1][0] - n[0] * via_pitch / 2 * s_cur, pts[-1][1] - n[1] * via_pitch / 2 * s_cur)
                p_off = p_off[:-1] + [(pts[-1][0] - u[0] / ln * run + n[0] * half * s_cur, pts[-1][1] - u[1] / ln * run + n[1] * half * s_cur), pv]
                n_off = n_off[:-1] + [(pts[-1][0] - u[0] / ln * run - n[0] * half * s_cur, pts[-1][1] - u[1] / ln * run - n[1] * half * s_cur), nv]
                vias.append(RouteVia(spec.p_net, round(pv[0], 4), round(pv[1], 4), spec.via_size, spec.via_drill))
                vias.append(RouteVia(spec.n_net, round(nv[0], 4), round(nv[1], 4), spec.via_size, spec.via_drill))
                in_dir, in_normal = (u[0] / ln, u[1] / ln), n
                last_vias = (pv, nv)
            p_lines.append((layer, p_off))
            n_lines.append((layer, n_off))
            prev_kind = "layer"
        first_layer, last_layer = pieces[0][0], pieces[-1][0]

        la, lb = first_layer, last_layer  # the leads already run on the pad layers
        p_route: list[tuple[str, list[tuple[float, float]]]] = [(la, [(pa.x, pa.y), pa_end])]
        n_route: list[tuple[str, list[tuple[float, float]]]] = [(la, [(na.x, na.y), na_end])]
        p_route.append((la, _dogleg(pa_end, p_lines[0][1][0])))
        n_route.append((la, _dogleg(na_end, n_lines[0][1][0])))
        p_main = (len(p_route), len(p_route) + len(p_lines))
        n_main = (len(n_route), len(n_route) + len(n_lines))
        p_route += p_lines
        n_route += n_lines
        p_route.append((last_layer, _dogleg(p_lines[-1][1][-1], pb_end)))
        n_route.append((last_layer, _dogleg(n_lines[-1][1][-1], nb_end)))
        p_route.append((lb, [pb_end, (pb.x, pb.y)]))
        n_route.append((lb, [nb_end, (nb.x, nb.y)]))

        # lengths and matching
        def total(route, net):
            return sum(_length(pts) for _, pts in route) + 1.6 * sum(1 for v in vias if v.net == net)

        lp, ln_ = total(p_route, spec.p_net), total(n_route, spec.n_net)
        limit = spec.skew_limit or 0.15
        if abs(lp - ln_) > limit * 0.8:
            shorter = p_route if lp < ln_ else n_route
            partner = n_route if shorter is p_route else p_route
            main = p_main if shorter is p_route else n_main
            extra = abs(lp - ln_)
            remaining = extra
            per_bump = 2 * (SQRT2 - 1) * 1.6  # what one bump of the largest amplitude adds

            def bump_checked(pts, amount, layer):
                for amp in (1.6, 1.2, 0.8):  # tallest that fits first; nothing under 0.8: runs of small bumps read as a staircase
                    got = bump_amp(pts, min(amount, 2 * (SQRT2 - 1) * amp), layer)
                    if got is not None:
                        return got
                return None

            def bump_amp(pts, amount, layer):
                a = amount / (2 * (SQRT2 - 1))
                t = 0.4
                for i in sorted(range(len(pts) - 1), key=lambda i: -math.dist(pts[i], pts[i + 1])):
                    p0, q0 = pts[i], pts[i + 1]
                    run = math.dist(p0, q0)
                    if run < 2 * a + t + 0.8:
                        break
                    ux, uy = (q0[0] - p0[0]) / run, (q0[1] - p0[1]) / run
                    m = ((p0[0] + q0[0]) / 2, (p0[1] + q0[1]) / 2)
                    near = min((_nearest_on_polyline(m, pp) for l, pp in partner if len(pp) > 1), key=lambda dc: dc[0], default=None)
                    side_b = 1
                    if near is not None and (near[1][0] - m[0]) * (-uy) + (near[1][1] - m[1]) * ux > 0:
                        side_b = -1  # partner on the left: lean right
                    nx, ny = -uy * side_b, ux * side_b
                    s0 = (run - (2 * a + t)) / 2
                    b1 = (p0[0] + ux * s0, p0[1] + uy * s0)
                    b2 = (b1[0] + ux * a + nx * a, b1[1] + uy * a + ny * a)
                    b3 = (b2[0] + ux * t, b2[1] + uy * t)
                    b4 = (b3[0] + ux * a - nx * a, b3[1] + uy * a - ny * a)
                    if all(_line_free(grid, layer, x, y) for x, y in ((b1, b2), (b2, b3), (b3, b4))):
                        return pts[: i + 1] + [b1, b2, b3, b4] + pts[i + 1 :], 2 * (SQRT2 - 1) * a
                return None

            added = 0
            tried: set[int] = set()
            for _ in range(24):
                if remaining <= 0.02:
                    break
                cands = [k for k in range(main[0], main[1]) if k not in tried and shorter[k][0] in layer_list and id(shorter[k][1]) not in no_bump and _length(shorter[k][1]) >= 1.6]
                if not cands:
                    break
                k_best = max(cands, key=lambda k: _length(shorter[k][1]))
                got = bump_checked(shorter[k_best][1], min(remaining, per_bump), shorter[k_best][0])
                if got is None:
                    tried.add(k_best)
                    continue
                bumped, gained = got
                shorter[k_best] = (shorter[k_best][0], bumped)
                remaining -= gained
                added += 1
            if added:
                res.notes.append(f"{added} tuning bump(s) added {extra - remaining:.3f} mm to the shorter half")
            if remaining > 0.02:
                res.notes.append(f"skew {remaining:.3f} mm left: no straight run long enough for more bumps")
            lp, ln_ = total(p_route, spec.p_net), total(n_route, spec.n_net)
        # P and N must keep the class gap wherever they run on one layer (crossings are the extreme case)
        min_gap = spec.gap - 0.005
        too_close = []
        for lp_, pts_p in p_route:
            for ln_2, pts_n in n_route:
                if lp_ != ln_2:
                    continue
                for a_, b_ in zip(pts_p, pts_p[1:]):
                    for c_, d_ in zip(pts_n, pts_n[1:]):
                        if math.dist(a_, c_) > 6.0 and math.dist(b_, d_) > 6.0 and math.dist(a_, d_) > 6.0 and math.dist(b_, c_) > 6.0:
                            continue
                        if _seg_seg_gap(a_, b_, c_, d_) - spec.width < min_gap:
                            too_close.append(((a_[0] + b_[0]) / 2, (a_[1] + b_[1]) / 2))
        for v in vias:
            other_route = n_route if v.net == spec.p_net else p_route
            for l_, pts_ in other_route:
                for a_, b_ in zip(pts_, pts_[1:]):
                    if _nearest_on_polyline((v.x, v.y), [a_, b_])[0] - spec.via_size / 2 - spec.width / 2 < spec.clearance - 0.005:
                        too_close.append((v.x, v.y))
        if too_close:
            res.status = "failed"
            res.notes.append("geometry rejected: P and N come closer than the class gap at " + ", ".join(f"({x:.1f}, {y:.1f})" for x, y in too_close[:4]))
            res.debug = {"p_route": p_route, "n_route": n_route, "vias": vias, "pieces": pieces, "mirrored": mirrored, "s_p": s_p}
            continue
        crossings = _crossings(p_route, n_route)
        if crossings:
            res.status = "failed"
            res.notes.append("geometry rejected: P and N cross each other at " + ", ".join(f"({x:.1f}, {y:.1f})" for x, y in crossings[:4]))
            res.debug = {"p_route": p_route, "n_route": n_route, "vias": vias, "pieces": pieces, "mirrored": mirrored, "s_p": s_p,
                         "ends": {"pa": (pa.x, pa.y), "na": (na.x, na.y), "pb": (pb.x, pb.y), "nb": (nb.x, nb.y), "pa_end": pa_end, "na_end": na_end, "pb_end": pb_end, "nb_end": nb_end, "dir_a": dir_a, "dir_b": dir_b}}
            continue
        # emit
        new = Routes()
        for layer, pts in p_route:
            new.segments += _segments(spec.p_net, layer, spec.width, pts)
        for layer, pts in n_route:
            new.segments += _segments(spec.n_net, layer, spec.width, pts)
        new.vias = vias + new.vias
        fp_pads = {f.ref: f.pads for f in bm.footprints}
        stub_dir = {id(pa): dir_a, id(na): dir_a, id(pb): dir_b, id(nb): dir_b}
        placed_extra: list[list[tuple[float, float]]] = []  # hooks already laid for this pair's doubled pads
        for (ep, _), (en, _) in extras:
            for pad, net in ((ep, spec.p_net), (en, spec.n_net)):
                target = min(((q, c) for q, c in (p_pads if net == spec.p_net else n_pads) if q is not pad), key=lambda qc: math.dist((pad.x, pad.y), (qc[0].x, qc[0].y)))[0]
                lay = pad_layer(pad, first_layer)
                others = [q for q in fp_pads.get(pad.ref, []) if q.net != net and q.kind != "np_thru_hole"]
                partner_pads = [q for q in fp_pads.get(pad.ref, []) if q.net in (spec.p_net, spec.n_net) and q.net != net]
                margin = spec.width / 2 + spec.clearance

                def clear(pts):
                    if any(_polyline_hits_rect(pts, _pad_rect(q), margin) for q in others):
                        return False
                    for hook_pts in placed_extra:  # keep clear of the other doubled pad's connection
                        for a_, b_ in zip(pts, pts[1:]):
                            if any(_seg_cross(a_, b_, c_, d_) for c_, d_ in zip(hook_pts, hook_pts[1:])):
                                return False
                            for c_, d_ in zip(hook_pts, hook_pts[1:]):
                                if _nearest_on_polyline(a_, [c_, d_])[0] < spec.width + spec.clearance or _nearest_on_polyline(b_, [c_, d_])[0] < spec.width + spec.clearance:
                                    return False
                    ox0, oy0, ox1, oy1 = bm.outline or (-1e9, -1e9, 1e9, 1e9)
                    return all(ox0 + 0.3 + margin <= x <= ox1 - 0.3 - margin and oy0 + 0.3 + margin <= y <= oy1 - 0.3 - margin for x, y in pts)

                straight = _dogleg((pad.x, pad.y), (target.x, target.y))
                if clear(straight):
                    new.segments += _segments(net, lay, spec.width, straight)
                    placed_extra.append(straight)
                    continue
                d = stub_dir.get(id(target))
                w_, h_ = pad.size
                if abs((pad.angle % 180) - 90) < 1e-6:
                    w_, h_ = h_, w_
                done_hook = False
                if d is not None:
                    ext = (w_ / 2 if d[0] else h_ / 2) + margin + 0.05
                    for sgn in (1, -1):
                        L = ext * sgn
                        e1 = (pad.x + d[0] * L, pad.y + d[1] * L)
                        t1 = (target.x + d[0] * L, target.y + d[1] * L)
                        hook = [(pad.x, pad.y), e1, t1] if sgn > 0 else [(pad.x, pad.y), e1, t1, (target.x, target.y)]
                        # the hook may not cross the partner's stub (same side as the stubs) or its pads
                        if sgn > 0 and any(min(q.x, target.x) < pad.x < max(q.x, target.x) or min(q.x, pad.x) < target.x < max(q.x, pad.x) for q in partner_pads if id(q) in stub_dir and abs(d[0]) > 0) :
                            pass
                        blocked = False
                        if sgn > 0:
                            for q in partner_pads:
                                if id(q) not in stub_dir:
                                    continue
                                lo, hi = sorted((pad.x, target.x)) if d[1] else sorted((pad.y, target.y))
                                coord = q.x if d[1] else q.y
                                if lo < coord < hi:
                                    blocked = True
                        if not blocked and clear(hook):
                            new.segments += _segments(net, lay, spec.width, hook)
                            placed_extra.append(hook)
                            done_hook = True
                            break
                if not done_hook and d is not None:
                    # via jumper: out along the stubs past the pad row, down to the other layer, across under the
                    # partner's stub, back up onto the target's stub
                    other_l = next((l for l in layer_list if l != lay), None)
                    if other_l is not None:
                        # the vias sit just outside the two pads (away from each other along the row) so they clear
                        # the stub of the pad between them; short stubs on the pad layer join them to the pads
                        ext = (w_ / 2 if d[0] else h_ / 2) + spec.via_size / 2 + spec.clearance + 0.1
                        row = (target.x - pad.x, target.y - pad.y)
                        rl = math.hypot(*row) or 1.0
                        row = (row[0] / rl, row[1] / rl)
                        pitch_row = min((math.dist((q.x, q.y), (pad.x, pad.y)) for q in fp_pads.get(pad.ref, []) if q is not pad and abs((q.x - pad.x) * row[0] + (q.y - pad.y) * row[1]) > 0.05), default=rl)
                        off = max(0.2, spec.via_size / 2 + spec.clearance + spec.width / 2 - pitch_row + 0.1)
                        e0 = (pad.x + d[0] * ext, pad.y + d[1] * ext)
                        e1 = (e0[0] - row[0] * off, e0[1] - row[1] * off)
                        t0 = (target.x + d[0] * ext, target.y + d[1] * ext)
                        t1 = (t0[0] + row[0] * off, t0[1] + row[1] * off)
                        if clear([(pad.x, pad.y), e0, e1]) and clear([t1, t0]):
                            new.segments += _segments(net, lay, spec.width, [(pad.x, pad.y), e0, e1])
                            new.segments += _segments(net, other_l, spec.width, [e1, t1])
                            new.segments += _segments(net, lay, spec.width, [t1, t0])
                            new.vias.append(RouteVia(net, round(e1[0], 4), round(e1[1], 4), spec.via_size, spec.via_drill))
                            new.vias.append(RouteVia(net, round(t1[0], 4), round(t1[1], 4), spec.via_size, spec.via_drill))
                            res.notes.append(f"{pad.ref}-{pad.number}: doubled pad joined through a via jumper on {other_l}")
                            done_hook = True
                if not done_hook:
                    new.segments += _segments(net, lay, spec.width, straight)
                    res.notes.append(f"{pad.ref}-{pad.number}: doubled pad joined with a straight stub that may cross other pads; check it")
        new.nets = {spec.p_net, spec.n_net}
        done.segments += new.segments
        done.vias += new.vias
        done.nets |= new.nets
        res.status = "routed"
        res.debug = {"mirrored": mirrored, "s_p": s_p, "dir_a": dir_a, "dir_b": dir_b, "pa_end": pa_end, "na_end": na_end, "pb_end": pb_end, "nb_end": nb_end,
                     "p_route": p_route, "n_route": n_route, "vias": vias}
        res.p_length, res.n_length, res.skew = round(lp, 3), round(ln_, 3), round(abs(lp - ln_), 3)
        res.layers = sorted({l for l, _ in pieces if l != "X"})
        res.vias = len(vias)
    return done, results
