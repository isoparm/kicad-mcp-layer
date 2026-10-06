"""Questions about a board file that DRC answers only in part.

* ``copper_query``  would a track, via or part fit here; what is in this rectangle (the design package's copper
  model, exposed as a tool: clearances from the project's net classes and ``.kicad_dru``).
* ``zone_islands``  which pieces of a net's copper are joined, and which zone fill islands hold pads the rest of
  the net never reaches. DRC says "unconnected"; this says which island and what is on it.
* ``parity``        board against schematic pin by pin: a symbol pin with no pad (``S1`` against ``SH``), a pad no
  pin names, a net that differs. KiCad's parity check reports "net_conflict" without the pin.
* ``plot``          a 2-D plot of copper, pads, vias and fills in a region, nets highlighted, to look at a spot
  without a 3-D render.

All read the board file; none writes it.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from pathlib import Path

from kicad_layer.errors import INVALID_ARGUMENT, LayerError
from kicad_layer.geometry import FillIndex, Ring, sample_segment
from kicad_layer.models import CopperQuery, IslandGroup, NetIslands, ParityIssue, ParityReport, ZoneIslands
from kicad_layer.paths import display, locate_project
from kicad_layer.review import BoardModel, load_board


def _pro_for(board: Path) -> Path | None:
    cand = board.with_suffix(".kicad_pro")
    if cand.is_file():
        return cand
    proj = locate_project(board)
    return proj.project_file


# ---------------------------------------------------------------- copper questions
def copper_query(board: Path, question: str, *, net: str | None = None, layer: str | None = None, points: list[list[float]] | None = None,
                 width: float | None = None, x: float | None = None, y: float | None = None, size: float | None = None, drill: float | None = None,
                 ref: str | None = None, rotation: float | None = None, rect: list[float] | None = None, step: float = 0.5, n: int = 5) -> CopperQuery:
    from kicad_layer.design import copper

    pro = _pro_for(board)
    model = copper.load(board, pro)
    rules = f"net classes from {pro.name}" if pro else "KiCad defaults (no .kicad_pro next to the board)"
    if pro is not None and pro.with_suffix(".kicad_dru").is_file():
        rules += f" and rules from {pro.with_suffix('.kicad_dru').name}"
    if question == "region":
        if not rect or len(rect) != 4:
            raise LayerError(INVALID_ARGUMENT, "region needs rect [x0, y0, x1, y1].")
        lines = copper.region(model, *rect, layer=layer)
        ok = True
    elif question == "clear":
        if not (net and layer and points and len(points) >= 2):
            raise LayerError(INVALID_ARGUMENT, "clear needs net, layer and at least two points.")
        lines = copper.clear(model, net, layer, [(float(p[0]), float(p[1])) for p in points], width)
        ok = len(lines) > 1 and lines[1].startswith("OK")
    elif question == "clear_via":
        if net is None or x is None or y is None:
            raise LayerError(INVALID_ARGUMENT, "clear_via needs net, x and y.")
        lines = copper.clear_via(model, net, x, y, size, drill)
        ok = lines[-1].startswith("OK")
    elif question == "free":
        if ref is None or x is None or y is None:
            raise LayerError(INVALID_ARGUMENT, "free needs ref, x and y.")
        lines = copper.free(model, ref, x, y, rotation)
        ok = lines[0].endswith("fits")
    elif question == "spots":
        if ref is None or not rect or len(rect) != 4:
            raise LayerError(INVALID_ARGUMENT, "spots needs ref and rect [x0, y0, x1, y1].")
        lines = copper.spots(model, ref, *rect, rot=rotation, step=step, n=n)
        ok = "no place fits" not in lines[0]
    else:
        raise LayerError(INVALID_ARGUMENT, f"unknown question {question!r}")
    return CopperQuery(question=question, board=display(board), ok=ok, lines=lines, rules=rules)  # type: ignore[arg-type]


# ---------------------------------------------------------------- zone islands
class _UF:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, i: int) -> int:
        while self.p[i] != i:
            self.p[i] = self.p[self.p[i]]
            i = self.p[i]
        return i

    def join(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def _vertex_buckets(ring: Ring, cell: float = 1.0) -> dict[tuple[int, int], list[tuple[float, float]]]:
    out: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    for x, y in ring.points:
        out[(int(x // cell), int(y // cell))].append((x, y))
    return out


def zone_islands(board: Path, nets: list[str] | None = None) -> ZoneIslands:
    """For every net with a filled zone: its copper in connected groups (fill polygons, pads, vias, tracks)."""
    from kicad_layer.design.copper import copper_layers, pad_item

    bm = load_board(board)
    warnings: list[str] = []
    if any(z.fill_requested and not z.filled for z in bm.zones):
        warnings.append("Some zones are unfilled in the file: refill them (pcb_refill_zones) first, or their copper is missing here.")
    copper = copper_layers(bm.copper_layers)
    fills = FillIndex.from_zones(bm.zones)
    zone_nets = sorted({n for (n, _l) in fills.rings if n} if nets is None else set(nets))
    out: list[NetIslands] = []
    dead = 0
    for net in zone_nets:
        nodes: list[tuple[str, object, tuple[str, ...], str]] = []  # kind, geometry, layers, label
        for (n, layer), rings in fills.rings.items():
            if n == net:
                for r in rings:
                    nodes.append(("ring", r, (layer,), f"fill {layer}"))
        pads = [(p, pad_item(p, copper)) for fp in bm.footprints for p in fp.pads if p.net == net]
        for p, it in pads:
            if it is not None:
                nodes.append(("pad", it, it.layers, f"{p.ref}.{p.number}"))
        for v in bm.vias:
            if v.net == net:
                nodes.append(("via", (v.x, v.y, v.size / 2), tuple(copper), "via"))
        for s in bm.segments:
            if s.net == net:
                nodes.append(("track", s, (s.layer,), "track"))
        uf = _UF(len(nodes))
        rings = [(i, nd) for i, nd in enumerate(nodes) if nd[0] == "ring"]
        buckets = {i: _vertex_buckets(nd[1]) for i, nd in rings}  # type: ignore[arg-type]

        def touches_ring(i: int, ring: Ring, pts: list[tuple[float, float]], reach: float, dist) -> bool:
            if any(ring.contains(x, y) for x, y in pts):
                return True
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            for cx in range(int((min(xs) - reach) // 1.0), int((max(xs) + reach) // 1.0) + 1):
                for cy in range(int((min(ys) - reach) // 1.0), int((max(ys) + reach) // 1.0) + 1):
                    for vx, vy in buckets[i].get((cx, cy), ()):
                        if dist(vx, vy) <= 0.02:
                            return True
            return False

        for j, nd in enumerate(nodes):
            kind = nd[0]
            if kind == "ring":
                continue
            for i, rn in rings:
                if not set(rn[2]) & set(nd[2]):
                    continue
                ring: Ring = rn[1]  # type: ignore[assignment]
                if kind == "pad":
                    it = nd[1]
                    x0, y0, x1, y1 = it.bbox  # type: ignore[attr-defined]
                    if x1 < ring.bbox[0] or x0 > ring.bbox[2] or y1 < ring.bbox[1] or y0 > ring.bbox[3]:
                        continue
                    pts = [((x0 + x1) / 2, (y0 + y1) / 2), (x0, y0), (x1, y1)]
                    if touches_ring(i, ring, pts[:1], max(x1 - x0, y1 - y0), it.distance_to_point):  # type: ignore[attr-defined]
                        uf.join(i, j)
                elif kind == "via":
                    vx, vy, r = nd[1]  # type: ignore[misc]
                    if touches_ring(i, ring, [(vx, vy)], r, lambda a, b: math.hypot(a - vx, b - vy) - r):
                        uf.join(i, j)
                else:
                    s = nd[1]
                    pts = sample_segment((s.x1, s.y1), (s.x2, s.y2), 0.5)  # type: ignore[attr-defined]
                    if any(ring.contains(x, y) for x, y in pts):
                        uf.join(i, j)
        # copper to copper
        from kicad_layer.design.copper import d_pt_seg, d_seg_seg

        others = [(j, nd) for j, nd in enumerate(nodes) if nd[0] != "ring"]
        for a in range(len(others)):
            ja, na = others[a]
            for b in range(a + 1, len(others)):
                jb, nb = others[b]
                if not set(na[2]) & set(nb[2]):
                    continue
                if _copper_touch(na, nb, d_pt_seg, d_seg_seg):
                    uf.join(ja, jb)
        groups: dict[int, list[int]] = defaultdict(list)
        for i in range(len(nodes)):
            groups[uf.find(i)].append(i)
        made: list[IslandGroup] = []
        for members in groups.values():
            pad_labels = sorted(nodes[i][3] for i in members if nodes[i][0] == "pad")
            rs = [nodes[i] for i in members if nodes[i][0] == "ring"]
            area = sum(r[1].area for r in rs)  # type: ignore[attr-defined]
            if rs and not pad_labels and not any(nodes[i][0] in ("via", "track") for i in members):
                dead += len(rs)
            first = rs[0][1].points[0] if rs else None  # type: ignore[attr-defined]
            if first is None and pad_labels:
                pi = next(i for i in members if nodes[i][0] == "pad")
                g = nodes[pi][1]
                first = ((g.bbox[0] + g.bbox[2]) / 2, (g.bbox[1] + g.bbox[3]) / 2)  # type: ignore[attr-defined]
            made.append(IslandGroup(pads=pad_labels, rings=len(rs), area_mm2=round(area, 1), layers=sorted({r[2][0] for r in rs}),
                                    x_mm=round(first[0], 3) if first else None, y_mm=round(first[1], 3) if first else None))
        if not made:
            continue
        made.sort(key=lambda g: (-len(g.pads), -g.area_mm2))
        out.append(NetIslands(net=net, groups=len(made), main=made[0], detached=[g for g in made[1:] if g.pads or g.area_mm2 > 0.5]))
    return ZoneIslands(board=display(board), nets=out, dead_copper=dead, warnings=warnings)


def _copper_touch(na, nb, d_pt_seg, d_seg_seg) -> bool:
    """Two non-fill items of one net overlap: pads, vias and tracks by their shapes."""
    def shape(nd):
        kind, g = nd[0], nd[1]
        if kind == "via":
            return ("circle", (g[0], g[1]), g[2])
        if kind == "track":
            return ("capsule", ((g.x1, g.y1), (g.x2, g.y2)), g.width / 2)
        return ("pad", g, 0.0)

    sa, sb = shape(na), shape(nb)
    if sa[0] == "pad" and sb[0] == "pad":
        ia, ib = sa[1], sb[1]
        if ia.bbox[2] < ib.bbox[0] or ib.bbox[2] < ia.bbox[0] or ia.bbox[3] < ib.bbox[1] or ib.bbox[3] < ia.bbox[1]:
            return False
        cx, cy = (ia.bbox[0] + ia.bbox[2]) / 2, (ia.bbox[1] + ia.bbox[3]) / 2
        return ib.distance_to_point(cx, cy) <= max(ia.bbox[2] - ia.bbox[0], ia.bbox[3] - ia.bbox[1]) / 2
    if sa[0] == "pad":
        sa, sb = sb, sa
    if sb[0] == "pad":
        it = sb[1]
        if sa[0] == "circle":
            return it.distance_to_point(*sa[1]) <= sa[2] + 1e-3
        return it.distance_to_segment(*sa[1]) <= sa[2] + 1e-3
    if sa[0] == "circle" and sb[0] == "circle":
        return math.dist(sa[1], sb[1]) <= sa[2] + sb[2] + 1e-3
    if sa[0] == "circle":
        sa, sb = sb, sa
    if sb[0] == "circle":
        return d_pt_seg(*sb[1], *sa[1][0], *sa[1][1]) <= sa[2] + sb[2] + 1e-3
    return d_seg_seg(sa[1][0], sa[1][1], sb[1][0], sb[1][1]) <= sa[2] + sb[2] + 1e-3


# ---------------------------------------------------------------- board against schematic, pin by pin
def _norm_net(n: str | None) -> str | None:
    if not n or n.startswith("unconnected-") or n.startswith("Net-("):
        return None if not n or n.startswith("unconnected-") else n
    return n.lstrip("/")


def parity(board: Path, root_schematic: Path) -> ParityReport:
    from kicad_layer.cli import netlist as netlist_mod

    bm = load_board(board)
    nl = netlist_mod.load_netlist(root_schematic, include_components=True, max_nets=100000)
    sch_pins: dict[tuple[str, str], str | None] = {}
    for net in nl.nets:
        for node in net.nodes:
            sch_pins[(node.ref, str(node.pin))] = _norm_net(net.name)
    comps = {c.ref: c for c in nl.components if not c.ref.startswith("#")}
    for c in comps.values():
        for pin in getattr(c, "pins", None) or []:
            num = pin if isinstance(pin, str) else getattr(pin, "number", getattr(pin, "pin", ""))
            if num:
                sch_pins.setdefault((c.ref, str(num)), None)
    fps = {fp.ref: fp for fp in bm.footprints}
    issues: list[ParityIssue] = []
    for ref, c in sorted(comps.items()):
        fp = fps.get(ref)
        if fp is None:
            issues.append(ParityIssue(kind="missing_footprint", ref=ref, schematic=c.footprint or "", hint="Update PCB from Schematic, or place the footprint."))
            continue
        if c.footprint and c.footprint != fp.lib_id:
            issues.append(ParityIssue(kind="footprint_mismatch", ref=ref, schematic=c.footprint, board=fp.lib_id))
        pad_nets: dict[str, set[str | None]] = defaultdict(set)
        for p in fp.pads:
            if p.number:
                pad_nets[p.number].add(_norm_net(p.net))
        pins = {pin for (r, pin) in sch_pins if r == ref}
        for pin in sorted(pins - set(pad_nets)):
            similar = [q for q in pad_nets if q not in pins]
            issues.append(ParityIssue(kind="pin_without_pad", ref=ref, pin=pin, schematic=sch_pins[(ref, pin)],
                                      hint=(f"the footprint has pad(s) {', '.join(sorted(similar))} that no pin names; renumber the symbol pin or the pad"
                                            if similar else "the footprint has no pad with this number")))
        for pad in sorted(set(pad_nets) - pins):
            issues.append(ParityIssue(kind="pad_without_pin", ref=ref, pin=pad, board=", ".join(sorted(n or "-" for n in pad_nets[pad])),
                                      hint="mechanical pads may be left unnumbered; otherwise give the symbol a pin with this number"))
        for pin in sorted(pins & set(pad_nets)):
            want = sch_pins[(ref, pin)]
            have = pad_nets[pin]
            if want not in have or len(have) > 1:
                issues.append(ParityIssue(kind="net_mismatch", ref=ref, pin=pin, schematic=want or "(no net)", board=", ".join(sorted(n or "(no net)" for n in have))))
    for ref, fp in sorted(fps.items()):
        if ref not in comps and not ref.startswith(("H", "MH", "FID", "LOGO", "REF**")):
            issues.append(ParityIssue(kind="extra_footprint", ref=ref, board=fp.lib_id, hint="no symbol behind it; delete it or add the symbol"))
    summary: dict[str, int] = defaultdict(int)
    for i in issues:
        summary[i.kind] += 1
    return ParityReport(board=display(board), schematic=display(root_schematic), components=len(comps), footprints=len(fps), issues=issues, summary=dict(summary))


# ---------------------------------------------------------------- a 2-D plot
LAYER_COLORS = {"F.Cu": (200, 52, 52), "B.Cu": (52, 96, 200)}
FILL_COLORS = {"F.Cu": (200, 52, 52, 45), "B.Cu": (52, 96, 200, 45)}


def plot(board: Path, out: Path, *, rect: list[float] | None = None, layers: list[str] | None = None, nets: list[str] | None = None,
         scale: float = 20.0, labels: bool = True, fills: bool = True) -> Path:
    """Copper, pads, vias and fills of ``layers`` in ``rect`` at ``scale`` px/mm; ``nets`` drawn bold, the rest faded."""
    from PIL import Image, ImageDraw

    from kicad_layer.design.copper import copper_layers, edge_segments, pad_item

    bm = load_board(board)
    layers = layers or ["F.Cu", "B.Cu"]
    if rect is None:
        if bm.outline is None:
            raise LayerError(INVALID_ARGUMENT, "the board has no outline; give rect [x0, y0, x1, y1].")
        x0, y0, x1, y1 = bm.outline
        x0, y0, x1, y1 = x0 - 1, y0 - 1, x1 + 1, y1 + 1
    else:
        x0, y0, x1, y1 = min(rect[0], rect[2]), min(rect[1], rect[3]), max(rect[0], rect[2]), max(rect[1], rect[3])
    w, h = int((x1 - x0) * scale), int((y1 - y0) * scale)
    if w * h > 36_000_000:
        scale = math.sqrt(36_000_000 / ((x1 - x0) * (y1 - y0)))
        w, h = int((x1 - x0) * scale), int((y1 - y0) * scale)
    img = Image.new("RGBA", (max(w, 1), max(h, 1)), (20, 24, 28, 255))
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(over)
    want = set(nets or [])

    def P(x, y):
        return ((x - x0) * scale, (y - y0) * scale)

    def col(layer, net, alpha=255):
        c = LAYER_COLORS.get(layer, (150, 150, 150))
        if want and net not in want:
            c = tuple(int(v * 0.35) for v in c)
        elif want and net in want:
            c = (255, 210, 60)
        return (*c, alpha)

    if fills:
        for z in bm.zones:
            for layer, polys in (z.fills or {}).items():
                if layer not in layers:
                    continue
                fc = FILL_COLORS.get(layer, (120, 120, 120, 40))
                if want and z.net in want:
                    fc = (255, 210, 60, 70)
                for pts in polys:
                    if len(pts) >= 3:
                        dr.polygon([P(x, y) for x, y in pts], fill=fc)
    for layer in reversed(layers):
        for s in bm.segments:
            if s.layer != layer:
                continue
            c = col(layer, s.net)
            dr.line([P(s.x1, s.y1), P(s.x2, s.y2)], fill=c, width=max(1, int(s.width * scale)))
            r = s.width * scale / 2
            for x, y in ((s.x1, s.y1), (s.x2, s.y2)):
                px, py = P(x, y)
                dr.ellipse([px - r, py - r, px + r, py + r], fill=c)
    copper = copper_layers(bm.copper_layers)
    for fp in bm.footprints:
        for p in fp.pads:
            it = pad_item(p, copper)
            if it is None or not set(it.layers) & set(layers):
                continue
            layer = next((l for l in layers if l in it.layers), layers[0])
            c = col(layer, p.net, 220)
            if it.shape == "circle":
                px, py = P(*it.geom)
                r = it.radius * scale
                dr.ellipse([px - r, py - r, px + r, py + r], fill=c)
            elif it.shape == "capsule":
                ax, ay, bx, by = it.geom
                dr.line([P(ax, ay), P(bx, by)], fill=c, width=max(1, int(2 * it.radius * scale)))
            else:
                from kicad_layer.design.copper import rotate

                cx, cy, ww, hh, ang = it.geom
                ww, hh = ww + 2 * it.radius, hh + 2 * it.radius
                pts = [rotate(sx * ww / 2, sy * hh / 2, ang) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
                dr.polygon([P(cx + dx, cy + dy) for dx, dy in pts], fill=c)
            if p.drill:
                px, py = P(p.x, p.y)
                r = p.drill / 2 * scale
                dr.ellipse([px - r, py - r, px + r, py + r], fill=(20, 24, 28, 255))
        if fp.courtyard:
            a, b = P(fp.courtyard[0], fp.courtyard[1]), P(fp.courtyard[2], fp.courtyard[3])
            dr.rectangle([a, b], outline=(130, 130, 130, 160))
            if labels:
                dr.text((a[0] + 2, a[1] + 1), fp.ref, fill=(230, 230, 230, 230))
    for v in bm.vias:
        px, py = P(v.x, v.y)
        r = v.size / 2 * scale
        c = (255, 210, 60, 255) if want and v.net in want else (200, 200, 200, 255)
        dr.ellipse([px - r, py - r, px + r, py + r], fill=c)
        rd = v.drill / 2 * scale
        dr.ellipse([px - rd, py - rd, px + rd, py + rd], fill=(20, 24, 28, 255))
    for a, b in edge_segments(board):
        dr.line([P(*a), P(*b)], fill=(240, 220, 120, 255), width=2)
    if labels and scale >= 12:
        seen: set[str] = set()
        for s in bm.segments:
            if s.layer in layers and s.net and (not want or s.net in want) and (s.net, s.layer) not in seen and math.dist((s.x1, s.y1), (s.x2, s.y2)) > 3:
                seen.add((s.net, s.layer))  # type: ignore[arg-type]
                dr.text(P((s.x1 + s.x2) / 2, (s.y1 + s.y2) / 2), s.net[:14], fill=(255, 255, 255, 200))
    img = Image.alpha_composite(img, over)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out)
    return out
