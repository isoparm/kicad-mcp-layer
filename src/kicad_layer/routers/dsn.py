"""Specctra DSN export of a KiCad 10 board, for an external autorouter (FreeRouting).

kicad-cli has no Specctra export, so this follows what KiCad's own exporter writes (pcbnew/
specctra_import_export/specctra_export.cpp): coordinates in micrometres with Y negated because DSN
counts Y upwards, ``(resolution um 10)``, one padstack per distinct pad geometry named by family and
size, pins in footprint coordinates with the footprint placed and rotated, existing tracks and vias
as ``protect`` wiring so the router keeps them, copper zones on plane layers as ``plane`` polygons,
outline holes as keepouts, net classes with width, clearance and via, and the default class called
``kicad_default``.

What the DSN cannot say is handled before the router sees the board (``build_dsn``): rule areas
(keep-out zones, on the board or inside a footprint) become per-layer ``keepout``, ``via_keepout``
and ``place_keepout`` entries; plain two-class clearance rules of the .kicad_dru become
``class_class`` rules; nets named by rules the router cannot honour (creepage, physical clearance,
disallow, area or footprint conditions), nets whose class width makes them pours, and nets or
classes the caller excludes go into ``<class>_excluded`` classes with their copper protected, and
the caller passes those classes to FreeRouting's ignore list. The result says which and why.

Only what FreeRouting needs is emitted. Pad shapes: circle, rect, oval (a path with an aperture),
roundrect and custom approximated as the enclosing rectangle for routing purposes (the router only
needs the copper's extent). Through-hole pads span every copper layer; plated holes without copper
become round keepouts.
"""

from __future__ import annotations

import fnmatch
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..kicad_libs import rotate_about
from ..review import BoardModel, PadGeo, load_board
from ..routing import load_netclasses, netclass_for
from ..sexpr import child, children, parse, tag, value
from . import dru as dru_mod

UM = 1000.0  # micrometres per millimetre


def _num(v: float) -> str:
    """A DSN number: micrometres to a tenth, without a trailing .0."""
    s = f"{v:.1f}"
    s = s[:-2] if s.endswith(".0") else s
    return "0" if s == "-0" else s


_SAFE = re.compile(r"^[A-Za-z0-9_./+:-]+$")


def _q(s: str) -> str:
    """Quote only strings that need it, as KiCad does. FreeRouting reads pin ids as bare REF-PIN
    tokens, and a quoted one derails its scanner; names with parentheses, spaces or brackets must
    be quoted (Net-(C1-Pad1), Via[0-3]_600:300_um)."""
    if s and _SAFE.match(s):
        return s
    return '"' + s.replace('"', "'") + '"'


def _pt(x: float, y: float) -> str:
    return f"{_num(x * UM)} {_num(-y * UM)}"


# --------------------------------------------------------------------------------------
# outline: chain Edge.Cuts into closed loops
# --------------------------------------------------------------------------------------


def _arc_points(start, mid, end, n: int = 12) -> list[tuple[float, float]]:
    """Points along a three-point arc, start to end, including both."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9:
        return [start, end]
    ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1) + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
    uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3) + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1, am, a3 = (math.atan2(p[1] - uy, p[0] - ux) for p in (start, mid, end))
    # sweep from a1 through am to a3
    def norm(a):
        while a <= -math.pi:
            a += 2 * math.pi
        while a > math.pi:
            a -= 2 * math.pi
        return a
    sweep = norm(a3 - a1)
    if norm(am - a1) * sweep < 0 or abs(norm(am - a1)) > abs(sweep):
        sweep = sweep - 2 * math.pi if sweep > 0 else sweep + 2 * math.pi
    pts = []
    for i in range(n + 1):
        a = a1 + sweep * i / n
        pts.append((ux + r * math.cos(a), uy + r * math.sin(a)))
    pts[0], pts[-1] = start, end
    return pts


def outline_loops(board_path: Path) -> list[list[tuple[float, float]]]:
    """Closed polygons on Edge.Cuts, largest first; arcs are sampled."""
    root = parse(board_path.read_text(encoding="utf-8", errors="replace"))
    pieces: list[list[tuple[float, float]]] = []
    for g in root:
        if not isinstance(g, list) or value(g, "layer") != "Edge.Cuts":
            continue
        t = tag(g)
        if t == "gr_line":
            a, e = child(g, "start"), child(g, "end")
            pieces.append([(float(a[1]), float(a[2])), (float(e[1]), float(e[2]))])
        elif t == "gr_arc":
            a, m, e = child(g, "start"), child(g, "mid"), child(g, "end")
            pieces.append(_arc_points((float(a[1]), float(a[2])), (float(m[1]), float(m[2])), (float(e[1]), float(e[2]))))
        elif t == "gr_rect":
            a, e = child(g, "start"), child(g, "end")
            x0, y0, x1, y1 = float(a[1]), float(a[2]), float(e[1]), float(e[2])
            pieces.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])
        elif t == "gr_poly":
            pts = [(float(xy[1]), float(xy[2])) for xy in children(child(g, "pts"), "xy")]
            pieces.append(pts + [pts[0]])
        elif t == "gr_circle":
            c, e = child(g, "center"), child(g, "end")
            cx, cy = float(c[1]), float(c[2])
            r = math.hypot(float(e[1]) - cx, float(e[2]) - cy)
            pieces.append([(cx + r * math.cos(2 * math.pi * i / 36), cy + r * math.sin(2 * math.pi * i / 36)) for i in range(37)])
    loops: list[list[tuple[float, float]]] = []
    tol = 1e-3
    remaining = pieces[:]
    while remaining:
        loop = remaining.pop(0)
        while True:
            if len(loop) > 2 and math.dist(loop[0], loop[-1]) < tol:
                break
            for i, piece in enumerate(remaining):
                if math.dist(piece[0], loop[-1]) < tol:
                    loop += piece[1:]
                elif math.dist(piece[-1], loop[-1]) < tol:
                    loop += piece[-2::-1]
                else:
                    continue
                remaining.pop(i)
                break
            else:
                break  # open chain: keep what we have
        loops.append(loop)

    def area(l):
        return abs(sum(l[i][0] * l[(i + 1) % len(l)][1] - l[(i + 1) % len(l)][0] * l[i][1] for i in range(len(l)))) / 2

    loops.sort(key=area, reverse=True)
    return loops


# --------------------------------------------------------------------------------------
# padstacks
# --------------------------------------------------------------------------------------


@dataclass
class _Padstack:
    name: str
    shapes: list[str] = field(default_factory=list)  # shape expressions without the layer, e.g. ("circle {L} 1700")


def _padstack(pad: PadGeo, copper_layers: list[str], fp_rot: float) -> tuple[str, list[str], list[str]]:
    """Padstack name, its layer list and per-layer shape templates ('{L}' stands for the layer)."""
    w, h = pad.size
    if pad.kind == "thru_hole":
        layers = copper_layers
        tagl = "A"
    else:
        layers = [l for l in copper_layers if l in pad.layers or any(x in pad.layers for x in ("*.Cu", "F&B.Cu"))] or [copper_layers[0]]
        tagl = "T" if layers == [copper_layers[0]] else ("B" if layers == [copper_layers[-1]] else "A")
    rel = (pad.angle - fp_rot) % 360  # pad rotation relative to the footprint
    wu, hu = w * UM, h * UM
    if pad.shape == "circle":
        return f"Round[{tagl}]Pad_{_num(wu)}_um", layers, [f"(circle {{L}} {_num(wu)})"]
    if pad.shape == "oval":
        if abs(rel - 90) < 1e-6 or abs(rel - 270) < 1e-6:
            wu, hu = hu, wu
        if wu >= hu:
            half = (wu - hu) / 2
            return f"Oval[{tagl}]Pad_{_num(wu)}x{_num(hu)}_um", layers, [f"(path {{L}} {_num(hu)} {_num(-half)} 0 {_num(half)} 0)"]
        half = (hu - wu) / 2
        return f"Oval[{tagl}]Pad_{_num(wu)}x{_num(hu)}_um", layers, [f"(path {{L}} {_num(wu)} 0 {_num(-half)} 0 {_num(half)})"]
    # rect, roundrect, trapezoid, custom: the enclosing rectangle, rotated with the pad
    if abs(rel - 90) < 1e-6 or abs(rel - 270) < 1e-6:
        wu, hu = hu, wu
        rel = 0.0
    if abs(rel) < 1e-6 or abs(rel - 180) < 1e-6:
        fam = "RoundRect" if pad.shape == "roundrect" else "Rect"
        return f"{fam}[{tagl}]Pad_{_num(wu)}x{_num(hu)}_um", layers, [f"(rect {{L}} {_num(-wu / 2)} {_num(-hu / 2)} {_num(wu / 2)} {_num(hu / 2)})"]
    # arbitrary angle: a rotated polygon
    a = math.radians(-rel)  # DSN y is up, so the rotation sense flips
    pts = []
    for cx, cy in ((-wu / 2, -hu / 2), (wu / 2, -hu / 2), (wu / 2, hu / 2), (-wu / 2, hu / 2)):
        pts.append((cx * math.cos(a) - cy * math.sin(a), cx * math.sin(a) + cy * math.cos(a)))
    poly = " ".join(f"{_num(x)} {_num(y)}" for x, y in pts)
    return f"Rect[{tagl}]Pad_{_num(wu)}x{_num(hu)}_r{int(round(rel))}_um", layers, [f"(polygon {{L}} 0 {poly})"]


def via_padstack_name(size: float, drill: float, n_layers: int) -> str:
    return f"Via[0-{n_layers - 1}]_{_num(size * UM)}:{_num(drill * UM)}_um"


def parse_via_name(name: str) -> tuple[float, float] | None:
    """(size, drill) in mm from a KiCad-style via padstack name, or None."""
    try:
        core = name.split("]_", 1)[1].rsplit("_um", 1)[0]
        size, drill = core.split(":")
        return float(size) / UM, float(drill) / UM
    except (IndexError, ValueError):
        return None


# --------------------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------------------


@dataclass
class DsnOptions:
    plane_layers: dict[str, str] = field(default_factory=dict)  # layer -> net, e.g. {"In1.Cu": "GND"}
    routable_layers: list[str] | None = None  # default: every copper layer that is not a plane
    default_via: tuple[float, float] = (0.6, 0.3)
    protect_existing: bool = True
    ignore_nets: tuple[str, ...] = ()  # nets left out of the network (e.g. planes the router must not route)
    keepout_npth_margin: float = 0.3
    min_hole_to_hole: float = 0.45  # KiCad's board setup default; sets the via-to-via clearance rule
    exclude_nets: tuple[str, ...] = ()  # net names or wildcard patterns: protected, in the network, never routed
    exclude_classes: tuple[str, ...] = ()  # net classes excluded the same way
    auto_exclude_ruled_nets: bool = False  # also exclude nets the .kicad_dru rules or their pour width make unroutable
    force_nets: tuple[str, ...] = ()  # nets never auto-excluded, whatever the rules say
    pour_width_mm: float = 2.0  # a net wider than this with a zone on it is a pour, not a track
    rules_path: Path | None = None  # the .kicad_dru; default: next to the project or the board


@dataclass
class DsnExport:
    """The DSN text and what the export decided on the router's behalf."""

    text: str
    excluded: dict[str, str] = field(default_factory=dict)  # net -> why it is not routed
    ignore_classes: list[str] = field(default_factory=list)  # pass these to FreeRouting's -inc
    class_rules: list[str] = field(default_factory=list)  # class_class rules emitted, readable
    keepouts: list[str] = field(default_factory=list)  # keep-out entries emitted, readable
    warnings: list[str] = field(default_factory=list)


def _dsn_class(cls: str) -> str:
    return "kicad_default" if cls == "Default" else cls


EXCLUDED_SUFFIX = "_excluded"


def _expand_layers(names: list[str], copper: list[str]) -> list[str]:
    out: list[str] = []
    for n in names:
        if n in ("*.Cu", "*.*"):
            out += copper
        elif n == "F&B.Cu":
            out += [copper[0], copper[-1]]
        elif n in copper:
            out.append(n)
    return list(dict.fromkeys(out))


def _zone_layers(z) -> list[str]:
    return [str(a) for ln in children(z, "layers") + children(z, "layer") for a in ln[1:]]


def _zone_outline(z) -> list[tuple[float, float]]:
    poly = child(z, "polygon")
    pts = child(poly, "pts") if poly is not None else None
    return [(float(xy[1]), float(xy[2])) for xy in children(pts, "xy")] if pts is not None else []


def _all_zones(root) -> list:
    """Every zone node: the board's and those inside footprints (stored in board coordinates)."""
    out = list(children(root, "zone"))
    for fp in children(root, "footprint"):
        out += children(fp, "zone")
    return out


def _poly(layer: str, pts: list[tuple[float, float]]) -> str:
    return f"(polygon {layer} 0 " + " ".join(_pt(x, y) for x, y in pts) + ")"


def _rule_layers(rule_layer: str | None, layers: list[str], copper: list[str]) -> list[str]:
    if not rule_layer:
        return layers
    if rule_layer == "outer":
        keep = {copper[0], copper[-1]}
    elif rule_layer == "inner":
        keep = set(copper[1:-1])
    else:
        keep = {rule_layer}
    return [l for l in layers if l in keep]


def export_dsn(board: Path, project: Path | None = None, *, options: DsnOptions | None = None) -> str:
    return build_dsn(board, project, options=options).text


def build_dsn(board: Path, project: Path | None = None, *, options: DsnOptions | None = None) -> DsnExport:
    opt = options or DsnOptions()
    bm = load_board(board)
    if project is None:
        cand = board.with_suffix(".kicad_pro")
        project = cand if cand.is_file() else None
    classes, assignments = load_netclasses(project)
    copper = ["F.Cu"] + [f"In{i}.Cu" for i in range(1, bm.copper_layers - 1)] + ["B.Cu"] if bm.copper_layers > 2 else ["F.Cu", "B.Cu"]
    planes = dict(opt.plane_layers)
    if not planes:
        for z in bm.zones:
            for l in z.layers:
                if l in copper[1:-1] and z.net:
                    planes[l] = z.net
    routable = opt.routable_layers or [l for l in copper if l not in planes]
    report = DsnExport(text="")
    root = parse(board.read_text(encoding="utf-8", errors="replace"))
    zones = _all_zones(root)

    # ---- nets, classes, and what the router must leave alone
    nets: dict[str, list[str]] = {}
    for f in bm.footprints:
        for p in f.pads:
            if p.net and p.kind != "np_thru_hole" and p.number:
                nets.setdefault(p.net, []).append(f"{f.ref}-{p.number}")
    class_of = {n: netclass_for(n, classes, assignments)[0] for n in nets}
    rules = dru_mod.load_rules(opt.rules_path or dru_mod.dru_for(board, project))
    excluded = report.excluded
    for pat in opt.exclude_nets:
        hits = [n for n in nets if fnmatch.fnmatchcase(n, pat)]
        if not hits:
            report.warnings.append(f"exclude_nets: no net on the board matches {pat!r}.")
        for n in hits:
            excluded.setdefault(n, "excluded by the caller")
    for cls in opt.exclude_classes:
        want = "Default" if cls == "kicad_default" else cls
        hits = [n for n in nets if class_of[n] == want]
        if not hits:
            report.warnings.append(f"exclude_classes: no net on the board is in class {cls!r}.")
        for n in hits:
            excluded.setdefault(n, f"class {cls} excluded by the caller")
    candidates: dict[str, list[str]] = {}
    net_names = list(nets)
    width_rule: dict[str, float] = {}
    for rule in rules:
        named = dru_mod.nets_of_rule(rule, net_names, class_of)
        unexp = sorted(rule.kinds() & set(dru_mod.UNEXPRESSIBLE))
        areas = rule.area_functions()
        for c in rule.constraints:
            if c.kind == "track_width" and (c.min or c.opt) and named:
                for n in named:
                    width_rule[n] = max(width_rule.get(n, 0.0), c.min or c.opt or 0.0)
        if not unexp and not areas:
            continue
        what = ", ".join(unexp + [f"{fn}('{arg}')" for fn, arg in areas])
        if named:
            for n in named:
                candidates.setdefault(n, []).append(f"rule '{rule.name}' ({what}) is not expressible in the DSN")
        elif not ("disallow" in unexp and areas):  # an area-only disallow becomes a keep-out below
            report.warnings.append(f"Rule '{rule.name}' ({what}) names no net or class; FreeRouting cannot honour it, check the result with run_drc.")
    zone_nets = {value(z, "net") for z in zones if child(z, "keepout") is None and value(z, "net")}
    for n in nets:
        w = max(float(classes.get(class_of[n], {}).get("track_width") or 0.0), width_rule.get(n, 0.0))
        if w > opt.pour_width_mm:
            if n in zone_nets:
                candidates.setdefault(n, []).append(f"track width {w:g} mm > {opt.pour_width_mm:g} mm and a zone on the net: a pour, not a track")
            else:
                report.warnings.append(f"{n}: track width {w:g} mm and no zone on the net; FreeRouting will draw {w:g} mm tracks.")
    for n, why in sorted(candidates.items()):
        if n in excluded:
            continue
        if n in opt.force_nets:
            report.warnings.append(f"{n} is routed on request although " + "; ".join(why) + ".")
        elif opt.auto_exclude_ruled_nets:
            excluded[n] = "; ".join(why)
        else:
            report.warnings.append(f"{n} should not be autorouted: " + "; ".join(why) + ".")
    auto = sorted(n for n in excluded if n in candidates)
    if auto:
        report.warnings.append("Not routed because of the design rules (route by hand or pass force_nets): " + ", ".join(auto) + ".")

    def class_key(n: str) -> str:
        base = _dsn_class(class_of[n])
        return base + EXCLUDED_SUFFIX if n in excluded else base

    out: list[str] = []
    w = out.append
    w(f"(pcb {_q(board.name.replace('.kicad_pcb', '.dsn'))}")
    w("  (parser")
    w('    (string_quote ")')
    w("    (space_in_quoted_tokens on)")
    # FreeRouting keys compatibility behaviour on the host string and warns about "old KiCad" otherwise
    w("    (host_cad \"KiCad's Pcbnew\")")
    w('    (host_version "10.0.6 (kicad-mcp-layer)")')
    w("  )")
    w("  (resolution um 10)")
    w("  (unit um)")
    # ---- structure
    w("  (structure")
    for i, l in enumerate(copper):
        w(f"    (layer {l} (type {'power' if l in planes else 'signal'}) (property (index {i})))")
    loops = outline_loops(board)
    if not loops:
        raise ValueError("the board has no Edge.Cuts outline")
    w("    (boundary")
    w("      (path pcb 0 " + " ".join(_pt(x, y) for x, y in loops[0]) + ")")
    w("    )")
    for hole in loops[1:]:
        w('    (keepout "" (polygon signal 0 ' + " ".join(_pt(x, y) for x, y in hole) + "))")
    for f in bm.footprints:
        for p in f.pads:
            if p.kind == "np_thru_hole" and p.drill:
                d = (max(p.drill, max(p.size)) + 2 * max(opt.keepout_npth_margin, p.clearance)) * UM
                w(f'    (keepout "" (circle signal {_num(d)} {_pt(p.x, p.y)}))')
    for layer, net in planes.items():
        for z in bm.zones:
            if layer in z.layers and z.net == net and z.polygon:
                w(f"    (plane {_q(net)} (polygon {layer} 0 " + " ".join(_pt(x, y) for x, y in z.polygon) + "))")
                break
    # rule areas: keep-out zones on the board and inside footprints, per copper layer
    by_name: dict[str, tuple[list[str], list[tuple[float, float]]]] = {}
    for z in zones:
        pts = _zone_outline(z)
        layers = _expand_layers(_zone_layers(z), copper)
        zname = value(z, "name") or ""
        if zname and pts:
            by_name.setdefault(zname, (layers, pts))
        ko = child(z, "keepout")
        if ko is None or len(pts) < 3:
            continue
        flags = {str(c[0]): str(c[1]) for c in ko[1:] if isinstance(c, list) and len(c) > 1}
        label = zname or "rule area"
        if flags.get("tracks") == "not_allowed":
            for l in layers:
                w(f"    (keepout {_q(label)} {_poly(l, pts)})")
            report.keepouts.append(f"keepout '{label}' on {', '.join(layers)}" + (" (vias too)" if flags.get("vias") == "allowed" else ""))
        elif flags.get("vias") == "not_allowed":
            for l in layers:
                w(f"    (via_keepout {_q(label)} {_poly(l, pts)})")
            report.keepouts.append(f"via_keepout '{label}' on {', '.join(layers)}")
        if flags.get("footprints") == "not_allowed" and layers:
            w(f"    (place_keepout {_q(label)} {_poly(layers[0], pts)})")
            report.keepouts.append(f"place_keepout '{label}'")
    # .kicad_dru rules that disallow vias or tracks inside a named area, for every net
    for rule in rules:
        dis = [c for c in rule.constraints if c.kind == "disallow"]
        areas = [(fn, arg) for fn, arg in rule.area_functions() if fn in ("insideArea", "enclosedByArea", "intersectsArea")]
        if not dis or not areas or rule.named_classes() or rule.named_nets():
            continue
        items = {a for c in dis for a in c.args}
        for _, arg in areas:
            if arg not in by_name:
                report.warnings.append(f"Rule '{rule.name}' refers to area {arg!r}, which is not on the board.")
                continue
            layers, pts = by_name[arg]
            layers = _rule_layers(rule.layer, layers, copper)
            if any(a in items for a in ("track", "tracks")):
                kind = "keepout"
            elif any("via" in a for a in items):
                kind = "via_keepout"
            else:
                report.warnings.append(f"Rule '{rule.name}' disallows {', '.join(sorted(items))} in {arg!r}; the DSN has no keep-out for that.")
                continue
            for l in layers:
                w(f"    ({kind} {_q(arg)} {_poly(l, pts)})")
            report.keepouts.append(f"{kind} '{arg}' on {', '.join(layers)} from rule '{rule.name}'")
    # the copper of excluded nets' pours: their fill is an obstacle, their outline only a plane when unfilled
    for z in children(root, "zone"):
        n = value(z, "net")
        if n not in excluded or child(z, "keepout") is not None:
            continue
        fills = children(z, "filled_polygon")
        done = False
        for fp in fills:
            l = value(fp, "layer")
            pts = [(float(xy[1]), float(xy[2])) for xy in children(child(fp, "pts"), "xy")] if child(fp, "pts") is not None else []
            if l in routable and len(pts) >= 3:
                w(f"    (keepout {_q('pour ' + n)} {_poly(l, pts)})")
                done = True
        if done:
            report.keepouts.append(f"keepout for the filled pour of {n}")
        elif fills == [] and _zone_outline(z):
            for l in _expand_layers(_zone_layers(z), copper):
                if l in routable:
                    w(f"    (plane {_q(n)} {_poly(l, _zone_outline(z))})")
            report.warnings.append(f"The zone of {n} is unfilled, so only its outline goes to the router as a plane; refill (pcb_refill_zones) before autoroute to protect its copper.")
    via_names: dict[tuple[float, float], str] = {}

    def via_name(size: float, drill: float) -> str:
        key = (round(size, 4), round(drill, 4))
        if key not in via_names:
            via_names[key] = via_padstack_name(size, drill, len(copper))
        return via_names[key]

    default_via = via_name(*opt.default_via)
    for c in classes.values():
        if c.get("via_diameter") and c.get("via_drill"):
            via_name(float(c["via_diameter"]), float(c["via_drill"]))
    for v in bm.vias:
        via_name(v.size, v.drill)
    w("    (via " + " ".join(_q(n) for n in via_names.values()) + ")")
    default = classes.get("Default", {})
    dw = float(default.get("track_width", 0.15)) * UM
    dc = float(default.get("clearance", 0.125)) * UM
    dv, dd = opt.default_via
    via_via = max(dc, (opt.min_hole_to_hole - (dv - dd) + 0.02) * UM)  # so two drills never come closer than the board allows
    w(f"    (rule (width {_num(dw)}) (clearance {_num(dc)}) (clearance {_num(dc)} (type default_smd)) (clearance {_num(dc / 2)} (type smd_smd)) (clearance {_num(via_via)} (type via_via)))")
    w("  )")
    # ---- placement and library
    padstacks: dict[str, tuple[list[str], list[str]]] = {}
    images: list[str] = []
    w("  (placement")
    for f in bm.footprints:
        w(f"    (component {_q(f.ref)}")
        w(f"      (place {_q(f.ref)} {_pt(f.x, f.y)} front {_num(f.rotation)})")
        w("    )")
    w("  )")
    w("  (library")
    for f in bm.footprints:
        pins = []
        for p in f.pads:
            if p.kind == "np_thru_hole" or not p.number:
                continue
            name, layers, shapes = _padstack(p, copper, f.rotation)
            padstacks.setdefault(name, (layers, shapes))
            lx, ly = rotate_about(p.x - f.x, p.y - f.y, -f.rotation, 0.0, 0.0)
            pins.append(f"      (pin {_q(name)} {_q(p.number)} {_pt(lx, ly)})")
        w(f"    (image {_q(f.ref)}")
        out.extend(pins)
        w("    )")
    for name, (layers, shapes) in padstacks.items():
        w(f"    (padstack {_q(name)}")
        for l in layers:
            for s in shapes:
                w(f"      (shape {s.replace('{L}', l)})")
        w("      (attach off)")
        w("    )")
    for (size, drill), name in via_names.items():
        w(f"    (padstack {_q(name)}")
        for l in copper:
            w(f"      (shape (circle {l} {_num(size * UM)}))")
        w("      (attach off)")
        w("    )")
    w("  )")
    # ---- network
    w("  (network")
    for net, pins in nets.items():
        if net in opt.ignore_nets:
            continue
        w(f"    (net {_q(net)}")
        w("      (pins " + " ".join(_q(x) for x in pins) + ")")
        w("    )")
    by_class: dict[str, list[str]] = {}
    for net in nets:
        if net in opt.ignore_nets:
            continue
        by_class.setdefault(class_key(net), []).append(net)
    for key, members in by_class.items():
        cls = key[: -len(EXCLUDED_SUFFIX)] if key.endswith(EXCLUDED_SUFFIX) and members[0] in excluded else key
        cls = "Default" if cls == "kicad_default" else cls
        c = classes.get(cls, default)
        cw = float(c.get("track_width", dw / UM)) * UM
        cc = float(c.get("clearance", dc / UM)) * UM
        v = via_name(float(c["via_diameter"]), float(c["via_drill"])) if c.get("via_diameter") and c.get("via_drill") else default_via
        w(f"    (class {_q(key)} " + " ".join(_q(n) for n in members))
        w(f"      (circuit (use_via {_q(v)}))")
        w(f"      (rule (width {_num(cw)}) (clearance {_num(cc)}))")
        w("    )")
        if key.endswith(EXCLUDED_SUFFIX) and members[0] in excluded:
            report.ignore_classes.append(key)
    # plain two-class clearance rules of the .kicad_dru; creepage and physical clearance as a straight clearance
    present = sorted({class_of[n] for n in nets if n not in opt.ignore_nets})
    variants = {cls: [k for k in by_class if k in (_dsn_class(cls), _dsn_class(cls) + EXCLUDED_SUFFIX)] for cls in present}
    pair_min: dict[tuple[str, str], tuple[float, str]] = {}
    for rule in rules:
        for con in rule.constraints:
            if con.kind not in dru_mod.CLEARANCE_KINDS or not con.min:
                continue
            pairs, why = dru_mod.class_pairs(rule, present)
            if not pairs:
                if rule.condition and (rule.named_classes() or rule.named_nets() or "NetClass" in rule.condition):
                    report.warnings.append(f"Rule '{rule.name}' ({con.kind} {con.min:g} mm) is not a plain two-class rule ({why}); the router does not see it.")
                continue
            for x, y in pairs:
                k = tuple(sorted((x, y)))
                if con.min > pair_min.get(k, (0.0, ""))[0]:
                    pair_min[k] = (con.min, f"{rule.name}" + ("" if con.kind == "clearance" else f", {con.kind} as clearance"))
    for (x, y), (mm_, src) in sorted(pair_min.items()):
        for a in variants.get(x, []):
            for b in variants.get(y, []):
                if x == y and b < a:
                    continue
                w(f"    (class_class (classes {_q(a)} {_q(b)}) (rule (clearance {_num(mm_ * UM)})))")
        report.class_rules.append(f"{_dsn_class(x)} / {_dsn_class(y)}: {mm_:g} mm ({src})")
    w("  )")
    # ---- existing copper
    w("  (wiring")
    if opt.protect_existing:
        for s in bm.segments:
            if s.net and s.layer in copper:
                w(f"    (wire (path {s.layer} {_num(s.width * UM)} {_pt(s.x1, s.y1)} {_pt(s.x2, s.y2)}) (net {_q(s.net)}) (type protect))")
        for v in bm.vias:
            if v.net:
                w(f"    (via {_q(via_name(v.size, v.drill))} {_pt(v.x, v.y)} (net {_q(v.net)}) (type protect))")
    w("  )")
    w(")")
    report.text = "\n".join(out) + "\n"
    return report


def write_dsn(board: Path, dest: Path, project: Path | None = None, *, options: DsnOptions | None = None) -> Path:
    write_dsn_export(board, dest, project, options=options)
    return dest


def write_dsn_export(board: Path, dest: Path, project: Path | None = None, *, options: DsnOptions | None = None) -> DsnExport:
    exp = build_dsn(board, project, options=options)
    # LF only: FreeRouting's scanner reports every carriage return as a stray character and misparses the file
    with open(dest, "w", encoding="utf-8", newline=chr(10)) as f:
        f.write(exp.text)
    return exp
