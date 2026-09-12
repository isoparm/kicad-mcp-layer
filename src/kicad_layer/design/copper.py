"""Exact copper geometry of a built board, for questions about placing a part or adding copper.

The model holds every pad (circle, capsule, rectangle or rounded rectangle, on its layers), every
hole, via and track, and the board edge with its cut-outs, in a bucket index. Clearances come from
the project's net classes and rules, the way KiCad's DRC applies them: the larger of the two nets'
class clearances between copper, ``min_hole_clearance`` from copper to a hole, ``min_hole_to_hole``
between holes, ``min_copper_edge_clearance`` to the edge. Questions (``inspect`` answers them)::

    region X0 Y0 X1 Y1 [LAYER]             what is there: pads, tracks, vias, holes, edge, per layer
    free REF X Y [ROT]                      would footprint REF fit at (X, Y, ROT): courtyards, edge, copper under its pads
    spots REF X0 Y0 X1 Y1 [ROT]             the first places in the rectangle where REF fits (0.5 mm grid)
    clear NET LAYER X1 Y1 X2 Y2 [X3 Y3 ...] [--width W]   would this track keep every clearance; what it hits if not
    clear-via NET X Y [--size S --drill D]  the same for a via

A ``differ`` from KiCad's DRC is possible only where a pad is a trapezoid or a custom shape (taken as
its bounding rectangle, the safe side) or a zone fill is involved (planes keep their own clearance).
The distance functions are the clean-up prototype's of 2026-09-07, which agreed with DRC on a
whole board.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from kicad_layer.review import BoardModel, FpGeo, PadGeo
from kicad_layer.sexpr import child, children, parse, tag

CELL = 2.0
EPS = 1e-4


# ---------------------------------------------------------------- distances (mm)
def d_pt_seg(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _cross(ax, ay, bx, by, cx, cy) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def seg_intersect(a, b, c, d) -> bool:
    d1, d2 = _cross(*c, *d, *a), _cross(*c, *d, *b)
    d3, d4 = _cross(*a, *b, *c), _cross(*a, *b, *d)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def d_seg_seg(a, b, c, d) -> float:
    if seg_intersect(a, b, c, d):
        return 0.0
    return min(d_pt_seg(*a, *c, *d), d_pt_seg(*b, *c, *d), d_pt_seg(*c, *a, *b), d_pt_seg(*d, *a, *b))


def _to_rect_frame(p, cx: float, cy: float, angle: float) -> tuple[float, float]:
    r = math.radians(-angle)
    cr, sr = math.cos(r), math.sin(r)
    x, y = p[0] - cx, p[1] - cy
    return (x * cr - y * sr, x * sr + y * cr)


def d_pt_rect(p, cx: float, cy: float, w: float, h: float, angle: float) -> float:
    """Distance from a point to a w x h rectangle centred at (cx, cy), rotated by angle degrees; negative inside."""
    x, y = _to_rect_frame(p, cx, cy, angle)
    hw, hh = w / 2, h / 2
    dx, dy = abs(x) - hw, abs(y) - hh
    if dx <= 0 and dy <= 0:
        return max(dx, dy)  # inside: how deep
    return math.hypot(max(dx, 0.0), max(dy, 0.0))


def d_seg_rect(a, b, cx: float, cy: float, w: float, h: float, angle: float) -> float:
    """Distance from segment ab to the rectangle; 0 when they touch or cross."""
    a2, b2 = _to_rect_frame(a, cx, cy, angle), _to_rect_frame(b, cx, cy, angle)
    hw, hh = w / 2, h / 2
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]

    def inside(p) -> bool:
        return -hw <= p[0] <= hw and -hh <= p[1] <= hh

    if inside(a2) or inside(b2):
        return 0.0
    for k in range(4):
        if seg_intersect(a2, b2, corners[k], corners[(k + 1) % 4]):
            return 0.0

    def d_pt(p) -> float:
        dx = max(-hw - p[0], 0.0, p[0] - hw)
        dy = max(-hh - p[1], 0.0, p[1] - hh)
        return math.hypot(dx, dy)

    return min(min(d_pt_seg(*c, *a2, *b2) for c in corners), d_pt(a2), d_pt(b2))


def rotate(x: float, y: float, angle: float) -> tuple[float, float]:
    """KiCad's rotation: positive is counter-clockwise on screen, y down."""
    r = math.radians(angle)
    c, s = math.cos(r), math.sin(r)
    return (x * c + y * s, -x * s + y * c)


# ---------------------------------------------------------------- rules
@dataclass
class Rules:
    """What the project file says: net classes with their patterns, and the board-wide minimums."""

    classes: dict[str, dict] = field(default_factory=dict)
    patterns: list[tuple[str, str]] = field(default_factory=list)  # (class, pattern) in order
    edge: float = 0.5
    hole: float = 0.25
    hole_to_hole: float = 0.25
    default_clearance: float = 0.2
    _memo: dict = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, pro: Path | None) -> "Rules":
        r = cls()
        if pro is None or not pro.is_file():
            return r
        data = json.loads(pro.read_text(encoding="utf-8"))
        ns = data.get("net_settings", {})
        for c in ns.get("classes", []):
            r.classes[c.get("name", "Default")] = c
        r.patterns = [(p.get("netclass", "Default"), p.get("pattern", "")) for p in ns.get("netclass_patterns") or []]
        rules = data.get("board", {}).get("design_settings", {}).get("rules", {})
        r.edge = float(rules.get("min_copper_edge_clearance", r.edge))
        r.hole = float(rules.get("min_hole_clearance", r.hole))
        r.hole_to_hole = float(rules.get("min_hole_to_hole", r.hole_to_hole))
        d = r.classes.get("Default", {})
        r.default_clearance = float(d.get("clearance", rules.get("min_clearance", r.default_clearance)) or r.default_clearance)
        return r

    def netclass(self, net: str | None) -> dict:
        if net is None:
            return self.classes.get("Default", {})
        short = net.rsplit("/", 1)[-1]
        for name, pat in self.patterns:
            if fnmatch(net, pat) or fnmatch(short, pat):
                return self.classes.get(name, self.classes.get("Default", {}))
        return self.classes.get("Default", {})

    def clearance(self, net: str | None) -> float:
        if net not in self._memo:
            self._memo[net] = float(self.netclass(net).get("clearance", self.default_clearance) or self.default_clearance)
        return self._memo[net]

    def between(self, a: str | None, b: str | None) -> float:
        return max(self.clearance(a), self.clearance(b))

    def via(self, net: str | None) -> tuple[float, float]:
        c = self.netclass(net)
        return float(c.get("via_diameter", 0.6) or 0.6), float(c.get("via_drill", 0.3) or 0.3)

    def track(self, net: str | None) -> float:
        return float(self.netclass(net).get("track_width", 0.2) or 0.2)


# ---------------------------------------------------------------- the model
@dataclass
class Item:
    kind: str  # pad, hole, via, track, edge
    net: str | None
    layers: tuple[str, ...]
    shape: str  # circle | capsule | rect
    geom: tuple  # circle: (x, y); capsule: (ax, ay, bx, by); rect: (cx, cy, w, h, angle) of the core rectangle
    radius: float  # inflation of the core shape: the circle's radius, the capsule's half width, the rectangle's corner radius
    bbox: tuple[float, float, float, float]
    label: str
    ref: str = ""  # the footprint a pad or hole belongs to
    clearance_override: float = 0.0  # a pad's own clearance, 0 when it uses the class

    def distance_to_point(self, x: float, y: float) -> float:
        if self.shape == "circle":
            return math.hypot(x - self.geom[0], y - self.geom[1]) - self.radius
        if self.shape == "capsule":
            return d_pt_seg(x, y, *self.geom) - self.radius
        return d_pt_rect((x, y), *self.geom) - self.radius

    def distance_to_segment(self, a, b) -> float:
        if self.shape == "circle":
            return d_pt_seg(self.geom[0], self.geom[1], *a, *b) - self.radius
        if self.shape == "capsule":
            return d_seg_seg(a, b, self.geom[:2], self.geom[2:]) - self.radius
        return d_seg_rect(a, b, *self.geom) - self.radius


def copper_layers(n: int) -> tuple[str, ...]:
    return ("F.Cu", *[f"In{i}.Cu" for i in range(1, max(n, 2) - 1)], "B.Cu")


def pad_item(p: PadGeo, all_copper: tuple[str, ...], x: float | None = None, y: float | None = None, angle: float | None = None) -> Item | None:
    """The copper of a pad as an item, at its own place or moved to (x, y, angle)."""
    x = p.x if x is None else x
    y = p.y if y is None else y
    angle = p.angle if angle is None else angle
    if p.kind == "np_thru_hole":
        return None
    layers = all_copper if (p.kind != "smd" or any(l == "*.Cu" for l in p.layers)) else tuple(l for l in p.layers if l.endswith(".Cu"))
    if not layers:
        return None
    w, h = p.size
    label = f"{p.ref}.{p.number}"
    if p.shape == "circle":
        r = w / 2
        return Item("pad", p.net, layers, "circle", (x, y), r, (x - r, y - r, x + r, y + r), label, p.ref, p.clearance)
    if p.shape == "oval" and abs(w - h) > EPS:
        long_, short = max(w, h), min(w, h)
        half = (long_ - short) / 2
        dx, dy = rotate(half, 0.0, angle) if w >= h else rotate(0.0, half, angle)
        r = short / 2
        reach = math.hypot(dx, dy) + r
        return Item("pad", p.net, layers, "capsule", (x - dx, y - dy, x + dx, y + dy), r, (x - reach, y - reach, x + reach, y + reach), label, p.ref, p.clearance)
    rr = p.roundrect_ratio * min(w, h) if p.shape == "roundrect" else 0.0
    corners = [rotate(sx * w / 2, sy * h / 2, angle) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    bbox = (x + min(c[0] for c in corners), y + min(c[1] for c in corners), x + max(c[0] for c in corners), y + max(c[1] for c in corners))
    return Item("pad", p.net, layers, "rect", (x, y, w - 2 * rr, h - 2 * rr, angle), rr, bbox, label, p.ref, p.clearance)


def hole_item(p: PadGeo, all_copper: tuple[str, ...], x: float | None = None, y: float | None = None) -> Item | None:
    if not p.drill:
        return None
    x = p.x if x is None else x
    y = p.y if y is None else y
    r = p.drill / 2
    return Item("hole", p.net, all_copper, "circle", (x, y), r, (x - r, y - r, x + r, y + r), f"{p.ref}.{p.number} hole", p.ref)


def _arc_points(sx, sy, mx, my, ex, ey, step_deg: float = 6.0) -> list[tuple[float, float]]:
    """The arc through three points as a polyline."""
    ax, ay, bx, by, cx, cy = sx, sy, mx, my, ex, ey
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return [(sx, sy), (ex, ey)]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0, a1, am = (math.atan2(p[1] - uy, p[0] - ux) for p in ((ax, ay), (cx, cy), (bx, by)))

    def ccw(fr, to):
        return (to - fr) % (2 * math.pi)

    sweep = ccw(a0, a1)
    if ccw(a0, am) > sweep:  # the middle point is not on the counter-clockwise way: go the other way
        sweep = sweep - 2 * math.pi
    n = max(2, int(abs(math.degrees(sweep)) / step_deg) + 1)
    return [(ux + r * math.cos(a0 + sweep * i / n), uy + r * math.sin(a0 + sweep * i / n)) for i in range(n + 1)]


def edge_segments(pcb: Path) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Every Edge.Cuts line, arc (sampled), rectangle, circle and polygon edge of the board file."""
    root = parse(pcb.read_text(encoding="utf-8"))
    segs: list[tuple[tuple[float, float], tuple[float, float]]] = []

    def xy(node, name):
        c = child(node, name)
        return (float(c[1]), float(c[2])) if c is not None else None

    for g in root[1:]:
        if not isinstance(g, list):
            continue
        t = tag(g)
        if t not in ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly"):
            continue
        layer = child(g, "layer")
        if layer is None or layer[1] != "Edge.Cuts":
            continue
        if t == "gr_line":
            segs.append((xy(g, "start"), xy(g, "end")))
        elif t == "gr_arc":
            pts = _arc_points(*xy(g, "start"), *xy(g, "mid"), *xy(g, "end"))
            segs += list(zip(pts, pts[1:]))
        elif t == "gr_rect":
            (x0, y0), (x1, y1) = xy(g, "start"), xy(g, "end")
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
            segs += list(zip(pts, pts[1:]))
        elif t == "gr_circle":
            (cx, cy), (ex, ey) = xy(g, "center"), xy(g, "end")
            r = math.hypot(ex - cx, ey - cy)
            pts = [(cx + r * math.cos(2 * math.pi * i / 60), cy + r * math.sin(2 * math.pi * i / 60)) for i in range(61)]
            segs += list(zip(pts, pts[1:]))
        elif t == "gr_poly":
            pts_node = child(g, "pts")
            pts = [(float(p[1]), float(p[2])) for p in children(pts_node, "xy")] if pts_node is not None else []
            if pts:
                pts.append(pts[0])
                segs += list(zip(pts, pts[1:]))
    return segs


class Model:
    """The board's copper and edge in a bucket index, with the rules to judge candidates by."""

    def __init__(self, bm: BoardModel, rules: Rules, pcb: Path | None = None):
        self.bm = bm
        self.rules = rules
        self.copper = copper_layers(bm.copper_layers)
        self.items: list[Item] = []
        self.buckets: dict[tuple[int, int], list[Item]] = defaultdict(list)
        for f in bm.footprints:
            for p in f.pads:
                it = pad_item(p, self.copper)
                if it is not None:
                    self.add(it)
                h = hole_item(p, self.copper)
                if h is not None:
                    self.add(h)
        for v in bm.vias:
            r = v.size / 2
            self.add(Item("via", v.net, self.copper, "circle", (v.x, v.y), r, (v.x - r, v.y - r, v.x + r, v.y + r), "via"))
            r = v.drill / 2
            self.add(Item("hole", v.net, self.copper, "circle", (v.x, v.y), r, (v.x - r, v.y - r, v.x + r, v.y + r), "via hole"))
        for s in bm.segments:
            hw = s.width / 2
            self.add(Item("track", s.net, (s.layer,), "capsule", (s.x1, s.y1, s.x2, s.y2), hw,
                          (min(s.x1, s.x2) - hw, min(s.y1, s.y2) - hw, max(s.x1, s.x2) + hw, max(s.y1, s.y2) + hw), "track"))
        src = pcb or bm.path
        if src is not None and Path(src).is_file():
            self.edges = edge_segments(Path(src))
        elif bm.outline:  # a model built in memory: the outline rectangle is the edge
            x0, y0, x1, y1 = bm.outline
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
            self.edges = list(zip(pts, pts[1:]))
        else:
            self.edges = []
        for a, b in self.edges:
            self.add(Item("edge", None, self.copper, "capsule", (*a, *b), 0.0, (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), "edge"))
        self.plane_nets = {z.net for z in bm.zones if z.net}

    def add(self, it: Item) -> None:
        self.items.append(it)
        x0, y0, x1, y1 = it.bbox
        for i in range(int(x0 // CELL), int(x1 // CELL) + 1):
            for j in range(int(y0 // CELL), int(y1 // CELL) + 1):
                self.buckets[(i, j)].append(it)

    def add_segment(self, net: str | None, layer: str, a, b, width: float) -> None:
        """New copper the next candidates must respect."""
        hw = width / 2
        self.add(Item("track", net, (layer,), "capsule", (a[0], a[1], b[0], b[1]), hw,
                      (min(a[0], b[0]) - hw, min(a[1], b[1]) - hw, max(a[0], b[0]) + hw, max(a[1], b[1]) + hw), "track"))

    def add_via(self, net: str | None, x: float, y: float, size: float, drill: float) -> None:
        r = size / 2
        self.add(Item("via", net, self.copper, "circle", (x, y), r, (x - r, y - r, x + r, y + r), "via"))
        r = drill / 2
        self.add(Item("hole", net, self.copper, "circle", (x, y), r, (x - r, y - r, x + r, y + r), "via hole"))

    def near(self, bbox: tuple[float, float, float, float], layer: str | None = None) -> list[Item]:
        x0, y0, x1, y1 = bbox
        seen: set[int] = set()
        out: list[Item] = []
        for i in range(int(x0 // CELL), int(x1 // CELL) + 1):
            for j in range(int(y0 // CELL), int(y1 // CELL) + 1):
                for it in self.buckets.get((i, j), ()):
                    if id(it) in seen:
                        continue
                    seen.add(id(it))
                    if layer is not None and layer not in it.layers:
                        continue
                    bx0, by0, bx1, by1 = it.bbox
                    if bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1:
                        continue
                    out.append(it)
        return out

    # ------------------------------------------------------------ what a candidate must keep from an item
    def required(self, net: str | None, it: Item, *, candidate_is_hole: bool = False) -> float | None:
        """The clearance the candidate of ``net`` owes ``it``, or None when none applies (same net)."""
        if it.kind == "edge":
            return self.rules.edge
        if candidate_is_hole:
            if it.kind == "hole":
                return self.rules.hole_to_hole
            return None if it.net == net else self.rules.hole
        if it.kind == "hole":
            return None if it.net == net else self.rules.hole
        if it.net == net and net is not None:
            return None
        return max(self.rules.between(net, it.net), it.clearance_override)

    def check_segment(self, net: str | None, layer: str, a, b, width: float, exclude_refs: tuple[str, ...] = ()) -> list[tuple[float, float, Item]]:
        """(distance, required, item) for every item the track a-b of ``net`` on ``layer`` would violate."""
        hw = width / 2
        reach = hw + 1.0
        bbox = (min(a[0], b[0]) - reach, min(a[1], b[1]) - reach, max(a[0], b[0]) + reach, max(a[1], b[1]) + reach)
        out = []
        for it in self.near(bbox, layer):
            if it.ref and it.ref in exclude_refs:
                continue
            req = self.required(net, it)
            if req is None:
                continue
            d = it.distance_to_segment(a, b) - hw
            if d < req - EPS:
                out.append((d, req, it))
        return sorted(out, key=lambda t: t[0] - t[1])

    def check_circle(self, net: str | None, layers: tuple[str, ...], x: float, y: float, r: float, *, is_hole: bool = False,
                     exclude_refs: tuple[str, ...] = ()) -> list[tuple[float, float, Item]]:
        """(distance, required, item) for every item a disc of radius r (copper or a hole) would violate on ``layers``."""
        reach = r + 1.0
        out = []
        for it in self.near((x - reach, y - reach, x + reach, y + reach)):
            if it.ref and it.ref in exclude_refs:
                continue
            if not (set(layers) & set(it.layers)):
                continue
            req = self.required(net, it, candidate_is_hole=is_hole)
            if req is None:
                continue
            d = it.distance_to_point(x, y) - r
            if d < req - EPS:
                out.append((d, req, it))
        return sorted(out, key=lambda t: t[0] - t[1])

    def check_rect(self, net: str | None, layers: tuple[str, ...], cx, cy, w, h, angle, rr: float, exclude_refs: tuple[str, ...] = ()) -> list[tuple[float, float, Item]]:
        """(distance, required, item) for a rounded rectangle placed at (cx, cy, angle): ``w`` and ``h`` are the core, ``rr`` the corner radius around it."""
        reach = math.hypot(w + 2 * rr, h + 2 * rr) / 2 + 1.0
        out = []
        for it in self.near((cx - reach, cy - reach, cx + reach, cy + reach)):
            if it.ref and it.ref in exclude_refs:
                continue
            if not (set(layers) & set(it.layers)):
                continue
            req = self.required(net, it)
            if req is None:
                continue
            d = _rect_to_item(cx, cy, w, h, angle, it) - rr
            if d < req - EPS:
                out.append((d, req, it))
        return sorted(out, key=lambda t: t[0] - t[1])

    def inside_outline(self, x: float, y: float) -> bool:
        """Ray test against the edge segments (cut-outs count as outside)."""
        inside = False
        for (ax, ay), (bx, by) in self.edges:
            if (ay > y) != (by > y):
                xi = ax + (y - ay) * (bx - ax) / (by - ay)
                if xi > x:
                    inside = not inside
        return inside


def _rect_to_item(cx, cy, w, h, angle, it: Item) -> float:
    """Distance from a core rectangle to an item's core shape, minus the item's inflation."""
    corners = [(cx + dx, cy + dy) for dx, dy in (rotate(sx * w / 2, sy * h / 2, angle) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)))]
    sides = list(zip(corners, corners[1:] + corners[:1]))
    if it.shape == "circle":
        x, y = it.geom
        d = d_pt_rect((x, y), cx, cy, w, h, angle)
        return (d if d > 0 else 0.0) - it.radius
    if it.shape == "capsule":
        a, b = it.geom[:2], it.geom[2:]
        return d_seg_rect(a, b, cx, cy, w, h, angle) - it.radius
    # rectangle against rectangle: zero when a corner of one is inside the other or sides cross, else the closest sides
    ocx, ocy, ow, oh, oang = it.geom
    ocorners = [(ocx + dx, ocy + dy) for dx, dy in (rotate(sx * ow / 2, sy * oh / 2, oang) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)))]
    osides = list(zip(ocorners, ocorners[1:] + ocorners[:1]))
    if any(d_pt_rect(c, ocx, ocy, ow, oh, oang) <= 0 for c in corners) or any(d_pt_rect(c, cx, cy, w, h, angle) <= 0 for c in ocorners):
        return -it.radius
    best = min(d_seg_seg(a, b, c, d) for a, b in sides for c, d in osides)
    return best - it.radius


# ---------------------------------------------------------------- the questions, as lines
def _fmt_item(it: Item) -> str:
    if it.kind == "pad":
        return f"pad {it.label} ({it.net or 'no net'})"
    if it.kind == "track":
        ax, ay, bx, by = it.geom
        return f"track {it.net or 'no net'} ({ax:.2f},{ay:.2f})-({bx:.2f},{by:.2f})"
    if it.kind in ("via", "hole"):
        return f"{it.label} ({it.net or 'no net'}) at ({it.geom[0]:.2f},{it.geom[1]:.2f})"
    return it.kind


def _worst_per_item(v: list[tuple[float, float, Item]]) -> list[tuple[float, float, Item]]:
    """One line per item: the check (copper ring or hole) that misses by most."""
    best: dict[int, tuple[float, float, Item]] = {}
    for d, req, it in v:
        cur = best.get(id(it))
        if cur is None or d - req < cur[0] - cur[1]:
            best[id(it)] = (d, req, it)
    return sorted(best.values(), key=lambda t: t[0] - t[1])


def region(model: Model, x0: float, y0: float, x1: float, y1: float, layer: str | None = None, limit: int = 80) -> list[str]:
    """Everything in the rectangle, one line each, pads first."""
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    items = model.near((x0, y0, x1, y1), layer)
    order = {"pad": 0, "hole": 1, "via": 2, "track": 3, "edge": 4}
    items.sort(key=lambda it: (order.get(it.kind, 9), it.label, it.bbox))
    lines = []
    n_edge = 0
    for it in items:
        if it.kind == "edge":
            n_edge += 1
            continue
        lay = "/".join(it.layers) if len(it.layers) <= 2 else "all"
        if it.shape == "circle":
            g = f"({it.geom[0]:.2f},{it.geom[1]:.2f}) d{2 * it.radius:.2f}"
        elif it.shape == "capsule":
            g = f"({it.geom[0]:.2f},{it.geom[1]:.2f})-({it.geom[2]:.2f},{it.geom[3]:.2f}) w{2 * it.radius:.2f}"
        else:
            cx, cy, w, h, ang = it.geom
            g = f"({cx:.2f},{cy:.2f}) {w + 2 * it.radius:.2f}x{h + 2 * it.radius:.2f}" + (f" rot {ang:g}" if ang else "")
        lines.append(f"{it.kind:5s} {lay:9s} {it.net or '-':24s} {it.label:12s} {g}")
    head = f"{len(lines)} items in ({x0}, {y0})-({x1}, {y1})" + (f" on {layer}" if layer else "") + (f", {n_edge} edge segments" if n_edge else "")
    if len(lines) > limit:
        return [head, *lines[:limit], f"... {len(lines) - limit} more; narrow the rectangle or give a layer"]
    return [head, *lines]


def _violation_lines(v: list[tuple[float, float, Item]], limit: int = 8) -> list[str]:
    return [f"   {d:6.3f} mm to {_fmt_item(it)}, needs {req:.3f}" for d, req, it in v[:limit]] + ([f"   ... {len(v) - limit} more"] if len(v) > limit else [])


def clear(model: Model, net: str, layer: str, pts: list[tuple[float, float]], width: float | None = None) -> list[str]:
    """Would the polyline of ``net`` on ``layer`` keep every clearance."""
    if layer not in model.copper:
        return [f"{layer} is not a copper layer of this board ({', '.join(model.copper)})"]
    width = width or model.rules.track(net)
    out = [f"track {net} on {layer}, width {width:g}, {len(pts) - 1} segment(s)"]
    worst: tuple[float, float, Item] | None = None
    bad = 0
    for k, (a, b) in enumerate(zip(pts, pts[1:]), start=1):
        for p in (a, b):
            if not model.inside_outline(*p):
                out.append(f"   segment {k}: ({p[0]}, {p[1]}) is outside the board outline")
                bad += 1
        v = model.check_segment(net, layer, a, b, width)
        if v:
            bad += 1
            out.append(f"   segment {k} ({a[0]}, {a[1]})-({b[0]}, {b[1]}) violates:")
            out += _violation_lines(v)
        else:
            close = _closest(model, net, layer, a, b, width)
            if close and (worst is None or close[0] - close[1] < worst[0] - worst[1]):
                worst = close
    if bad:
        out.insert(1, f"NO: {bad} segment(s) violate a clearance")
    else:
        margin = f"; closest: {worst[0]:.3f} mm to {_fmt_item(worst[2])} (needs {worst[1]:.3f})" if worst else ""
        out.insert(1, "OK: every clearance kept" + margin)
    return out


def _closest(model: Model, net: str | None, layer: str, a, b, width: float) -> tuple[float, float, Item] | None:
    hw = width / 2
    reach = hw + 1.0
    best = None
    for it in model.near((min(a[0], b[0]) - reach, min(a[1], b[1]) - reach, max(a[0], b[0]) + reach, max(a[1], b[1]) + reach), layer):
        req = model.required(net, it)
        if req is None:
            continue
        d = it.distance_to_segment(a, b) - hw
        if best is None or d - req < best[0] - best[1]:
            best = (d, req, it)
    return best


def clear_via(model: Model, net: str, x: float, y: float, size: float | None = None, drill: float | None = None) -> list[str]:
    """Would a via of ``net`` at (x, y) keep every clearance, copper ring and hole both."""
    dsize, ddrill = model.rules.via(net)
    size, drill = size or dsize, drill or ddrill
    out = [f"via {net} at ({x}, {y}), {size:g}/{drill:g}"]
    if not model.inside_outline(x, y):
        return out + ["NO: outside the board outline"]
    v = model.check_circle(net, model.copper, x, y, size / 2) + model.check_circle(net, model.copper, x, y, drill / 2, is_hole=True)
    if v:
        return out + ["NO: violates"] + _violation_lines(_worst_per_item(v))
    return out + ["OK: every clearance kept"]


def _moved(fp: FpGeo, x: float, y: float, rot: float | None):
    """The footprint's pads, holes and courtyard at (x, y, rot)."""
    rot = fp.rotation if rot is None else rot
    delta = rot - fp.rotation
    pads = []
    for p in fp.pads:
        dx, dy = rotate(p.x - fp.x, p.y - fp.y, delta)
        pads.append((p, x + dx, y + dy, p.angle + delta))
    court = None
    if fp.courtyard:
        cx0, cy0, cx1, cy1 = fp.courtyard
        pts = [rotate(px - fp.x, py - fp.y, delta) for px, py in ((cx0, cy0), (cx1, cy0), (cx1, cy1), (cx0, cy1))]
        court = (x + min(p[0] for p in pts), y + min(p[1] for p in pts), x + max(p[0] for p in pts), y + max(p[1] for p in pts))
    return pads, court


def free(model: Model, ref: str, x: float, y: float, rot: float | None = None, *, quiet: bool = False) -> list[str]:
    """Would footprint ``ref`` fit at (x, y, rot): its courtyard against the others and the edge, its pads and holes against the copper."""
    fp = next((f for f in model.bm.footprints if f.ref == ref), None)
    if fp is None:
        return [f"no footprint {ref} on the board"]
    pads, court = _moved(fp, x, y, rot)
    problems: list[str] = []
    if court:
        cx0, cy0, cx1, cy1 = court
        for other in model.bm.footprints:
            if other.ref == ref or not other.courtyard:
                continue
            ox0, oy0, ox1, oy1 = other.courtyard
            if cx0 < ox1 and ox0 < cx1 and cy0 < oy1 and oy0 < cy1:
                problems.append(f"courtyard overlaps {other.ref}")
        corners = [(cx0, cy0), (cx1, cy0), (cx1, cy1), (cx0, cy1)]
        if not all(model.inside_outline(*c) for c in corners) or any(seg_intersect(a, b, c, d) for a, b in model.edges for c, d in zip(corners, corners[1:] + corners[:1])):
            problems.append("courtyard crosses the board edge or a cut-out")
    for p, px, py, pang in pads:
        it = pad_item(p, model.copper, px, py, pang)
        if it is not None:
            if it.shape == "rect":
                v = model.check_rect(p.net, it.layers, *it.geom, it.radius, exclude_refs=(ref,))
            elif it.shape == "circle":
                v = model.check_circle(p.net, it.layers, px, py, it.radius, exclude_refs=(ref,))
            else:
                ax, ay, bx, by = it.geom
                v = [(d, r, i) for d, r, i in model.check_segment(p.net, it.layers[0], (ax, ay), (bx, by), 2 * it.radius, exclude_refs=(ref,))]
            for d, req, other in v[:4]:
                problems.append(f"pad {p.number} ({p.net or 'no net'}) {d:.3f} mm to {_fmt_item(other)}, needs {req:.3f}")
        h = hole_item(p, model.copper, px, py)
        if h is not None:
            for d, req, other in model.check_circle(p.net, model.copper, px, py, h.radius, is_hole=True, exclude_refs=(ref,))[:4]:
                problems.append(f"pad {p.number} hole {d:.3f} mm to {_fmt_item(other)}, needs {req:.3f}")
    head = f"{ref} at ({x}, {y}) rot {fp.rotation if rot is None else rot:g}"
    if problems:
        return [f"{head}: NO", *[f"   {p}" for p in problems[:10]]] if not quiet else [f"{head}: NO"]
    return [f"{head}: fits"]


def spots(model: Model, ref: str, x0: float, y0: float, x1: float, y1: float, rot: float | None = None, step: float = 0.5, n: int = 5) -> list[str]:
    """The first ``n`` places on a ``step`` grid in the rectangle where ``ref`` fits, nearest the rectangle's centre first."""
    fp = next((f for f in model.bm.footprints if f.ref == ref), None)
    if fp is None:
        return [f"no footprint {ref} on the board"]
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    grid = [(x0 + i * step, y0 + j * step) for i in range(int((x1 - x0) / step) + 1) for j in range(int((y1 - y0) / step) + 1)]
    grid.sort(key=lambda p: math.hypot(p[0] - cx, p[1] - cy))
    found: list[tuple[float, float]] = []
    tried = 0
    for x, y in grid:
        tried += 1
        if free(model, ref, round(x, 3), round(y, 3), rot, quiet=True)[0].endswith("fits"):
            found.append((round(x, 3), round(y, 3)))
            if len(found) >= n:
                break
    r = fp.rotation if rot is None else rot
    if not found:
        return [f"{ref} rot {r:g}: no place fits in ({x0}, {y0})-({x1}, {y1}) on a {step} mm grid ({tried} tried)"]
    return [f"{ref} rot {r:g}: {len(found)} place(s) fit, nearest the centre first ({tried} tried)"] + [f"   ({x}, {y})" for x, y in found]


def load(board: Path, pro: Path | None) -> Model:
    from kicad_layer.review import load_board

    return Model(load_board(board), Rules.load(pro), board)
