"""Plane stitching: a short stub and a via from every surface-mount pad on a plane net down to its plane.

Through-hole pads reach the inner planes on their own; surface pads need a via. For each such pad the
stitcher tries a via just past the pad end (first along the pad's long axis, then across it), starting on
the side that suits the footprint: connectors keep their vias inside, in the channel between their pin
rows, so the signal escapes on the outside stay free; small parts put the via on the outside. Every
candidate is checked against the other pads, the routes already on the board, the vias placed so far,
keep-outs and the board edge. Pads that cannot be stitched are reported and left to the autorouter.

A via must also land on its plane: ``plane_cover`` checks that the net's plane has copper under it on a
layer other than the pad's (the fill when the zones are filled, the outlines minus other nets' zones and
keep-outs when not), and keep-outs that forbid vias are respected. Pads with no spot over the plane are
reported under ``rejected`` with the reason.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .pairrouter import _pad_rect
from .plane_cover import PlaneCoverage
from ..review import BoardModel, PadGeo, load_board
from ..routing import load_netclasses, netclass_for
from .ses import RouteSegment, Routes, RouteVia


@dataclass
class StitchResult:
    routes: Routes
    stitched: int = 0
    joined: int = 0  # pads linked to a same-net pad of their footprint instead of a via
    in_pad: int = 0  # large pads that took the via inside
    skipped: list[str] = None  # "REF-PIN net: reason"
    rejected: list[str] = None  # pads refused because no via spot reaches their plane, "REF-PIN net: reason"
    warnings: list[str] = None

    def __post_init__(self):
        if self.skipped is None:
            self.skipped = []
        if self.rejected is None:
            self.rejected = []
        if self.warnings is None:
            self.warnings = []


def _rect_dist(x: float, y: float, r: tuple[float, float, float, float]) -> float:
    dx = 0.0 if r[0] <= x <= r[2] else min(abs(x - r[0]), abs(x - r[2]))
    dy = 0.0 if r[1] <= y <= r[3] else min(abs(y - r[1]), abs(y - r[3]))
    return math.hypot(dx, dy)


def _seg_point_dist(a, b, p) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 < 1e-12 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    return math.dist(p, (a[0] + dx * t, a[1] + dy * t))


def _seg_rect_dist(a, b, r, samples: int = 8) -> float:
    return min(_rect_dist(a[0] + (b[0] - a[0]) * k / samples, a[1] + (b[1] - a[1]) * k / samples, r) for k in range(samples + 1))


def _seg_seg_dist(a, b, c, d) -> float:
    """Distance between segments ab and cd (0 when they cross)."""
    def orient(p, q, r):
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)
    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    if o1 != o2 and o3 != o4:
        return 0.0
    return min(_seg_point_dist(a, b, c), _seg_point_dist(a, b, d), _seg_point_dist(c, d, a), _seg_point_dist(c, d, b))


def stitch_planes(board: Path, project: Path | None = None, *, existing: Routes | None = None, plane_nets: set[str] | None = None,
                  keepouts: list[tuple[float, float, float, float]] = (), via: tuple[float, float] | None = None,
                  layers: tuple[str, str] = ("F.Cu", "B.Cu"), plane_layers: dict[str, str] | None = None,
                  fanout_nets: set[str] | None = None) -> StitchResult:
    """``plane_nets`` may include nets without a plane: those pads get a fan-out via for the autorouter to reach on
    the other layer, placed on the open side of a connector (the channel between pin rows is full of plane vias).
    ``fanout_nets`` limits that to the nets named; ``None`` lets every plane-less net of ``plane_nets`` fan out.
    ``plane_layers`` (layer -> net, as for the autorouter) says which layers are the planes; a plane layer without
    a zone of its net is taken as solid."""
    bm: BoardModel = load_board(board)
    if project is None:
        cand = board.with_suffix(".kicad_pro")
        project = cand if cand.is_file() else None
    classes, assignments = load_netclasses(project)
    coverage = PlaneCoverage(bm, plane_layers)
    real_planes = {z.net for z in bm.zones if z.net and not z.rule_area} | set((plane_layers or {}).values())
    nets = set(plane_nets or real_planes) | set(fanout_nets or ())
    existing = existing or Routes()
    out = Routes()
    result = StitchResult(routes=out, warnings=list(coverage.warnings))
    NO_PLANE = "no plane copper under the via"
    ox0, oy0, ox1, oy1 = bm.outline or (-1e9, -1e9, 1e9, 1e9)
    edge = 0.5

    # copper to keep clear of: every pad (other nets), board routes and vias, routes handed in, keep-outs
    pads_all = [(p, _pad_rect(p), f.ref) for f in bm.footprints for p in f.pads]
    segs = [(s.layer, (s.x1, s.y1), (s.x2, s.y2), s.width, s.net) for s in bm.segments] + \
           [(s.layer, (s.x1, s.y1), (s.x2, s.y2), s.width, s.net) for s in existing.segments]
    vias_all = [((v.x, v.y), v.size, v.net) for v in bm.vias] + [((v.x, v.y), v.size, v.net) for v in existing.vias]
    hole_gap = 0.5  # drill-to-drill between vias
    NPTH_GAP = 0.25  # copper to an unplated hole
    clr_cache: dict[str, float] = {}

    def net_clearance(net: str) -> float:
        """KiCad applies the larger of the two nets' class clearances."""
        if net not in clr_cache:
            _, c = netclass_for(net, classes, assignments)
            clr_cache[net] = float(c.get("clearance", 0.125))
        return clr_cache[net]

    for f in bm.footprints:
        big = len(f.pads) > 12
        for p in f.pads:
            if p.kind != "smd" or p.net not in nets:
                continue
            has_plane = p.net in coverage.nets
            if not has_plane and fanout_nets is not None and p.net not in fanout_nets:
                why = f"{f.ref}-{p.number} {p.net}: no {p.net} plane on any layer"
                result.rejected.append(why)
                result.skipped.append(why)
                continue
            _, cls = netclass_for(p.net, classes, assignments)
            clearance = float(cls.get("clearance", 0.2))
            default_cls = classes.get("Default", {})
            class_clearance = clearance
            via_options = [(via, clearance)] if via else [((float(cls.get("via_diameter", 0.7)), float(cls.get("via_drill", 0.3))), clearance)]
            small = (float(default_cls.get("via_diameter", 0.6)), float(default_cls.get("via_drill", 0.3)))
            if not via and small[0] < via_options[0][0][0]:
                via_options.append((small, float(default_cls.get("clearance", 0.125))))  # a tight spot takes the smaller via at the board's base clearance
            w, h = p.size
            if abs((p.angle % 180) - 90) < 1e-6:
                w, h = h, w
            width = min(float(cls.get("track_width", 0.5)), min(w, h))
            layer = "F.Cu" if ("F.Cu" in p.layers or "*.Cu" in p.layers) else next((l for l in layers if l in p.layers), "F.Cu")
            # candidate directions: along the long axis, then across; inward first for connectors, outward for small parts
            long_x = w >= h
            axis = [(1, 0), (-1, 0)] if long_x else [(0, 1), (0, -1)]
            cross = [(0, 1), (0, -1)] if long_x else [(1, 0), (-1, 0)]
            to_centre = (f.x - p.x, f.y - p.y)

            def inward(d):
                return d[0] * to_centre[0] + d[1] * to_centre[1]

            prefer_in = big and p.net in real_planes  # plane vias inside the connector; fan-out vias where the router can reach them
            axis.sort(key=lambda d: -inward(d) if prefer_in else inward(d))
            cross.sort(key=lambda d: -inward(d) if prefer_in else inward(d))
            placed = False
            reasons: list[str] = []
            for ((v_size, v_drill), clearance), d in [(vo, d_) for vo in via_options for d_ in axis + cross]:
                v_r = v_size / 2
                half = (w / 2 if d[0] else h / 2)
                for t in (0.55, 0.8, 1.1, 1.5, 2.0):
                    dist = half + t
                    vx, vy = p.x + d[0] * dist, p.y + d[1] * dist
                    if not (ox0 + edge + v_r <= vx <= ox1 - edge - v_r and oy0 + edge + v_r <= vy <= oy1 - edge - v_r):
                        continue
                    if any(kx0 - v_r - clearance <= vx <= kx1 + v_r + clearance and ky0 - v_r - clearance <= vy <= ky1 + v_r + clearance for kx0, ky0, kx1, ky1 in keepouts):
                        continue
                    far = (p.x + d[0] * half, p.y + d[1] * half)  # the stub copper outside the pad starts here
                    # a pad with a hole in it (a microphone port) takes the stub from its edge, not across the hole
                    own_holes = [r for o, r, ref in pads_all if o.kind == "np_thru_hole" and ref == f.ref and _rect_dist(p.x, p.y, r) <= max(w, h) / 2]
                    stub_from = (p.x + d[0] * max(0.0, half - 0.1), p.y + d[1] * max(0.0, half - 0.1)) if own_holes else (p.x, p.y)
                    stub_a, stub_b = stub_from, (vx, vy)
                    ok = True
                    reason = ""
                    for q, r, ref in pads_all:
                        if q is p:
                            continue
                        if q.net == p.net and q.kind != "np_thru_hole":
                            continue  # same net: touching is fine
                        if not q.net and q.kind == "smd" and ref == f.ref and _rect_dist(p.x, p.y, r) <= max(w, h) / 2:
                            continue  # a paste window or copper piece of our own exposed pad
                        need = v_r + clearance if q.kind != "np_thru_hole" else v_r + NPTH_GAP
                        if _rect_dist(vx, vy, r) < need:
                            ok, reason = False, f"via too close to {ref}-{q.number}"
                            break
                        gap = width / 2 + clearance if q.kind != "np_thru_hole" else width / 2 + NPTH_GAP
                        if _seg_rect_dist(far, stub_b, r) < gap - 1e-6:
                            ok, reason = False, f"stub too close to {ref}-{q.number}"
                            break
                    if not ok:
                        reasons.append(reason)
                        continue
                    for sl, a, b, sw, snet in segs:
                        if snet == p.net:
                            continue
                        c2 = max(clearance, net_clearance(snet)) if not snet.startswith("__") else clearance
                        if _seg_point_dist(a, b, (vx, vy)) < v_r + sw / 2 + c2:
                            ok, reason = False, f"via too close to a {snet} track"
                            break
                        if sl == layer and _seg_seg_dist(a, b, far, stub_b) < width / 2 + sw / 2 + c2:
                            ok, reason = False, f"stub too close to a {snet} track"
                            break
                    if not ok:
                        reasons.append(reason)
                        continue
                    for (qx, qy), qs, qnet in vias_all:
                        dd = math.dist((qx, qy), (vx, vy))
                        if dd < v_r + qs / 2 + (max(clearance, net_clearance(qnet)) if qnet != p.net else 0.0) or dd < v_drill + hole_gap:
                            ok, reason = False, f"via too close to a {qnet} via"
                            break
                        if qnet != p.net and _seg_point_dist(far, stub_b, (qx, qy)) < qs / 2 + width / 2 + max(clearance, net_clearance(qnet)):
                            ok, reason = False, f"stub too close to a {qnet} via"
                            break
                    if not ok:
                        reasons.append(reason)
                        continue
                    if coverage.via_forbidden(vx, vy, v_r):
                        reasons.append("via in a keep-out that forbids vias")
                        continue
                    if has_plane and not coverage.reaches(p.net, vx, vy, v_r + clearance, layer):
                        reasons.append(NO_PLANE)
                        continue
                    out.segments.append(RouteSegment(p.net, layer, width, round(stub_a[0], 4), round(stub_a[1], 4), round(vx, 4), round(vy, 4)))
                    out.vias.append(RouteVia(p.net, round(vx, 4), round(vy, 4), v_size, v_drill))
                    vias_all.append(((vx, vy), v_size, p.net))
                    segs.append((layer, stub_a, stub_b, width, p.net))
                    placed = True
                    break
                if placed:
                    break
            if not placed:
                # a large pad (an exposed pad) takes the via inside it, off any hole in the pad
                for (v_size, v_drill), clr in via_options:
                    if placed or min(w, h) < v_size + 0.3:
                        continue
                    holes = [(o, r) for o, r, ref in pads_all if o.kind == "np_thru_hole" and _rect_dist(p.x, p.y, r) <= max(w, h) / 2]
                    for ox, oy in ((0.0, 0.0), (w / 2 - v_size / 2 - 0.15, 0.0), (-(w / 2 - v_size / 2 - 0.15), 0.0), (0.0, h / 2 - v_size / 2 - 0.15), (0.0, -(h / 2 - v_size / 2 - 0.15))):
                        vx, vy = p.x + ox, p.y + oy
                        if any(_rect_dist(vx, vy, r) < v_size / 2 + NPTH_GAP for _, r in holes):
                            continue
                        if any(math.dist((qx, qy), (vx, vy)) < v_drill + hole_gap for (qx, qy), _, _ in vias_all):
                            continue
                        if coverage.via_forbidden(vx, vy, v_size / 2):
                            continue
                        if has_plane and not coverage.reaches(p.net, vx, vy, v_size / 2 + clr, layer):
                            reasons.append(NO_PLANE)
                            continue
                        out.vias.append(RouteVia(p.net, round(vx, 4), round(vy, 4), v_size, v_drill))
                        vias_all.append(((vx, vy), v_size, p.net))
                        placed = True
                        result.in_pad += 1
                        break
            if not placed:
                # last resort: a straight link to a same-net pad of the same footprint (an exposed pad to its GND pin)
                mates = [q for q in f.pads if q is not p and q.net == p.net and q.kind != "np_thru_hole"]
                mates.sort(key=lambda q: math.dist((q.x, q.y), (p.x, p.y)))
                for q in mates[:4]:
                    dq = (q.x - p.x, q.y - p.y)
                    lq = math.hypot(*dq) or 1.0
                    inset = max(0.0, min(w, h) / 2 - 0.1)
                    start = (p.x + dq[0] / lq * inset, p.y + dq[1] / lq * inset)  # inside the pad, near its edge toward the mate
                    link = (start, (q.x, q.y))
                    lw = min(width, min(q.size))
                    blocked = False
                    for o, r, ref in pads_all:
                        if o is p or o is q or (o.net == p.net and o.kind != "np_thru_hole"):
                            continue
                        if not o.net and o.kind == "smd" and ref == f.ref and (_rect_dist(p.x, p.y, r) <= max(w, h) / 2 or _rect_dist(q.x, q.y, r) <= max(q.size) / 2):
                            continue
                        gap = lw / 2 + clearance if o.kind != "np_thru_hole" else lw / 2 + NPTH_GAP
                        if _seg_rect_dist(link[0], link[1], r) < gap - 1e-6:
                            blocked = True
                            break
                    if not blocked:
                        for sl, a, b, sw, snet in segs:
                            if snet != p.net and sl == layer and _seg_seg_dist(a, b, link[0], link[1]) < lw / 2 + sw / 2 + clearance:
                                blocked = True
                                break
                    if not blocked:
                        out.segments.append(RouteSegment(p.net, layer, lw, round(start[0], 4), round(start[1], 4), round(q.x, 4), round(q.y, 4)))
                        segs.append((layer, link[0], link[1], lw, p.net))
                        placed = True
                        result.joined += 1
                        break
            if placed:
                result.stitched += 1
            else:
                top = Counter(reasons).most_common(2)
                result.skipped.append(f"{f.ref}-{p.number} {p.net}: " + "; ".join(f"{r_} x{n_}" for r_, n_ in top))
                if NO_PLANE in reasons:
                    result.rejected.append(f"{f.ref}-{p.number} {p.net}: {NO_PLANE} at any clear spot ({reasons.count(NO_PLANE)} candidates: a cut-out or another net's zone)")
    out.nets = {s.net for s in out.segments}
    return result
