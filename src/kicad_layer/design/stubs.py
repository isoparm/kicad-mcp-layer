"""Stub routing: the open connections of single-ended nets, over the exact copper model, with every
existing track, via and pad protected.

For each open connection the DRC report names (a pad and what it should reach), the router tries,
in order: the straight segment; two elbows and four octilinear dog-legs; detours that step aside by
up to three millimetres; a via next to the start and the same shapes on the other outer layer (or a
via at each end when the far end is a track on the other layer); and last a grid search on a
quarter-millimetre lattice inside the connection's neighbourhood, straightened afterwards. A pad on
a plane net gets a via beside it. Every candidate is judged by ``copper.Model``, so the result keeps
the project's clearances by construction; the build re-runs DRC anyway. Differential pairs are left
to the pair router.

    python -m kicad_layer.design.stubs <build-dir or .kicad_pcb> [--dry-run]

reads ``_drc.json`` next to the board and ``routing/routes.json`` of the project (or ``--routes``),
and ``build.py --route-stubs`` runs it after the DRC and rebuilds.
"""
from __future__ import annotations

import heapq
import json
import math
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from kicad_layer.review import BoardModel, load_board
from kicad_layer.routes import RouteSegment, RouteVia, Routes, load as load_routes, save as save_routes

from . import copper

_NET = re.compile(r"\[([^\]]+)\]")
_LAYER = re.compile(r"\bon (F\.Cu|B\.Cu|In\d\.Cu)\b")
PAIR = re.compile(r"(_P|_N|_DP|_DN|\+|-)$")
OUTER = ("F.Cu", "B.Cu")
GRID = 0.25
GRID_REACH = 4.0
GRID_LIMIT = 40000


@dataclass
class Open:
    """One unconnected pair from the DRC report: where each end is and on which layer, if the report says."""

    net: str
    a: tuple[float, float]
    b: tuple[float, float]
    layer_a: str | None = None
    layer_b: str | None = None
    what_a: str = ""
    what_b: str = ""


def open_connections(drc: dict) -> list[Open]:
    out = []
    for u in drc.get("unconnected_items", []):
        items = u.get("items", [])
        if len(items) < 2:
            continue
        d0, d1 = items[0]["description"], items[1]["description"]
        m = _NET.search(d0)
        if not m:
            continue
        la, lb = _LAYER.search(d0), _LAYER.search(d1)
        out.append(Open(m.group(1), (items[0]["pos"]["x"], items[0]["pos"]["y"]), (items[1]["pos"]["x"], items[1]["pos"]["y"]),
                        la.group(1) if la else None, lb.group(1) if lb else None, d0, d1))
    return out


@dataclass
class Stub:
    net: str
    segments: list[tuple[str, tuple[float, float], tuple[float, float]]] = field(default_factory=list)  # layer, a, b
    vias: list[tuple[float, float]] = field(default_factory=list)
    how: str = ""

    def length(self) -> float:
        return sum(math.dist(a, b) for _, a, b in self.segments)


@dataclass
class Result:
    routes: Routes
    lines: list[str] = field(default_factory=list)
    routed: int = 0
    failed: int = 0
    skipped: int = 0


# ---------------------------------------------------------------- candidate shapes on one layer
def _clear(model: copper.Model, net: str, layer: str, pts: list[tuple[float, float]], width: float) -> bool:
    return all(not model.check_segment(net, layer, p, q, width) for p, q in zip(pts, pts[1:]) if p != q)


def _shapes(a: tuple[float, float], b: tuple[float, float]) -> list[tuple[str, list[tuple[float, float]]]]:
    """The straight line, two elbows, four dog-legs (diagonal then straight, straight then diagonal), then side-steps."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    out: list[tuple[str, list[tuple[float, float]]]] = [("straight", [a, b])]
    if abs(dx) > 1e-6 and abs(dy) > 1e-6:
        out += [("elbow", [a, (bx, ay), b]), ("elbow", [a, (ax, by), b])]
        sx, sy = math.copysign(1, dx), math.copysign(1, dy)
        m = min(abs(dx), abs(dy))
        out += [("dog-leg", [a, (ax + sx * m, ay + sy * m), b]), ("dog-leg", [a, (bx - sx * m, by - sy * m), b])]
    length = math.hypot(dx, dy)
    if length > 1e-6:
        nx, ny = -dy / length, dx / length
        for t in [0.25 * k * s for k in range(1, 13) for s in (1, -1)]:
            out.append(("side-step", [a, (ax + nx * t, ay + ny * t), (bx + nx * t, by + ny * t), b]))
    return out


def _on_layer(model: copper.Model, net: str, layer: str, a, b, width: float) -> tuple[str, list[tuple[float, float]]] | None:
    for how, pts in _shapes(a, b):
        if _clear(model, net, layer, pts, width):
            return how, pts
    return None


# ---------------------------------------------------------------- vias
def _via_spots(model: copper.Model, net: str, p: tuple[float, float], from_layer: str, width: float, size: float, drill: float,
               radii=(0.6, 0.8, 1.0, 1.3, 1.6, 2.0, 2.5)) -> list[tuple[float, float]]:
    """Places for a via reachable from ``p`` on ``from_layer`` by a clear stub, nearest first; never inside the pad itself."""
    out = []
    for r in radii:
        angles = [math.radians(k * 45) for k in range(8)]
        for ang in angles:
            v = (round(p[0] + r * math.cos(ang), 3), round(p[1] + r * math.sin(ang), 3))
            if not model.inside_outline(*v):
                continue
            if model.check_circle(net, model.copper, v[0], v[1], size / 2) or model.check_circle(net, model.copper, v[0], v[1], drill / 2, is_hole=True):
                continue
            if r > 0 and model.check_segment(net, from_layer, p, v, width):
                continue
            out.append(v)
    return out


# ---------------------------------------------------------------- grid fallback
def _grid(model: copper.Model, net: str, layer: str, a, b, width: float) -> list[tuple[float, float]] | None:
    """A* on a GRID lattice inside the connection's neighbourhood; None when nothing gets through."""
    x0, y0 = min(a[0], b[0]) - GRID_REACH, min(a[1], b[1]) - GRID_REACH
    x1, y1 = max(a[0], b[0]) + GRID_REACH, max(a[1], b[1]) + GRID_REACH

    def snap(p):
        return (round(round((p[0] - x0) / GRID) * GRID + x0, 4), round(round((p[1] - y0) / GRID) * GRID + y0, 4))

    start, goal = snap(a), snap(b)
    if not _clear(model, net, layer, [a, start], width) or not _clear(model, net, layer, [goal, b], width):
        return None
    moves = [(GRID, 0), (-GRID, 0), (0, GRID), (0, -GRID), (GRID, GRID), (GRID, -GRID), (-GRID, GRID), (-GRID, -GRID)]
    seen_ok: dict[tuple, bool] = {}

    def step_ok(p, q) -> bool:
        key = (p, q)
        if key not in seen_ok:
            seen_ok[key] = not model.check_segment(net, layer, p, q, width)
        return seen_ok[key]

    frontier = [(math.dist(start, goal), 0.0, start)]
    came: dict[tuple, tuple | None] = {start: None}
    cost: dict[tuple, float] = {start: 0.0}
    n = 0
    while frontier and n < GRID_LIMIT:
        _, g, p = heapq.heappop(frontier)
        n += 1
        if p == goal:
            pts = []
            while p is not None:
                pts.append(p)
                p = came[p]
            pts.reverse()
            return [a] + pts[1:-1] + [b] if len(pts) > 1 else [a, b]
        for mx, my in moves:
            q = (round(p[0] + mx, 4), round(p[1] + my, 4))
            if not (x0 <= q[0] <= x1 and y0 <= q[1] <= y1):
                continue
            ng = g + math.hypot(mx, my)
            if ng >= cost.get(q, math.inf) or not step_ok(p, q):
                continue
            cost[q] = ng
            came[q] = p
            heapq.heappush(frontier, (ng + math.dist(q, goal), ng, q))
    return None


def _straighten(model: copper.Model, net: str, layer: str, pts: list[tuple[float, float]], width: float) -> list[tuple[float, float]]:
    """String pulling: from each point, jump to the farthest later point the straight segment reaches clear."""
    out = [pts[0]]
    i = 0
    while i < len(pts) - 1:
        j = len(pts) - 1
        while j > i + 1 and not _clear(model, net, layer, [pts[i], pts[j]], width):
            j -= 1
        out.append(pts[j])
        i = j
    return out


# ---------------------------------------------------------------- one connection
def _route_one(model: copper.Model, o: Open, width: float, size: float, drill: float) -> Stub | None:
    net = o.net
    stub = Stub(net)
    if net in model.plane_nets:
        # a pad of a plane net: a via beside it reaches the plane; the far end is the plane or another such pad
        for p, layer, what in ((o.a, o.layer_a, o.what_a), (o.b, o.layer_b, o.what_b)):
            if layer not in OUTER or not what.startswith("Pad"):
                continue
            spots = _via_spots(model, net, p, layer, width, size, drill)
            if not spots:
                return None
            v = spots[0]
            if v != p:
                stub.segments.append((layer, p, v))
            stub.vias.append(v)
        if stub.vias:
            stub.how = "via to the plane"
            return stub
    # any copper of the end's own island is as good a place to join as the reported item itself
    starts = _joints(model, net, o.a, o.layer_a)
    ends = _joints(model, net, o.b, o.layer_b)
    pairs = sorted(((s, t) for s in starts for t in ends if math.dist(s[0], t[0]) > 1e-6), key=lambda st: math.dist(st[0][0], st[1][0]))[:12]
    best: Stub | None = None
    for (s, ls), (t, lt) in pairs:
        r = _connect(model, net, s, ls, t, lt, width, size, drill, grid=False)
        if r and (r.segments or r.vias) and (best is None or _cost(r) < _cost(best)):
            best = r
    if best is not None:
        return best
    for (s, ls), (t, lt) in pairs[:4]:
        r = _connect(model, net, s, ls, t, lt, width, size, drill, grid=True)
        if r and (r.segments or r.vias):
            return r
    return None


def _cost(stub: Stub) -> float:
    """Length plus a via penalty: a via costs as much as a millimetre and a half of track."""
    return stub.length() + 1.5 * len(stub.vias)


def _item_gap(a: copper.Item, b: copper.Item) -> float:
    if a.shape == "circle":
        return b.distance_to_point(*a.geom) - a.radius
    if a.shape == "capsule":
        return b.distance_to_segment(a.geom[:2], a.geom[2:]) - a.radius
    return copper._rect_to_item(*a.geom, b) - a.radius


def _island(model: copper.Model, net: str, p: tuple[float, float], radius: float) -> list[copper.Item]:
    """The net's copper connected to the item at ``p``, within ``radius``: same-net items that touch on a shared layer."""
    pool = [it for it in model.near((p[0] - radius, p[1] - radius, p[0] + radius, p[1] + radius)) if it.net == net and it.kind not in ("hole", "edge")]
    seeds = [it for it in pool if it.distance_to_point(*p) <= 0.05]
    reached: list[copper.Item] = []
    seen: set[int] = {id(it) for it in seeds}
    queue = list(seeds)
    while queue:
        a = queue.pop()
        reached.append(a)
        for b in pool:
            if id(b) in seen or not (set(a.layers) & set(b.layers)):
                continue
            if _item_gap(a, b) <= 0.02:
                seen.add(id(b))
                queue.append(b)
    return reached


def _joints(model: copper.Model, net: str, p: tuple[float, float], layer: str | None, radius: float = 4.0, limit: int = 8) -> list[tuple[tuple[float, float], str | None]]:
    """``p`` itself, then the vias, pads and track ends of its own island within ``radius`` (a via or through-hole pad is on every layer: None)."""
    home = (round(p[0], 2), round(p[1], 2))
    found: dict[tuple[float, float], tuple[tuple[float, float], str | None]] = {}
    for it in _island(model, net, p, radius):
        if it.kind == "track":
            pts = [((it.geom[0], it.geom[1]), it.layers[0]), ((it.geom[2], it.geom[3]), it.layers[0])]
        elif it.kind == "via":
            pts = [((it.geom[0], it.geom[1]), None)]
        elif it.shape in ("circle", "rect"):
            pts = [((it.geom[0], it.geom[1]), None if len(it.layers) > 2 else it.layers[0])]
        else:
            pts = []
        for q, lq in pts:
            key = (round(q[0], 2), round(q[1], 2))
            if key == home or math.dist(q, p) > radius:
                continue
            if key not in found or lq is None:  # a via at a track's end makes that point reachable on every layer
                found[key] = (q, lq)
    cands = sorted(found.values(), key=lambda c: math.dist(c[0], p))
    return [(p, layer)] + cands[:limit]


def _path_segments(layer: str, pts: list[tuple[float, float]]) -> list[tuple[str, tuple[float, float], tuple[float, float]]]:
    return [(layer, p, q) for p, q in zip(pts, pts[1:]) if p != q]


def _connect(model: copper.Model, net: str, s, ls: str | None, t, lt: str | None, width: float, size: float, drill: float, *, grid: bool) -> Stub | None:
    """Join s (on ls, or any layer when None) to t: on a shared layer; with a via beside the end bound to the other layer;
    with a via at each end; or, with ``grid``, by the lattice search on a shared layer or through a via."""
    common = [L for L in OUTER if ls in (L, None) and lt in (L, None)]
    stub = Stub(net)
    if not grid:
        for L in common:
            found = _on_layer(model, net, L, s, t, width)
            if found:
                stub.segments = _path_segments(L, found[1])
                stub.how = f"{found[0]} on {L}"
                return stub
        for L in OUTER:  # s reaches L; t is bound to the other layer: a via beside t
            if ls not in (L, None) or lt in (L, None):
                continue
            for v in _via_spots(model, net, t, lt, width, size, drill)[:10]:
                found = _on_layer(model, net, L, s, v, width)
                if found:
                    stub.segments = _path_segments(L, found[1]) + [(lt, v, t)]
                    stub.vias = [v]
                    stub.how = f"{found[0]} on {L}, via beside the end"
                    return stub
        for L in OUTER:  # t reaches L; s is bound to the other layer: a via beside s
            if lt not in (L, None) or ls in (L, None):
                continue
            for v in _via_spots(model, net, s, ls, width, size, drill)[:10]:
                found = _on_layer(model, net, L, v, t, width)
                if found:
                    stub.segments = [(ls, s, v)] + _path_segments(L, found[1])
                    stub.vias = [v]
                    stub.how = f"via beside the start, {found[0]} on {L}"
                    return stub
        if ls in OUTER and lt == ls:  # both bound to one layer: over the other one with a via at each end
            other = "B.Cu" if ls == "F.Cu" else "F.Cu"
            for va in _via_spots(model, net, s, ls, width, size, drill)[:8]:
                for vb in _via_spots(model, net, t, lt, width, size, drill)[:8]:
                    if va == vb:
                        continue
                    found = _on_layer(model, net, other, va, vb, width)
                    if found:
                        stub.segments = [(ls, s, va)] + _path_segments(other, found[1]) + [(lt, vb, t)]
                        stub.vias = [va, vb]
                        stub.how = f"two vias, {found[0]} on {other}"
                        return stub
        return None
    for L in common:
        pts = _grid(model, net, L, s, t, width)
        if pts:
            pts = _straighten(model, net, L, pts, width)
            stub.segments = _path_segments(L, pts)
            stub.how = f"grid search on {L}, {len(stub.segments)} segments"
            return stub
    for L in OUTER:
        if ls in (L, None) and lt not in (L, None):
            for v in _via_spots(model, net, t, lt, width, size, drill)[:3]:
                pts = _grid(model, net, L, s, v, width)
                if pts:
                    pts = _straighten(model, net, L, pts, width)
                    stub.segments = _path_segments(L, pts) + [(lt, v, t)]
                    stub.vias = [v]
                    stub.how = f"grid search on {L}, {len(stub.segments) - 1} segments, via beside the end"
                    return stub
        if lt in (L, None) and ls not in (L, None):
            for v in _via_spots(model, net, s, ls, width, size, drill)[:3]:
                pts = _grid(model, net, L, v, t, width)
                if pts:
                    pts = _straighten(model, net, L, pts, width)
                    stub.segments = [(ls, s, v)] + _path_segments(L, pts)
                    stub.vias = [v]
                    stub.how = f"via beside the start, grid search on {L}, {len(stub.segments) - 1} segments"
                    return stub
    return None


def _blockers(model: copper.Model, o: Open, width: float) -> str:
    layer = o.layer_a if o.layer_a in OUTER else (o.layer_b if o.layer_b in OUTER else "F.Cu")
    v = model.check_segment(o.net, layer, o.a, o.b, width)
    if not v:
        return "no straight-line blocker"
    d, req, it = v[0]
    return f"straight line on {layer} blocked by {copper._fmt_item(it)} ({d:.3f} mm, needs {req:.3f})"


def route_stubs(bm: BoardModel, rules: copper.Rules, opens: list[Open], pcb: Path | None = None, *, routes: Routes | None = None) -> Result:
    """Route the open connections; the new copper is appended to ``routes`` (a copy) and described in ``lines``."""
    model = copper.Model(bm, rules, pcb)
    out = Routes(segments=list(routes.segments) if routes else [], vias=list(routes.vias) if routes else [], nets=set(routes.nets) if routes else set())
    res = Result(out)
    for o in opens:
        if PAIR.search(o.net.rsplit("/", 1)[-1]):
            res.skipped += 1
            res.lines.append(f"skipped {o.net}: a differential pair line, for the pair router")
            continue
        width = rules.track(o.net)
        size, drill = rules.via(o.net)
        t0 = time.perf_counter()
        stub = _route_one(model, o, width, size, drill)
        dt = time.perf_counter() - t0
        if stub is None:
            res.failed += 1
            res.lines.append(f"FAILED {o.net} ({o.a[0]:.2f},{o.a[1]:.2f}) to ({o.b[0]:.2f},{o.b[1]:.2f}): {_blockers(model, o, width)}")
            continue
        for layer, p, q in stub.segments:
            out.segments.append(RouteSegment(o.net, layer, width, p[0], p[1], q[0], q[1]))
            model.add_segment(o.net, layer, p, q, width)
        for x, y in stub.vias:
            out.vias.append(RouteVia(o.net, x, y, size, drill))
            model.add_via(o.net, x, y, size, drill)
        out.nets.add(o.net)
        res.routed += 1
        res.lines.append(f"routed {o.net}: {stub.how}, {len(stub.segments)} segment(s), {len(stub.vias)} via(s), {stub.length():.1f} mm, {dt:.2f} s")
    return res


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    p = Path(args[0]).resolve()
    board = next(p.glob("*.kicad_pcb")) if p.is_dir() else p
    folder = board.parent
    drc_json = folder / "_drc.json"
    if not drc_json.is_file():
        print(f"no _drc.json next to {board.name}; build first")
        return 1
    routes_path = Path(argv[argv.index("--routes") + 1]) if "--routes" in argv else folder / "routing" / "routes.json"
    opens = open_connections(json.loads(drc_json.read_text(encoding="utf-8")))
    if not opens:
        print("nothing open")
        return 0
    routes = load_routes(routes_path) if routes_path.is_file() else Routes()
    res = route_stubs(load_board(board), copper.Rules.load(board.with_suffix(".kicad_pro")), opens, board, routes=routes)
    for line in res.lines:
        print(line)
    print(f"{res.routed} routed, {res.failed} failed, {res.skipped} skipped")
    if res.routed and "--dry-run" not in argv:
        save_routes(res.routes, routes_path)
        print(f"routes saved: {routes_path}")
    return 0 if not res.failed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
