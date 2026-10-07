"""Route one connection: a grid A* over the board's copper model, clearances from the project's rules.

FreeRouting routes a whole board; fixing one net after a placement change, a rip-up or a review
finding is a different job: one connection, with a say over where it goes. ``route_connection``
searches a grid (0.25 mm by default) on the allowed layers, in eight directions, changing layer by a
via where the rules allow one, and checks every step against ``design.copper``: the net classes and
``.kicad_dru`` of the project, holes, the board edge, the copper already there.

Two costs steer it beyond length:

* ``layer_cost``: a factor per layer, e.g. ``{"B.Cu": 3}`` keeps a two-layer board's ground plane
  whole by preferring the top.
* ``keep_under``: nets (a USB pair, a clock) whose copper this route should not run beneath on the
  other layer. Running under them costs ``under_cost`` per mm, so the router crosses them square
  and short when it must, and goes around when it can.

The result is a routes JSON (segments and vias by net, the format of the other routers) and,
when asked, the copper written into the board.
"""

from __future__ import annotations

import heapq
import json
import math
from dataclasses import dataclass
from pathlib import Path

from kicad_layer.errors import INVALID_ARGUMENT, NOT_FOUND_IN_DESIGN, LayerError
from kicad_layer.routes import RouteSegment, Routes, RouteVia

DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]


@dataclass
class Endpoint:
    x: float
    y: float
    layers: tuple[str, ...]
    label: str


def endpoint(model, spec, net: str) -> Endpoint:
    """'REF.PAD' (the pad's centre and copper layers) or [x, y] or [x, y, layer]."""
    if isinstance(spec, str):
        ref, _, num = spec.partition(".")
        fp = next((f for f in model.bm.footprints if f.ref == ref), None)
        if fp is None:
            raise LayerError(NOT_FOUND_IN_DESIGN, f"No footprint {ref} on the board.")
        pad = next((p for p in fp.pads if p.number == num), None)
        if pad is None:
            raise LayerError(NOT_FOUND_IN_DESIGN, f"{ref} has no pad {num}.")
        if pad.net != net:
            raise LayerError(INVALID_ARGUMENT, f"{spec} is on net {pad.net}, not {net}.")
        if pad.kind == "smd":
            layers = tuple(l for l in pad.layers if l.endswith(".Cu")) or ("F.Cu",)
        else:
            layers = tuple(model.copper)
        return Endpoint(pad.x, pad.y, layers, spec)
    if isinstance(spec, (list, tuple)) and len(spec) in (2, 3):
        layers = (str(spec[2]),) if len(spec) == 3 else tuple(model.copper)
        return Endpoint(float(spec[0]), float(spec[1]), layers, f"({spec[0]}, {spec[1]})")
    raise LayerError(INVALID_ARGUMENT, f"An end is 'REF.PAD' or [x, y] or [x, y, layer], not {spec!r}.")


def _under_tracks(model, nets: list[str]):
    out = []
    for s in model.bm.segments:
        if s.net in nets:
            out.append(((s.x1, s.y1), (s.x2, s.y2), s.width / 2, s.layer, s.net))
    return out


def route_connection(model, net: str, a: Endpoint, b: Endpoint, *, width: float | None = None, layers: tuple[str, ...] | None = None,
                     via_cost: float = 6.0, layer_cost: dict[str, float] | None = None, step: float = 0.25, margin: float = 4.0,
                     keep_under: list[str] | None = None, under_cost: float = 25.0, under_gap: float = 0.3, max_nodes: int = 400_000,
                     via_size: float | None = None, via_drill: float | None = None):
    """(points, nodes) where points is [(x, y, layer), ...] from a to b, or (None, nodes)."""
    from kicad_layer.design.copper import d_seg_seg

    layers = tuple(layers or model.copper)
    width = width or model.rules.track(net)
    vsize, vdrill = model.rules.via(net)
    vsize, vdrill = via_size or vsize, via_drill or vdrill
    vias_ok = model.rules.disallowed("via", net) is None and len(layers) > 1
    lc = {l: 1.0 for l in layers}
    lc.update(layer_cost or {})
    under = _under_tracks(model, keep_under or [])
    hw = width / 2

    def q(v: float) -> float:
        return round(round(v / step) * step, 4)

    sx, sy, gx, gy = q(a.x), q(a.y), q(b.x), q(b.y)
    x0, x1 = min(a.x, b.x) - margin, max(a.x, b.x) + margin
    y0, y1 = min(a.y, b.y) - margin, max(a.y, b.y) + margin
    seg_ok: dict[tuple, bool] = {}
    via_ok: dict[tuple, bool] = {}
    # a via never lands in a surface-mount pad, its own net's included: solder wicks down an open via
    # (review's via_in_pad check), so the clearance model, which lets same-net copper touch, is not enough
    from kicad_layer.design.copper import pad_item

    smd_pads = []
    for fp in model.bm.footprints:
        for p in fp.pads:
            if p.kind == "smd":
                it = pad_item(p, model.copper)
                if it is not None:
                    smd_pads.append(it)

    def clear_seg(p, r, layer) -> bool:
        key = (p, r, layer)
        if key not in seg_ok:
            seg_ok[key] = not model.check_segment(net, layer, p, r, width)
        return seg_ok[key]

    def clear_via(p) -> bool:
        if p not in via_ok:
            via_ok[p] = (not model.check_circle(net, model.copper, p[0], p[1], vsize / 2)
                         and not model.check_circle(net, model.copper, p[0], p[1], vdrill / 2, is_hole=True) and model.inside_outline(*p)
                         and all(it.distance_to_point(*p) >= vsize / 2 for it in smd_pads
                                 if it.bbox[0] - vsize < p[0] < it.bbox[2] + vsize and it.bbox[1] - vsize < p[1] < it.bbox[3] + vsize))
        return via_ok[p]

    def under_pen(p, r, layer) -> float:
        if not under:
            return 0.0
        pen = 0.0
        for ua, ub, uhw, ul, _un in under:
            if ul == layer:
                continue
            if d_seg_seg(p, r, ua, ub) < uhw + hw + under_gap:
                pen += under_cost * math.dist(p, r)
        return pen

    starts = []
    for l in a.layers:
        if l in layers and clear_seg((a.x, a.y), (sx, sy), l):
            starts.append((sx, sy, l))
    if not starts:
        return None, 0
    goals = {(gx, gy, l) for l in b.layers if l in layers}
    openq: list[tuple[float, float, tuple, tuple | None]] = []
    for s in starts:
        heapq.heappush(openq, (math.dist((sx, sy), (gx, gy)), 0.0, s, None))
    came: dict[tuple, tuple | None] = {}
    best: dict[tuple, float] = {s: 0.0 for s in starts}
    n = 0
    while openq and n < max_nodes:
        _, g, cur, par = heapq.heappop(openq)
        if cur in came:
            continue
        came[cur] = par
        n += 1
        if cur in goals and clear_seg((cur[0], cur[1]), (b.x, b.y), cur[2]):
            path = [cur]
            while came[path[-1]] is not None:
                path.append(came[path[-1]])
            path.reverse()
            return [(a.x, a.y, path[0][2])] + path + [(b.x, b.y, path[-1][2])], n
        x, y, l = cur
        for dx, dy in DIRS:
            nx, ny = round(x + dx * step, 4), round(y + dy * step, 4)
            if not (x0 <= nx <= x1 and y0 <= ny <= y1):
                continue
            nb = (nx, ny, l)
            if nb in came or not clear_seg((x, y), (nx, ny), l):
                continue
            ng = g + step * math.hypot(dx, dy) * lc.get(l, 1.0) + under_pen((x, y), (nx, ny), l)
            if ng < best.get(nb, 1e18):
                best[nb] = ng
                heapq.heappush(openq, (ng + math.dist((nx, ny), (gx, gy)), ng, nb, cur))
        if vias_ok:
            for l2 in layers:
                if l2 == l:
                    continue
                nb = (x, y, l2)
                if nb in came or not clear_via((x, y)):
                    continue
                ng = g + via_cost
                if ng < best.get(nb, 1e18):
                    best[nb] = ng
                    heapq.heappush(openq, (ng + math.dist((x, y), (gx, gy)), ng, nb, cur))
    return None, n


def route_preferred(model, net: str, a: Endpoint, b: Endpoint, *, width: float | None = None, **kw):
    """(points, nodes, width, necked): with no width given, the preferred width (0.25 mm or the net's minimum if
    larger) first, and the net's minimum only when the wider track finds no way."""
    if width:
        pts, n = route_connection(model, net, a, b, width=width, **kw)
        return pts, n, width, False
    wide, narrow = model.rules.preferred_track(net), model.rules.track(net)
    pts, n = route_connection(model, net, a, b, width=wide, **kw)
    if pts is not None or narrow >= wide:
        return pts, n, wide, False
    pts, n2 = route_connection(model, net, a, b, width=narrow, **kw)
    return pts, n + n2, narrow, pts is not None


def simplify(model, net: str, pts: list[tuple[float, float, str]], width: float) -> list[tuple[float, float, str]]:
    """Drop collinear points, then shortcut runs on one layer where the straight line keeps every clearance."""
    out = [pts[0]]
    for p in pts[1:]:
        if len(out) >= 2 and out[-1][2] == p[2] == out[-2][2]:
            (ax, ay, _), (bx, by, _) = out[-2], out[-1]
            if abs((bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)) < 1e-9:
                out[-1] = p
                continue
        out.append(p)
    i = 0
    res = [out[0]]
    while i < len(out) - 1:
        j = len(out) - 1
        while j > i + 1:
            if all(out[k][2] == out[i][2] for k in range(i, j + 1)) and not model.check_segment(net, out[i][2], out[i][:2], out[j][:2], width):
                break
            j -= 1
        res.append(out[j])
        i = j
    return res


def to_routes(net: str, pts: list[tuple[float, float, str]], width: float, vsize: float, vdrill: float) -> Routes:
    r = Routes()
    for (ax, ay, la), (bx, by, lb) in zip(pts, pts[1:]):
        if la != lb:
            r.vias.append(RouteVia(net, round(ax, 4), round(ay, 4), vsize, vdrill))
            continue
        if (ax, ay) != (bx, by):
            r.segments.append(RouteSegment(net, la, width, round(ax, 4), round(ay, 4), round(bx, 4), round(by, 4)))
    r.nets = {net}
    return r


def under_report(model, routes: Routes, nets: list[str]) -> list[str]:
    from kicad_layer.design.copper import d_seg_seg

    out = []
    under = _under_tracks(model, nets)
    for s in routes.segments:
        for ua, ub, uhw, ul, un in under:
            if ul == s.layer:
                continue
            if d_seg_seg((s.x1, s.y1), (s.x2, s.y2), ua, ub) < uhw + s.width / 2 + 0.3:
                out.append(f"{un} at ({s.x1:.2f},{s.y1:.2f})-({s.x2:.2f},{s.y2:.2f}) on {s.layer}")
                break
    return out
