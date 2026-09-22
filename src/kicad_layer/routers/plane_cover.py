"""Where a plane has copper: the acceptance test for a stitching via.

A via down to a plane only connects where the plane has copper under it. Planes have cut-outs, and a
zone of another net on the same layer (a -9V island in the GND layer) takes its area away. With the
zones filled in the file, the fill of the via's net is the truth; unfilled, the zone outlines are the
best guess: the net's outline minus the outlines of other nets' zones of the same or higher priority
on that layer and minus keep-outs that forbid copper pours. Keep-outs that forbid vias refuse a via
on any layer, since a through via crosses them all.

Polygons are rings of points. A fill that KiCad fractured into one outline (holes joined to the outer
ring by zero-width slits) is read correctly: the two edges of a slit cancel.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..review import BoardModel, ZoneGeo

Pt = tuple[float, float]


def _d_pt_seg(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 < 1e-18 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + dx * t), py - (ay + dy * t))


class Region:
    """An even-odd region of rings, with its edges bucketed on a grid for fast point and disc tests."""

    def __init__(self, rings: list[list[Pt]], cell: float = 2.0) -> None:
        directed: dict[tuple[Pt, Pt], int] = {}
        for ring in rings:
            n = len(ring)
            for i in range(n):
                a = (round(ring[i][0], 4), round(ring[i][1], 4))
                b = (round(ring[(i + 1) % n][0], 4), round(ring[(i + 1) % n][1], 4))
                if a == b:
                    continue
                if directed.get((b, a)):
                    directed[(b, a)] -= 1  # the other side of a fracture slit: both edges go
                else:
                    directed[(a, b)] = directed.get((a, b), 0) + 1
        self.edges: list[tuple[float, float, float, float]] = [(a[0], a[1], b[0], b[1]) for (a, b), k in directed.items() for _ in range(k)]
        self.cell = cell
        self.grid: dict[tuple[int, int], list[int]] = {}
        self.rows: dict[int, list[int]] = {}
        if self.edges:
            xs = [c for e in self.edges for c in (e[0], e[2])]
            ys = [c for e in self.edges for c in (e[1], e[3])]
            self.bbox = (min(xs), min(ys), max(xs), max(ys))
        else:
            self.bbox = (0.0, 0.0, 0.0, 0.0)
        for k, (ax, ay, bx, by) in enumerate(self.edges):
            i0, i1 = math.floor(min(ax, bx) / cell), math.floor(max(ax, bx) / cell)
            j0, j1 = math.floor(min(ay, by) / cell), math.floor(max(ay, by) / cell)
            for j in range(j0, j1 + 1):
                self.rows.setdefault(j, []).append(k)
                for i in range(i0, i1 + 1):
                    self.grid.setdefault((i, j), []).append(k)

    def __bool__(self) -> bool:
        return bool(self.edges)

    def contains(self, x: float, y: float) -> bool:
        if not self.edges or not (self.bbox[0] <= x <= self.bbox[2] and self.bbox[1] <= y <= self.bbox[3]):
            return False
        inside = False
        for k in self.rows.get(math.floor(y / self.cell), ()):
            ax, ay, bx, by = self.edges[k]
            if (ay > y) != (by > y) and ax + (y - ay) * (bx - ax) / (by - ay) > x:
                inside = not inside
        return inside

    def edge_within(self, x: float, y: float, r: float) -> bool:
        c = self.cell
        seen: set[int] = set()
        for i in range(math.floor((x - r) / c), math.floor((x + r) / c) + 1):
            for j in range(math.floor((y - r) / c), math.floor((y + r) / c) + 1):
                for k in self.grid.get((i, j), ()):
                    if k in seen:
                        continue
                    seen.add(k)
                    if _d_pt_seg(x, y, *self.edges[k]) < r:
                        return True
        return False

    def covers(self, x: float, y: float, r: float) -> bool:
        """The disc of radius r about (x, y) lies wholly inside."""
        return self.contains(x, y) and not self.edge_within(x, y, r)

    def touches(self, x: float, y: float, r: float) -> bool:
        """The disc of radius r about (x, y) overlaps the region."""
        if not self.edges or x + r < self.bbox[0] or x - r > self.bbox[2] or y + r < self.bbox[1] or y - r > self.bbox[3]:
            return False
        return self.contains(x, y) or self.edge_within(x, y, r)


def on_layer(zone_layers: list[str], layer: str) -> bool:
    for zl in zone_layers:
        if zl == layer or zl == "*.Cu" or (zl == "F&B.Cu" and layer in ("F.Cu", "B.Cu")) or (zl == "*.In.Cu" and layer.startswith("In")):
            return True
    return False


@dataclass
class _Source:
    layer: str
    copper: Region
    blockers: list[Region] = field(default_factory=list)
    filled: bool = True


class PlaneCoverage:
    """Per plane net, where a via reaches its plane on a layer other than the pad's."""

    def __init__(self, bm: BoardModel, plane_layers: dict[str, str] | None = None) -> None:
        self.warnings: list[str] = []
        self.unfilled = False
        copper = [z for z in bm.zones if not z.rule_area and z.net]
        areas = [z for z in bm.zones if z.rule_area]
        self.via_keepouts = [Region(z.outlines) for z in areas if z.keepout.get("vias")]
        self.sources: dict[str, list[_Source]] = {}
        layers_for: dict[str, set[str]] = {}
        for layer, net in (plane_layers or {}).items():
            layers_for.setdefault(net, set()).add(layer)
        for z in copper:
            wanted = layers_for.get(z.net)
            for layer in self._zone_layers(z, bm):
                if wanted is not None and layer not in wanted:
                    continue
                fill = z.fills.get(layer)
                if fill:
                    src = _Source(layer, Region(fill))
                else:
                    self.unfilled = True
                    blockers = [Region(o.outlines) for o in copper if o is not z and o.net != z.net and on_layer(o.layers, layer) and o.priority >= z.priority]
                    blockers += [Region(a.outlines) for a in areas if a.keepout.get("copperpour") and on_layer(a.layers, layer)]
                    src = _Source(layer, Region(z.outlines), blockers, filled=False)
                self.sources.setdefault(z.net, []).append(src)
        for net, layers in layers_for.items():
            have = {s.layer for s in self.sources.get(net, [])}
            for layer in sorted(layers - have):
                if bm.outline is None:
                    continue
                x0, y0, x1, y1 = bm.outline
                self.sources.setdefault(net, []).append(_Source(layer, Region([[(x0, y0), (x1, y0), (x1, y1), (x0, y1)]])))
                self.warnings.append(f"plane_layers names {layer} for {net} but the board has no {net} zone there; the layer is taken as a solid plane")
        if self.unfilled:
            self.warnings.append("zones are unfilled; fill first for exact results (vias were checked against the zone outlines)")

    @staticmethod
    def _zone_layers(z: ZoneGeo, bm: BoardModel) -> list[str]:
        out: list[str] = []
        for zl in z.layers:
            if zl.endswith(".Cu") and "*" not in zl and "&" not in zl:
                out.append(zl)
            else:
                out += [l for l in z.fills if l not in out]
        return out

    @property
    def nets(self) -> set[str]:
        return set(self.sources)

    def via_forbidden(self, x: float, y: float, r: float) -> bool:
        return any(k.touches(x, y, r) for k in self.via_keepouts)

    def reaches(self, net: str, x: float, y: float, r: float, pad_layer: str) -> bool:
        """A via of radius r at (x, y) lands on ``net``'s plane copper on some layer other than ``pad_layer``."""
        for s in self.sources.get(net, ()):
            if s.layer == pad_layer:
                continue
            if s.copper.covers(x, y, r) and not any(b.touches(x, y, r) for b in s.blockers):
                return True
        return False
