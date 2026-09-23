"""Design review beyond DRC: one report, every check with a verdict, nothing skipped silently.

Checks read the design files directly and lean on kicad-cli for ERC, DRC and the netlist.
Each check states what it looked at and where its limits come from. A check that cannot
run says UNVERIFIED with the reason; it never disappears from the report.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_layer.cli import netlist as netlist_mod
from kicad_layer.cli import reports
from kicad_layer.errors import LayerError
from kicad_layer.fab_limits import FabLimits, limits_for
from kicad_layer.kicad_libs import rotate_about
from kicad_layer.models import Netlist, ReviewCheck, ReviewFinding, ReviewReport, VerdictReport
from kicad_layer.paths import display, locate_project
from kicad_layer.sexpr import atoms, child, children, parse, tag, value

POWER_NET = re.compile(r"^(\+|-)?\d+(V|V\d+|\.\d+V)$|^(\+|-)?\d*V?(CC|DD|BUS|IN|OUT|BAT|SYS|PWR)|GND|VSS|POWER|VREF", re.IGNORECASE)
GENERIC_VALUES = {"", "~", "R", "C", "L", "D", "LED", "Q", "U", "J", "SW", "Y", "F", "FB", "T", "K", "TP", "MountingHole"}


# --------------------------------------------------------------------------------------
# board model (from the file)
# --------------------------------------------------------------------------------------


@dataclass
class PadGeo:
    ref: str
    number: str
    x: float
    y: float
    size: tuple[float, float]
    drill: float | None
    net: str | None
    kind: str
    layers: list[str]
    shape: str = "rect"  # circle, rect, oval, roundrect, trapezoid, custom
    angle: float = 0.0  # absolute, degrees
    roundrect_ratio: float = 0.0
    clearance: float = 0.0  # the pad's own clearance override, 0 when it uses the class value


@dataclass
class FpGeo:
    ref: str
    lib_id: str
    x: float
    y: float
    rotation: float
    layer: str
    pads: list[PadGeo] = field(default_factory=list)
    courtyard: tuple[float, float, float, float] | None = None  # absolute bbox


@dataclass
class SegGeo:
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: str
    net: str | None


@dataclass
class ViaGeo:
    x: float
    y: float
    size: float
    drill: float
    net: str | None


@dataclass
class ZoneGeo:
    net: str | None
    layers: list[str]
    name: str
    fill_requested: bool
    filled: bool
    polygon: list[tuple[float, float]] = field(default_factory=list)  # the zone's own outline, not the fill
    outlines: list[list[tuple[float, float]]] = field(default_factory=list)  # every outline ring (a cut-out is a ring of its own)
    fills: dict[str, list[list[tuple[float, float]]]] = field(default_factory=dict)  # layer -> filled polygons, empty when unfilled
    priority: int = 0
    rule_area: bool = False  # a keep-out: no copper of its own
    keepout: dict[str, bool] = field(default_factory=dict)  # tracks, vias, pads, copperpour, footprints -> not allowed


@dataclass
class TextGeo:
    text: str
    layer: str
    height: float
    thickness: float
    x: float
    y: float


@dataclass
class BoardModel:
    path: Path
    copper_layers: int
    outline: tuple[float, float, float, float] | None
    footprints: list[FpGeo]
    segments: list[SegGeo]
    vias: list[ViaGeo]
    zones: list[ZoneGeo]
    texts: list[TextGeo]
    design_rules: dict[str, float]


def _f(node, name: str, idx: int = 1, default: float = 0.0) -> float:
    c = child(node, name)
    try:
        return float(c[idx]) if c is not None and len(c) > idx else default
    except (TypeError, ValueError):
        return default


def _custom_pad_bbox(pad) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """(xmin, xmax), (ymin, ymax) of a custom pad's primitives in the pad's own frame."""
    prims = child(pad, "primitives")
    if prims is None:
        return None, None
    xs: list[float] = []
    ys: list[float] = []
    for g in prims[1:]:
        if not isinstance(g, list):
            continue
        t = tag(g)
        w = _f(child(g, "stroke"), "width", 1, 0.0) if child(g, "stroke") is not None else _f(g, "width", 1, 0.0)
        if t == "gr_circle":
            c, e = child(g, "center"), child(g, "end")
            rad = math.hypot(float(e[1]) - float(c[1]), float(e[2]) - float(c[2])) + w / 2
            xs += [float(c[1]) - rad, float(c[1]) + rad]
            ys += [float(c[2]) - rad, float(c[2]) + rad]
        elif t in ("gr_line", "gr_rect", "gr_arc"):
            for k in ("start", "end", "mid"):
                c = child(g, k)
                if c is not None:
                    xs += [float(c[1]) - w / 2, float(c[1]) + w / 2]
                    ys += [float(c[2]) - w / 2, float(c[2]) + w / 2]
        elif t == "gr_poly":
            pts = child(g, "pts")
            for xy in children(pts, "xy") if pts is not None else []:
                xs += [float(xy[1]) - w / 2, float(xy[1]) + w / 2]
                ys += [float(xy[2]) - w / 2, float(xy[2]) + w / 2]
    # the anchor itself
    size = child(pad, "size")
    if size is not None and len(size) > 2:
        xs += [-float(size[1]) / 2, float(size[1]) / 2]
        ys += [-float(size[2]) / 2, float(size[2]) / 2]
    if not xs:
        return None, None
    return (min(xs), max(xs)), (min(ys), max(ys))


COURTYARD_SHAPES = ("fp_line", "fp_rect", "fp_arc", "fp_poly", "fp_circle")


def _arc_extremes(a: tuple[float, float], m: tuple[float, float], b: tuple[float, float]) -> list[tuple[float, float]]:
    """The points where the arc from ``a`` through ``m`` to ``b`` reaches its circle's leftmost, rightmost, top or bottom."""
    (ax, ay), (mx, my), (bx, by) = a, m, b
    d = 2 * (ax * (my - by) + mx * (by - ay) + bx * (ay - my))
    if abs(d) < 1e-12:
        return []  # collinear: a straight line, its ends bound it
    ux = ((ax * ax + ay * ay) * (my - by) + (mx * mx + my * my) * (by - ay) + (bx * bx + by * by) * (ay - my)) / d
    uy = ((ax * ax + ay * ay) * (bx - mx) + (mx * mx + my * my) * (ax - bx) + (bx * bx + by * by) * (mx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)

    def ang(x: float, y: float) -> float:
        return math.atan2(y - uy, x - ux) % (2 * math.pi)

    t0, tm, t1 = ang(ax, ay), ang(mx, my), ang(bx, by)
    ccw_span = (t1 - t0) % (2 * math.pi)
    ccw = (tm - t0) % (2 * math.pi) <= ccw_span  # the mid point lies on the counter-clockwise way from a to b
    out = []
    for k in range(4):
        t = k * math.pi / 2
        off = (t - t0) % (2 * math.pi)
        if (off <= ccw_span) if ccw else (off >= ccw_span or off == 0.0):
            out.append((ux + r * math.cos(t), uy + r * math.sin(t)))
    return out


def courtyard_points(g, rot: float = 0.0, ox: float = 0.0, oy: float = 0.0) -> list[tuple[float, float]]:
    """Points whose bounding box bounds one courtyard graphic of a footprint placed at (ox, oy) turned by ``rot``.

    Lines and rectangles give their ends, polygons their vertices, arcs their ends and the extremes they
    sweep through, circles their centre plus and minus the radius along the board's axes (exact at any
    rotation). Both the design package's placement check and ``load_board`` read courtyards through it."""
    t = tag(g)

    def pt(name: str) -> tuple[float, float] | None:
        c = child(g, name)
        return rotate_about(float(c[1]), float(c[2]), rot, ox, oy) if c is not None and len(c) > 2 else None

    if t == "fp_circle":
        c, e = child(g, "center"), child(g, "end")
        if c is None or e is None or len(c) < 3 or len(e) < 3:
            return []
        r = math.hypot(float(e[1]) - float(c[1]), float(e[2]) - float(c[2]))
        cx, cy = pt("center")
        return [(cx - r, cy - r), (cx + r, cy + r)]
    out = [q for q in (pt("start"), pt("mid"), pt("end")) if q is not None]
    if t == "fp_arc" and len(out) == 3:
        out += _arc_extremes(out[0], out[1], out[2])
    pts = child(g, "pts")
    if pts is not None:
        out += [rotate_about(float(xy[1]), float(xy[2]), rot, ox, oy) for xy in children(pts, "xy")]
    return out


def load_board(path: Path) -> BoardModel:
    root = parse(path.read_text(encoding="utf-8", errors="replace"))
    layers_node = child(root, "layers") or []
    copper = sum(1 for l in layers_node[1:] if isinstance(l, list) and len(l) > 2 and str(l[2]) in ("signal", "power", "mixed", "jumper") and str(l[1]).endswith(".Cu"))

    xs: list[float] = []
    ys: list[float] = []
    for g in root:
        if not isinstance(g, list) or tag(g) not in ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly"):
            continue
        if value(g, "layer") != "Edge.Cuts":
            continue
        for k in ("start", "end", "mid", "center"):
            c = child(g, k)
            if c is not None and len(c) > 2:
                xs.append(float(c[1]))
                ys.append(float(c[2]))
        pts = child(g, "pts")
        if pts is not None:
            for xy in children(pts, "xy"):
                xs.append(float(xy[1]))
                ys.append(float(xy[2]))
        if tag(g) == "gr_circle":
            c = child(g, "center")
            e = child(g, "end")
            if c is not None and e is not None:
                r = math.hypot(float(e[1]) - float(c[1]), float(e[2]) - float(c[2]))
                xs += [float(c[1]) - r, float(c[1]) + r]
                ys += [float(c[2]) - r, float(c[2]) + r]
    outline = (min(xs), min(ys), max(xs), max(ys)) if xs else None

    footprints: list[FpGeo] = []
    texts: list[TextGeo] = []
    for fp in children(root, "footprint"):
        at = child(fp, "at") or []
        fx, fy = (float(at[1]), float(at[2])) if len(at) > 2 else (0.0, 0.0)
        rot = float(at[3]) if len(at) > 3 else 0.0
        props = {str(p[1]): str(p[2]) for p in children(fp, "property") if len(p) > 2}
        geo = FpGeo(ref=props.get("Reference", "?"), lib_id=str(fp[1]), x=fx, y=fy, rotation=rot, layer=value(fp, "layer") or "F.Cu")
        for pad in children(fp, "pad"):
            pat = child(pad, "at") or []
            lx, ly = (float(pat[1]), float(pat[2])) if len(pat) > 2 else (0.0, 0.0)
            # KiCad writes pad coordinates in the footprint's own frame, already mirrored for a part on
            # B.Cu, so one rotation about the footprint origin places pads on either side.
            ax, ay = rotate_about(lx, ly, rot, fx, fy)
            size = child(pad, "size") or []
            drill = child(pad, "drill")
            dval = None
            if drill is not None:
                nums = [a for a in atoms(drill) if re.match(r"^[\d.]+$", a)]
                if nums:
                    dval = float(nums[0])
            pad_angle = float(pat[3]) if len(pat) > 3 else 0.0
            rr = child(pad, "roundrect_rratio")
            shape = str(pad[3]) if len(pad) > 3 else "rect"
            psize = (float(size[1]), float(size[2])) if len(size) > 2 else (0.0, 0.0)
            if shape == "custom":
                # the anchor's size says nothing about the copper: take the primitives' bounding box, rotated with the pad
                bx, by = _custom_pad_bbox(pad)
                if bx and by:
                    cs = [rotate_about(px, py, pad_angle, 0.0, 0.0) for px in bx for py in by]
                    xs2 = [c[0] for c in cs]
                    ys2 = [c[1] for c in cs]
                    ax, ay = ax + (min(xs2) + max(xs2)) / 2, ay + (min(ys2) + max(ys2)) / 2
                    psize = (max(xs2) - min(xs2), max(ys2) - min(ys2))
                    shape = "rect"
                    pad_angle = 0.0
            geo.pads.append(PadGeo(ref=geo.ref, number=str(pad[1]), x=ax, y=ay, size=psize,
                                   drill=dval, net=value(pad, "net"), kind=str(pad[2]) if len(pad) > 2 else "", layers=[str(l) for l in (child(pad, "layers") or [])[1:]],
                                   shape=shape, angle=pad_angle % 360, roundrect_ratio=float(rr[1]) if rr is not None else 0.0,
                                   clearance=_f(pad, "clearance", 1, 0.0) if child(pad, "clearance") is not None and not isinstance(child(pad, "clearance")[1], list) and re.match(r"^[\d.]+$", str(child(pad, "clearance")[1])) else 0.0))
        cx: list[float] = []
        cy: list[float] = []
        for g in fp:
            if not isinstance(g, list):
                continue
            t = tag(g)
            if t in COURTYARD_SHAPES and value(g, "layer") in ("F.CrtYd", "B.CrtYd"):
                for px, py in courtyard_points(g, rot, fx, fy):
                    cx.append(px)
                    cy.append(py)
            if t in ("property", "fp_text") and (value(g, "layer") or "").endswith("SilkS") and child(g, "hide") is None:
                eff = child(g, "effects")
                font = child(eff, "font") if eff is not None else None
                size = child(font, "size") if font is not None else None
                th = _f(font, "thickness", 1, 0.15) if font is not None else 0.15
                h = float(size[2]) if size is not None and len(size) > 2 else 1.0
                txt = str(g[2]) if t == "property" and len(g) > 2 else (str(g[2]) if len(g) > 2 else "")
                if t == "property" and str(g[1]) == "Reference":
                    txt = geo.ref
                texts.append(TextGeo(text=txt, layer=value(g, "layer") or "", height=h, thickness=th, x=fx, y=fy))
        if cx:
            geo.courtyard = (min(cx), min(cy), max(cx), max(cy))
        footprints.append(geo)

    segments: list[SegGeo] = []
    for s in children(root, "segment") + children(root, "arc"):
        a = child(s, "start")
        e = child(s, "end")
        if a is None or e is None:
            continue
        segments.append(SegGeo(float(a[1]), float(a[2]), float(e[1]), float(e[2]), _f(s, "width"), value(s, "layer") or "", value(s, "net")))
    vias = [ViaGeo(_f(v, "at", 1), _f(v, "at", 2), _f(v, "size"), _f(v, "drill"), value(v, "net")) for v in children(root, "via")]
    zones = []
    for z in children(root, "zone"):
        layer_nodes = children(z, "layers") + children(z, "layer")
        zl = [str(a) for ln in layer_nodes for a in ln[1:]]
        fill = child(z, "fill")
        poly = child(z, "polygon")
        pts = [(float(xy[1]), float(xy[2])) for xy in children(child(poly, "pts"), "xy")] if poly is not None and child(poly, "pts") is not None else []
        rings = [[(float(xy[1]), float(xy[2])) for xy in children(child(pg, "pts"), "xy")] for pg in children(z, "polygon") if child(pg, "pts") is not None]
        fills: dict[str, list[list[tuple[float, float]]]] = {}
        for fp_ in children(z, "filled_polygon"):
            if child(fp_, "pts") is not None:
                fills.setdefault(value(fp_, "layer") or (zl[0] if zl else ""), []).append([(float(xy[1]), float(xy[2])) for xy in children(child(fp_, "pts"), "xy")])
        ko = child(z, "keepout")
        keepout = {str(c[0]): str(c[1]) == "not_allowed" for c in (ko or [])[1:] if isinstance(c, list) and len(c) > 1}
        prio = child(z, "priority")
        zones.append(ZoneGeo(net=value(z, "net"), layers=zl, name=value(z, "name") or "", fill_requested=fill is not None and atoms(fill)[:1] == ["yes"],
                             filled=bool(children(z, "filled_polygon")), polygon=pts, outlines=rings, fills=fills,
                             priority=int(float(prio[1])) if prio is not None and len(prio) > 1 else 0, rule_area=ko is not None, keepout=keepout))
    for g in children(root, "gr_text"):
        if (value(g, "layer") or "").endswith("SilkS"):
            eff = child(g, "effects")
            font = child(eff, "font") if eff is not None else None
            size = child(font, "size") if font is not None else None
            texts.append(TextGeo(text=str(g[1]), layer=value(g, "layer") or "", height=float(size[2]) if size is not None and len(size) > 2 else 1.0,
                                 thickness=_f(font, "thickness", 1, 0.15) if font is not None else 0.15, x=_f(g, "at", 1), y=_f(g, "at", 2)))
    rules: dict[str, float] = {}
    project = locate_project(path)
    if project.project_file is not None:
        import json

        try:
            pro = json.loads(project.project_file.read_text(encoding="utf-8"))
            for k, v in (pro.get("board", {}).get("design_settings", {}).get("rules", {}) or {}).items():
                if isinstance(v, (int, float)):
                    rules[k] = float(v)
        except (OSError, ValueError):
            pass
    return BoardModel(path=path, copper_layers=copper or 2, outline=outline, footprints=footprints, segments=segments, vias=vias, zones=zones, texts=texts, design_rules=rules)


# --------------------------------------------------------------------------------------
# check helpers
# --------------------------------------------------------------------------------------


def _finding(check: str, severity: str, message: str, **kw) -> ReviewFinding:
    return ReviewFinding(check=check, severity=severity, message=message, **kw)


def _verdict(findings: list[ReviewFinding]) -> str:
    if any(f.severity == "error" for f in findings):
        return "FAIL"
    if any(f.severity == "warning" for f in findings):
        return "WARN"
    return "PASS"


_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
MAX_GROUPS = 50


def _group(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    """Collapse identical findings into one line with a count.

    A board with 705 undersized vias is one problem, not 705 lines; the first occurrence keeps
    its position and detail. Groups are ordered errors first, then by how often they occur.
    """
    groups: dict[tuple[str, str], ReviewFinding] = {}
    for f in findings:
        key = (f.severity, f.message)
        if key in groups:
            groups[key].count += f.count
        else:
            groups[key] = f.model_copy()
    return sorted(groups.values(), key=lambda f: (_SEVERITY_ORDER.get(f.severity, 3), -f.count, f.message))


def _check(cid: str, name: str, findings: list[ReviewFinding], evidence: str, summary: str | None = None, *, limit_source: str | None = None, verdict: str | None = None,
           data: dict[str, Any] | None = None) -> ReviewCheck:
    v = verdict or _verdict(findings)
    grouped = _group(findings)
    return ReviewCheck(id=cid, name=name, verdict=v, summary=summary or (f"{len(findings)} finding(s)" if findings else "no findings"),
                       findings=grouped[:MAX_GROUPS], truncated=len(grouped) > MAX_GROUPS, evidence=evidence, limit_source=limit_source, data=data or {})


def _unverified(cid: str, name: str, reason: str, evidence: str = "") -> ReviewCheck:
    return ReviewCheck(id=cid, name=name, verdict="UNVERIFIED", summary=reason, findings=[], evidence=evidence or "check could not run")


def _info(cid: str, name: str, summary: str, evidence: str, data: dict[str, Any] | None = None) -> ReviewCheck:
    return ReviewCheck(id=cid, name=name, verdict="INFO", summary=summary, findings=[], evidence=evidence, data=data or {})


# --------------------------------------------------------------------------------------
# board checks
# --------------------------------------------------------------------------------------


def check_drc(board: Path, parity: bool) -> tuple[ReviewCheck, ReviewCheck]:
    try:
        rep = reports.run_drc(board, severity="all", schematic_parity=parity)
    except LayerError as exc:
        return _unverified("drc", "Design rules check (kicad-cli)", str(exc)), _unverified("unrouted", "Unrouted connections", "DRC did not run")
    drc_findings = [
        _finding("drc", f.severity if f.severity in ("error", "warning") else "info", f"{f.type}: {f.description}",
                 ref=None, x_mm=f.items[0].x_mm if f.items else None, y_mm=f.items[0].y_mm if f.items else None, detail="; ".join(i.description for i in f.items[:2]))
        for f in rep.findings if not f.excluded and f.category != "unconnected"
    ]
    drc = _check("drc", "Design rules check (kicad-cli)", drc_findings, f"kicad-cli pcb drc, KiCad {rep.kicad_version}, parity {'on' if parity else 'off'}",
                 summary=f"{rep.verdict}: {rep.counts.get('errors', 0)} errors, {rep.counts.get('warnings', 0)} warnings", verdict=rep.verdict if rep.verdict in ("BLOCKED", "UNVERIFIED") else None)
    unrouted_f = [_finding("unrouted", "error", f.description, x_mm=f.items[0].x_mm if f.items else None, y_mm=f.items[0].y_mm if f.items else None, detail="; ".join(i.description for i in f.items[:2]))
                  for f in rep.findings if f.category == "unconnected"]
    unrouted = _check("unrouted", "Unrouted connections", unrouted_f, "kicad-cli DRC unconnected_items", summary=f"{len(unrouted_f)} unrouted connection(s)")
    return drc, unrouted


def check_zone_fills(bm: BoardModel) -> ReviewCheck:
    stale = [z for z in bm.zones if z.fill_requested and not z.filled]
    findings = [_finding("zone_fills", "warning", f"zone {z.name or z.net or '?'} on {','.join(z.layers)} is not filled in the file", net=z.net) for z in stale]
    return _check("zone_fills", "Zone fills present in the file", findings, f"{len(bm.zones)} zone(s) in the board file",
                  summary="all zones filled" if not stale else f"{len(stale)} unfilled zone(s): DRC and renders on this file do not show the copper pours")


def check_off_board(bm: BoardModel) -> ReviewCheck:
    if bm.outline is None:
        return _unverified("off_board", "Footprints inside the outline", "the board has no Edge.Cuts outline")
    x0, y0, x1, y1 = bm.outline
    findings = []
    for fp in bm.footprints:
        if fp.lib_id.startswith("MountingHole") is False and not (x0 <= fp.x <= x1 and y0 <= fp.y <= y1):
            findings.append(_finding("off_board", "error", f"{fp.ref} at ({fp.x}, {fp.y}) lies outside the outline", ref=fp.ref, x_mm=fp.x, y_mm=fp.y))
        elif fp.courtyard and (fp.courtyard[0] < x0 - 0.01 or fp.courtyard[1] < y0 - 0.01 or fp.courtyard[2] > x1 + 0.01 or fp.courtyard[3] > y1 + 0.01):
            findings.append(_finding("off_board", "warning", f"{fp.ref}'s courtyard crosses the outline", ref=fp.ref, x_mm=fp.x, y_mm=fp.y))
    return _check("off_board", "Footprints inside the outline", findings, f"outline bbox {x0:.1f},{y0:.1f} to {x1:.1f},{y1:.1f} mm; {len(bm.footprints)} footprints")


def check_dfm(bm: BoardModel, fab: FabLimits) -> ReviewCheck:
    f: list[ReviewFinding] = []
    cid = "dfm"
    # tracks
    if bm.segments:
        narrow = [s for s in bm.segments if s.width < fab.min_track_mm - 1e-6]
        for s in narrow[:20]:
            f.append(_finding(cid, "error", f"track {s.width} mm on {s.layer} is narrower than {fab.min_track_mm} mm", net=s.net, x_mm=s.x1, y_mm=s.y1, value=s.width, limit=fab.min_track_mm))
    # design rules vs fab spacing
    dr_clear = bm.design_rules.get("min_clearance")
    if dr_clear is not None and dr_clear < fab.min_space_mm - 1e-6:
        f.append(_finding(cid, "warning", f"project minimum clearance {dr_clear} mm is below the fab's {fab.min_space_mm} mm, so DRC may pass spacing the fab rejects", value=dr_clear, limit=fab.min_space_mm))
    # vias
    for v in bm.vias:
        ring = (v.size - v.drill) / 2
        if v.drill < fab.min_via_drill_mm - 1e-6:
            f.append(_finding(cid, "error", f"via drill {v.drill} mm below {fab.min_via_drill_mm} mm", net=v.net, x_mm=v.x, y_mm=v.y, value=v.drill, limit=fab.min_via_drill_mm))
        elif v.size < fab.min_via_diameter_mm - 1e-6:
            f.append(_finding(cid, "error", f"via diameter {v.size} mm below {fab.min_via_diameter_mm} mm", net=v.net, x_mm=v.x, y_mm=v.y, value=v.size, limit=fab.min_via_diameter_mm))
        elif ring < fab.abs_min_annular_ring_mm - 1e-6:
            f.append(_finding(cid, "error", f"via annular ring {ring:.3f} mm below the absolute minimum {fab.abs_min_annular_ring_mm} mm", net=v.net, x_mm=v.x, y_mm=v.y, value=round(ring, 3), limit=fab.abs_min_annular_ring_mm))
        elif ring < fab.min_annular_ring_mm - 1e-6:
            f.append(_finding(cid, "warning", f"via annular ring {ring:.3f} mm below the recommended {fab.min_annular_ring_mm} mm", net=v.net, x_mm=v.x, y_mm=v.y, value=round(ring, 3), limit=fab.min_annular_ring_mm))
    # plated through-hole pads
    for fp in bm.footprints:
        for p in fp.pads:
            if p.kind == "thru_hole" and p.drill:
                ring = (min(p.size) - p.drill) / 2
                if ring < fab.abs_min_annular_ring_mm - 1e-6:
                    f.append(_finding(cid, "error", f"{p.ref} pad {p.number} annular ring {ring:.3f} mm below {fab.abs_min_annular_ring_mm} mm", ref=p.ref, x_mm=p.x, y_mm=p.y, value=round(ring, 3), limit=fab.abs_min_annular_ring_mm))
    # via hole to via hole spacing
    vs = bm.vias[:3000]
    for i in range(len(vs)):
        for j in range(i + 1, len(vs)):
            a, b = vs[i], vs[j]
            gap = math.hypot(a.x - b.x, a.y - b.y) - (a.drill + b.drill) / 2
            if gap < fab.min_via_hole_to_hole_mm - 1e-6:
                f.append(_finding(cid, "error", f"via holes {gap:.3f} mm apart, below {fab.min_via_hole_to_hole_mm} mm", x_mm=a.x, y_mm=a.y, value=round(gap, 3), limit=fab.min_via_hole_to_hole_mm))
    # copper to edge (bounding-box estimate)
    if bm.outline:
        x0, y0, x1, y1 = bm.outline
        worst = None
        for s in bm.segments:
            for (x, y) in ((s.x1, s.y1), (s.x2, s.y2)):
                d = min(x - x0, x1 - x, y - y0, y1 - y) - s.width / 2
                if worst is None or d < worst[0]:
                    worst = (d, x, y, "track", s.net)
        for fp in bm.footprints:
            for p in fp.pads:
                if "F.Cu" in p.layers or "B.Cu" in p.layers or "*.Cu" in p.layers:
                    d = min(p.x - x0, x1 - p.x, p.y - y0, y1 - p.y) - max(p.size) / 2
                    if worst is None or d < worst[0]:
                        worst = (d, p.x, p.y, f"{p.ref} pad {p.number}", p.net)
        if worst and worst[0] < fab.min_copper_to_edge_mm - 1e-6:
            f.append(_finding(cid, "warning", f"{worst[3]} is about {worst[0]:.2f} mm from the outline's bounding box, fab wants {fab.min_copper_to_edge_mm} mm (estimate; DRC's copper_edge_clearance is exact)",
                              net=worst[4], x_mm=worst[1], y_mm=worst[2], value=round(worst[0], 3), limit=fab.min_copper_to_edge_mm))
        w, h = x1 - x0, y1 - y0
        if min(w, h) < fab.min_board_mm:
            f.append(_finding(cid, "error", f"board {w:.1f} x {h:.1f} mm is below the fab's minimum {fab.min_board_mm} mm", value=min(w, h), limit=fab.min_board_mm))
    # silkscreen text
    for t in bm.texts:
        if t.height < fab.min_silk_text_height_mm - 1e-6:
            f.append(_finding(cid, "warning", f"silkscreen text {t.text!r} height {t.height} mm below {fab.min_silk_text_height_mm} mm; may be unreadable", x_mm=t.x, y_mm=t.y, value=t.height, limit=fab.min_silk_text_height_mm))
        if t.thickness < fab.min_silk_line_mm - 1e-6:
            f.append(_finding(cid, "warning", f"silkscreen text {t.text!r} stroke {t.thickness} mm below {fab.min_silk_line_mm} mm", x_mm=t.x, y_mm=t.y, value=t.thickness, limit=fab.min_silk_line_mm))
    if bm.copper_layers > fab.layers:
        f.append(_finding(cid, "warning", f"board has {bm.copper_layers} copper layers; limits applied are for {fab.layers}", value=bm.copper_layers, limit=fab.layers))
    ev = f"{fab.name}; {len(bm.segments)} tracks, {len(bm.vias)} vias, {sum(len(fp.pads) for fp in bm.footprints)} pads, {len(bm.texts)} silk texts"
    return _check(cid, f"Manufacturability against {fab.name}", f, ev, limit_source=f"{fab.source} (checked {fab.checked_on})",
                  summary=("meets the standard process" if not f else f"{sum(1 for x in f if x.severity == 'error')} violation(s), {sum(1 for x in f if x.severity == 'warning')} warning(s)"))


def check_power_track_widths(bm: BoardModel, min_width: float = 0.25) -> ReviewCheck:
    by_net: dict[str, float] = {}
    for s in bm.segments:
        if s.net and POWER_NET.search(s.net):
            by_net[s.net] = min(by_net.get(s.net, 99.0), s.width)
    f = [_finding("power_tracks", "warning", f"power net {n} has tracks down to {w} mm (about {0.5 * w / 0.25:.1f} A at 10 C rise, 1 oz, per IPC-2152); check the current", net=n, value=w, limit=min_width)
         for n, w in sorted(by_net.items()) if w < min_width - 1e-6]
    return _check("power_tracks", "Track width on power nets", f, f"{len(by_net)} power-named net(s) with tracks; threshold {min_width} mm",
                  limit_source="IPC-2152 1 oz external, 10 C rise: 0.25 mm about 0.5 A, 0.5 mm about 1 A", summary="power nets use tracks at or above the threshold" if not f else f"{len(f)} power net(s) with thin tracks")


def check_via_stitching(bm: BoardModel) -> ReviewCheck:
    nets_with_zones = {z.net for z in bm.zones if z.net}
    if not nets_with_zones:
        return _info("stitching", "Zone stitching vias", "no copper zones on this board", "board file")
    f = []
    data = {}
    for n in sorted(nets_with_zones):
        count = sum(1 for v in bm.vias if v.net == n)
        data[n] = count
        if count == 0 and bm.copper_layers >= 2:
            f.append(_finding("stitching", "warning", f"zone net {n} has no vias; the pour is reachable only on its own layer", net=n, value=0))
    return _check("stitching", "Zone stitching vias", f, f"vias per zone net: {data}", summary="every zone net has vias" if not f else f"{len(f)} zone net(s) without vias")


def check_decoupling(bm: BoardModel, project, *, warn_mm: float = 5.0, high_mm: float = 8.0) -> ReviewCheck:
    root = project.root_schematic
    if root is None:
        return _unverified("decoupling", "Decoupling capacitor placement", "no schematic next to the board to identify power pins")
    try:
        nl = netlist_mod.load_netlist(root, include_components=True, max_nets=5000)
    except LayerError as exc:
        return _unverified("decoupling", "Decoupling capacitor placement", f"netlist unavailable: {exc}")
    power_nets: dict[str, set[str]] = {}   # ic ref -> nets on its power_in pins
    for net in nl.nets:
        for node in net.nodes:
            if node.pin_type == "power_in" and node.ref.startswith("U"):
                power_nets.setdefault(node.ref, set()).add(net.name)
    if not power_nets:
        return _info("decoupling", "Decoupling capacitor placement", "no ICs with power_in pins in the netlist", "kicad-cli netlist pin types")
    pos = {fp.ref: fp for fp in bm.footprints}
    caps_by_net: dict[str, list[FpGeo]] = {}
    for fp in bm.footprints:
        if fp.ref.startswith("C"):
            for p in fp.pads:
                if p.net:
                    caps_by_net.setdefault(p.net, []).append(fp)
    f = []
    data = {}
    for ic, nets in sorted(power_nets.items()):
        ic_fp = pos.get(ic)
        if ic_fp is None:
            continue
        for n in sorted(nets):
            if re.search(r"GND|VSS", n, re.IGNORECASE):
                continue
            caps = caps_by_net.get(n, [])
            if not caps:
                f.append(_finding("decoupling", "warning", f"{ic} power net {n} has no capacitor on it", ref=ic, net=n))
                continue
            d = min(math.hypot(c.x - ic_fp.x, c.y - ic_fp.y) for c in caps)
            nearest = min(caps, key=lambda c: math.hypot(c.x - ic_fp.x, c.y - ic_fp.y)).ref
            data[f"{ic}:{n}"] = round(d, 2)
            if d > high_mm:
                f.append(_finding("decoupling", "warning", f"{ic} on {n}: nearest capacitor {nearest} is {d:.1f} mm away (over {high_mm} mm)", ref=ic, net=n, value=round(d, 2), limit=high_mm))
            elif d > warn_mm:
                f.append(_finding("decoupling", "info", f"{ic} on {n}: nearest capacitor {nearest} is {d:.1f} mm away (over {warn_mm} mm)", ref=ic, net=n, value=round(d, 2), limit=warn_mm))
    return _check("decoupling", "Decoupling capacitor placement", f, f"power_in pins from the netlist; capacitor distance by footprint origin: {data}",
                  limit_source="kicad-happy EMC rule DC-001: over 8 mm high, over 5 mm medium", summary="every IC power pin has a capacitor within 5 mm" if not f else f"{len(f)} IC power net(s) to look at")


def check_diff_pairs(bm: BoardModel, project_file: Path | None) -> ReviewCheck:
    """Differential pairs by name: skew, coupling, class gap and width, through kicad_layer.routing."""
    from . import routing

    rep = routing.analyse(bm, project_file)
    if not rep.pairs:
        return _info("diff_pairs", "Differential pairs", "no differential pairs found by name", "net names ending in _P/_N, _DP/_DN, +/-")
    findings: list[ReviewFinding] = []
    for p in rep.pairs:
        if p.status == "warn":
            reasons = "; ".join(n for n in p.notes if not n.startswith("CM5 datasheet"))
            findings.append(_finding("diff_pairs", "warning", f"{p.name}: {reasons}", net=p.p_net, value=p.skew_mm, limit=p.skew_limit_mm))
        elif p.status == "partial":
            findings.append(_finding("diff_pairs", "warning", f"{p.name}: only one half is routed", net=p.p_net))
    s = rep.summary
    summary = f"{len(rep.pairs)} pair(s): {s['ok']} ok, {s['warn']} with findings, {s['partial']} half routed, {s['unrouted']} unrouted"
    verdict = "INFO" if (s["ok"] == 0 and s["warn"] == 0 and s["partial"] == 0) else None
    return _check("diff_pairs", "Differential pairs", findings, "route_check: lengths from track segments plus 1.6 mm per via; gap and width from the project's net classes", summary,
                  limit_source="CM5 datasheet sections 2.2 to 2.5: Ethernet and MIPI within 0.15 mm, PCIe and USB 3.0 within 0.1 mm, USB 2.0 within 0.15 mm", verdict=verdict)


def board_info(bm: BoardModel) -> ReviewCheck:
    size = f"{bm.outline[2] - bm.outline[0]:.1f} x {bm.outline[3] - bm.outline[1]:.1f} mm" if bm.outline else "no outline"
    tht = sum(1 for fp in bm.footprints for p in fp.pads if p.kind == "thru_hole")
    return _info("board", "Board summary", f"{size}, {bm.copper_layers} copper layers, {len(bm.footprints)} footprints, {len(bm.segments)} track segments, {len(bm.vias)} vias, {len(bm.zones)} zones, {tht} plated holes",
                 "board file", {"size": size, "copper_layers": bm.copper_layers, "footprints": len(bm.footprints), "tracks": len(bm.segments), "vias": len(bm.vias), "zones": len(bm.zones), "plated_holes": tht})


# --------------------------------------------------------------------------------------
# schematic checks
# --------------------------------------------------------------------------------------


def _erc_report(root: Path) -> tuple[VerdictReport | None, str]:
    """Run ERC once; the report feeds more than one check."""
    try:
        return reports.run_erc(root, severity="all"), ""
    except LayerError as exc:
        return None, str(exc)


def _erc_check(rep: VerdictReport | None, reason: str) -> ReviewCheck:
    if rep is None:
        return _unverified("erc", "Electrical rules check (kicad-cli)", reason)
    f = [_finding("erc", x.severity if x.severity in ("error", "warning") else "info", f"{x.type}: {x.description}", detail="; ".join(i.description for i in x.items[:2]), sheet=x.sheet)
         for x in rep.findings if not x.excluded]
    return _check("erc", "Electrical rules check (kicad-cli)", f, f"kicad-cli sch erc, KiCad {rep.kicad_version}",
                  summary=f"{rep.verdict}: {rep.counts.get('errors', 0)} errors, {rep.counts.get('warnings', 0)} warnings", verdict=rep.verdict if rep.verdict in ("BLOCKED", "UNVERIFIED") else None)


def check_erc(root: Path) -> ReviewCheck:
    return _erc_check(*_erc_report(root))


# "Symbol U1 Pin 8 [VCC, Power input, Line]" or "Symbol #PWR03 Hidden pin 1 [GND, Power input, Line]"
_ERC_PIN_RE = re.compile(r"Symbol (\S+) (?:Hidden )?[Pp]in (\S+) \[([^,\]]*)")


def _erc_rule_severity(root: Path, rule: str) -> str:
    """The severity the project gives an ERC rule: error, warning or ignore (KiCad's default is error)."""
    project = locate_project(root)
    if project.project_file is None:
        return "error"
    try:
        import json

        pro = json.loads(project.project_file.read_text(encoding="utf-8"))
        return str(pro.get("erc", {}).get("rule_severities", {}).get(rule, "error"))
    except (OSError, ValueError):
        return "error"


def check_power_sources(root: Path, rep: VerdictReport | None, reason: str, nl: Netlist | None) -> ReviewCheck:
    """Every net with power-input pins needs a driver: a power-output pin or a PWR_FLAG.

    PWR_FLAG and power symbols never appear in the exported netlist, so the netlist alone
    cannot tell a flagged net from an undriven one. KiCad's own ERC rule can, and its report
    names the first undriven pin; the netlist only names the net and says which nets rely on
    a flag rather than on a real output pin.
    """
    name = "Power nets have a source"
    if rep is None:
        return _unverified("power_sources", name, f"ERC did not run: {reason}")
    if rep.verdict in ("BLOCKED", "UNVERIFIED"):
        return _unverified("power_sources", name, f"ERC verdict {rep.verdict}", "kicad-cli sch erc")
    sev = _erc_rule_severity(root, "power_pin_not_driven")
    if sev == "ignore":
        return _unverified("power_sources", name, "the project's ERC settings ignore power_pin_not_driven; set it to error in Schematic Setup, Violation Severity",
                           "kicad_pro erc.rule_severities")
    by_pin: dict[tuple[str, str], str] = {}
    no_output: list[str] = []
    if nl is not None:
        for net in nl.nets:
            types = {n.pin_type for n in net.nodes}
            for n in net.nodes:
                by_pin[(n.ref, n.pin)] = net.name
            if "power_in" in types and "power_out" not in types:
                no_output.append(net.name)
    findings: list[ReviewFinding] = []
    seen: set[str] = set()
    excluded = 0
    for x in rep.findings:
        if x.type != "power_pin_not_driven":
            continue
        if x.excluded:
            excluded += 1
            continue
        it = x.items[0] if x.items else None
        m = _ERC_PIN_RE.search(it.description) if it else None
        if m:
            ref, pin, pin_name = m.group(1), m.group(2), m.group(3).strip()
            net = by_pin.get((ref, pin)) or pin_name
            where = f" (first at {ref} pin {pin})"
        else:
            net, where = (it.description if it else x.description), ""
        if net in seen:
            continue
        seen.add(net)
        findings.append(_finding("power_sources", "error" if x.severity == "error" else "warning", f"net {net} has power inputs but no power output or PWR_FLAG{where}",
                                 net=net, sheet=x.sheet, x_mm=it.x_mm if it else None, y_mm=it.y_mm if it else None))
    flag_only = sorted(n for n in no_output if n not in seen)
    if findings:
        summary = f"{len(findings)} undriven power net(s)"
    elif flag_only:
        summary = f"every power net is driven; {len(flag_only)} rely on a PWR_FLAG rather than an output pin: " + ", ".join(flag_only[:6]) + (" ..." if len(flag_only) > 6 else "")
    else:
        summary = "every power net is driven by a power output pin"
    evidence = f"kicad-cli ERC rule power_pin_not_driven (project severity {sev}); nets named from the netlist"
    if excluded:
        evidence += f"; {excluded} finding(s) excluded in the project"
    return _check("power_sources", name, findings, evidence, summary=summary, data={"flag_only": flag_only})


def schematic_checks(root: Path) -> list[ReviewCheck]:
    rep, reason = _erc_report(root)
    out: list[ReviewCheck] = [_erc_check(rep, reason)]
    try:
        nl = netlist_mod.load_netlist(root, include_components=True, max_nets=5000)
    except LayerError as exc:
        out.append(_unverified("footprints", "Footprints assigned", f"netlist unavailable: {exc}"))
        out.append(check_power_sources(root, rep, reason, None))
        return out
    comps = [c for c in nl.components if not c.ref.startswith("#")]
    missing = [c for c in comps if not c.footprint]
    out.append(_check("footprints", "Footprints assigned", [_finding("footprints", "error", f"{c.ref} ({c.value or c.part}) has no footprint", ref=c.ref) for c in missing],
                      f"{len(comps)} components in the netlist", summary="every component has a footprint" if not missing else f"{len(missing)} without a footprint"))
    generic = [c for c in comps if (c.value or "").strip() in GENERIC_VALUES and not c.ref.startswith(("H", "TP", "J", "SW"))]
    out.append(_check("values", "Values set", [_finding("values", "warning", f"{c.ref} still has the library default value {c.value!r}", ref=c.ref) for c in generic],
                      f"{len(comps)} components", summary="no library-default values" if not generic else f"{len(generic)} component(s) with a default value"))
    unannotated = [c for c in comps if "?" in c.ref]
    out.append(_check("annotation", "References annotated", [_finding("annotation", "error", f"{c.ref} is not annotated", ref=c.ref) for c in unannotated],
                      f"{len(comps)} components", summary="all annotated" if not unannotated else f"{len(unannotated)} unannotated"))
    # power nets need a driver; only ERC can see PWR_FLAGs
    out.append(check_power_sources(root, rep, reason, nl))
    # decoupling by schematic
    ic_nets: dict[str, set[str]] = {}
    cap_nets: set[str] = set()
    for net in nl.nets:
        for node in net.nodes:
            if node.pin_type == "power_in" and node.ref.startswith("U"):
                ic_nets.setdefault(node.ref, set()).add(net.name)
            if node.ref.startswith("C"):
                cap_nets.add(net.name)
    f = [_finding("decoupling_sch", "warning", f"{ic} power net {n} has no capacitor", ref=ic, net=n) for ic, nets in sorted(ic_nets.items()) for n in sorted(nets)
         if n not in cap_nets and not re.search(r"GND|VSS", n, re.IGNORECASE)]
    out.append(_check("decoupling_sch", "Decoupling present in the schematic", f, f"{len(ic_nets)} IC(s) with power_in pins", summary="every IC power net has a capacitor" if not f else f"{len(f)} IC power net(s) without a capacitor"))
    # bill of materials summary
    by_prefix: dict[str, int] = {}
    for c in comps:
        by_prefix[c.ref.rstrip("0123456789?")] = by_prefix.get(c.ref.rstrip("0123456789?"), 0) + 1
    out.append(_info("bom", "Bill of materials", f"{len(comps)} components, {len({(c.value, c.footprint) for c in comps})} distinct value/footprint pairs, {len(nl.nets)} nets",
                     "netlist", {"by_prefix": by_prefix, "nets": nl.net_count}))
    # simulation: honest status
    from kicad_layer.cli import runner
    from kicad_layer.cli.discovery import find_kicad_cli
    from kicad_layer.config import settings

    try:
        cli = find_kicad_cli()
        cir = settings().cache_dir / "reports" / f"{root.stem}.cir"
        cir.parent.mkdir(parents=True, exist_ok=True)
        r = runner.run([cli.path, "sch", "export", "netlist", "--format", "spice", "-o", cir, root], timeout_s=120, cwd=root.parent)
        text = cir.read_text(encoding="utf-8", errors="replace") if cir.is_file() else ""
        lines = [ln for ln in text.splitlines() if ln and not ln.startswith((".", "*"))]
        modelless = sum(1 for ln in lines if re.search(r"\s__\w+\s*$", ln))
        reason = f"{len(lines)} SPICE elements, {modelless} without a model; no ngspice executable on this machine (KiCad ships only the library)"
        out.append(_unverified("spice", "SPICE operating point", reason, "kicad-cli sch export netlist --format spice"))
    except LayerError as exc:
        out.append(_unverified("spice", "SPICE operating point", str(exc)))
    return out


# --------------------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------------------


def _overall(checks: list[ReviewCheck]) -> str:
    verdicts = {c.verdict for c in checks}
    if "FAIL" in verdicts or "BLOCKED" in verdicts:
        return "FAIL"
    if "WARN" in verdicts:
        return "WARN"
    return "PASS"


def _counts(checks: list[ReviewCheck]) -> dict[str, int]:
    return {
        "errors": sum(f.count for c in checks for f in c.findings if f.severity == "error"),
        "warnings": sum(f.count for c in checks for f in c.findings if f.severity == "warning"),
        "unverified": sum(1 for c in checks if c.verdict == "UNVERIFIED"),
        "checks": len(checks),
    }


def review_board(board: Path, *, fab: str = "jlcpcb", parity: bool = True) -> ReviewReport:
    t0 = time.time()
    bm = load_board(board)
    project = locate_project(board)
    limits = limits_for(fab, bm.copper_layers)
    checks: list[ReviewCheck] = [board_info(bm)]
    drc, unrouted = check_drc(board, parity and project.root_schematic is not None)
    checks += [drc, unrouted, check_zone_fills(bm), check_off_board(bm), check_dfm(bm, limits), check_power_track_widths(bm), check_via_stitching(bm), check_decoupling(bm, project), check_diff_pairs(bm, project.project_file)]
    return ReviewReport(target=display(board), kind="board", fab=limits.name, verdict=_overall(checks), counts=_counts(checks), checks=checks,
                        unverified=[c.id for c in checks if c.verdict == "UNVERIFIED"], duration_s=round(time.time() - t0, 1))


def review_schematic(root: Path) -> ReviewReport:
    t0 = time.time()
    checks = schematic_checks(root)
    return ReviewReport(target=display(root), kind="schematic", fab=None, verdict=_overall(checks), counts=_counts(checks), checks=checks,
                        unverified=[c.id for c in checks if c.verdict == "UNVERIFIED"], duration_s=round(time.time() - t0, 1))


def review_project(path: Path, *, fab: str = "jlcpcb") -> ReviewReport:
    t0 = time.time()
    project = locate_project(path)
    checks: list[ReviewCheck] = []
    if project.root_schematic is not None:
        checks += schematic_checks(project.root_schematic)
    else:
        checks.append(_unverified("erc", "Electrical rules check", "no schematic in the project"))
    fab_name = limits_for(fab, 2).name
    if project.board is not None:
        board_report = review_board(project.board, fab=fab, parity=project.root_schematic is not None)
        checks += board_report.checks
        fab_name = board_report.fab or fab_name
    else:
        checks.append(_unverified("drc", "Design rules check", "no board in the project"))
    return ReviewReport(target=display(project.directory), kind="project", fab=fab_name, verdict=_overall(checks), counts=_counts(checks), checks=checks,
                        unverified=[c.id for c in checks if c.verdict == "UNVERIFIED"], duration_s=round(time.time() - t0, 1))
