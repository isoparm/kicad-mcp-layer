"""Differential pairs, intra-pair length matching and impedance, from the board file.

What this module knows how to do:

* **Find pairs by name.** ``X_P``/``X_N``, ``X_DP``/``X_DN``, ``X+``/``X-`` and ``XP``/``XN`` are
  pairs; hierarchical prefixes (``/CM5/DSI_D0_P``) are kept on the net and stripped from the
  pair's name.
* **Measure each half.** Routed length is the sum of the net's track segments plus a fixed
  length per via (the board thickness by default, which is what KiCad adds too). Skew is the
  difference between the halves. Segments of the two halves that run parallel on one layer are
  checked for the class's pair gap and width; the share of the shorter half that runs coupled
  is reported, because an uncoupled stretch is where impedance drifts.
* **Apply interface rules.** Defaults come from the Compute Module 5 datasheet (pages 8 to 10):
  Ethernet and MIPI 100 ohm within 0.15 mm, PCIe and USB 3.0 90 ohm within 0.1 mm, USB 2.0
  90 ohm within 0.15 mm. They are ordinary for these interfaces, and callers can override them.
* **Estimate impedance** for a stack-up preset with closed-form microstrip formulas
  (Hammerstad and Jensen for the single line, the usual coupled-line correction for the pair).
  Closed forms are about 10 percent optimistic against a field solver for tightly coupled
  pairs, so a fab's published table wins whenever the geometry matches one of its entries; the
  result says which it used.

Nothing here draws tracks. It measures what the writer or the user drew, so a generator can
call it after every routing step and a reviewer can trust the numbers it prints.
"""

from __future__ import annotations

import fnmatch
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import INVALID_ARGUMENT, LayerError
from .models import DiffPairReport, ImpedanceResult, RouteReport, StackupInfo
from .review import BoardModel, SegGeo, load_board

# --------------------------------------------------------------------------------------
# stack-ups
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stackup:
    name: str
    source: str
    # (layer name, thickness mm, relative permittivity or None for copper)
    layers: tuple[tuple[str, float, float | None], ...]
    # fab-published geometries per target impedance: {"w": width, "s": gap (pairs only)}
    table: dict[int, dict[str, float]] = field(default_factory=dict)

    @property
    def outer_dielectric(self) -> tuple[float, float]:
        """Thickness and permittivity of the dielectric between an outer layer and the first plane."""
        for _, th, er in self.layers[1:]:
            if er is not None:
                return th, er
        raise LayerError(INVALID_ARGUMENT, f"stack-up {self.name} has no dielectric layer")

    @property
    def outer_copper(self) -> float:
        return self.layers[0][1]

    @property
    def thickness(self) -> float:
        return round(sum(th for _, th, _ in self.layers), 3)


JLC04161H_7628 = Stackup(
    name="JLC04161H-7628",
    source="JLCPCB 4-layer 1.6 mm standard stack-up; impedance geometries as published by JLCPCB's calculator "
    "(docs.jitx.com JLC04161H_7628 module, read 2026-09-05). Confirm in JLCPCB's impedance calculator when ordering.",
    layers=(
        ("F.Cu", 0.035, None),
        ("prepreg 7628", 0.2104, 4.4),
        ("In1.Cu", 0.0152, None),
        ("core", 1.065, 4.6),
        ("In2.Cu", 0.0152, None),
        ("prepreg 7628", 0.2104, 4.4),
        ("B.Cu", 0.035, None),
    ),
    table={50: {"w": 0.3244}, 90: {"w": 0.2332, "s": 0.15}, 100: {"w": 0.1722, "s": 0.15}},
)

PCBWAY_4L_1P6 = Stackup(
    name="PCBWay-4L-1.6mm",
    source="PCBWay standard 4-layer 1.6 mm through-hole stack-up, 1 oz outer (0.5 oz base plated to 1 oz) / 1 oz inner, "
    "70 percent inner residual copper (pcbway.com/multi-layer-laminated-structure.html, read 2026-09-06): 7628 RC46% prepreg "
    "DK 4.74, 0.1960 mm raw and 0.1855 mm after lamination (0.1785 mm at 50 percent, 0.1715 mm at 30 percent residual copper); "
    "core DK 4.6, 1.03 mm; finished 1.61 mm +-10 percent. PCBWay publishes no width table for it and its online calculator "
    "(pcbway.com/pcb_prototype/impedance_calculator.html) is the IPC-2141 closed form, so the estimate here is all there is; "
    "order with impedance control and let PCBWay's engineers confirm the widths.",
    layers=(
        ("F.Cu", 0.035, None),
        ("prepreg 7628 RC46%", 0.1855, 4.74),
        ("In1.Cu", 0.035, None),
        ("core", 1.03, 4.6),
        ("In2.Cu", 0.035, None),
        ("prepreg 7628 RC46%", 0.1855, 4.74),
        ("B.Cu", 0.035, None),
    ),
)

AISLER_4L_1P6 = Stackup(
    name="AISLER-4L-1.6mm",
    source="AISLER 4-layer 1.6 mm 35 um stack-up (community.aisler.net/t/4-layers-1-6mm-35-m-stackup/5457, 2025-12-01: two 1080 "
    "prepregs of 70 um at Dk 4.0-4.3 each side, core 1200 um at Dk 4.5-4.6, finished 1.6 mm +-10 percent), layer values as in "
    "AISLER's own KiCad template (github.com/AislerHQ/aisler-support, kicad/aisler-4-layer-hd-drc: prepreg Panasonic R-1551(W) "
    "0.138 mm Dk 4.3, core R-1566(W) 1.113 mm Dk 4.6, mask 0.025 mm Dk 3.7). Impedance geometries as published on the stack-up "
    "page (read 2026-09-08), which AISLER calls a basic orientation; it does not measure impedance on standard orders.",
    layers=(
        ("F.Cu", 0.035, None),
        ("prepreg 2x1080 R-1551(W)", 0.138, 4.3),
        ("In1.Cu", 0.035, None),
        ("core R-1566(W)", 1.113, 4.6),
        ("In2.Cu", 0.035, None),
        ("prepreg 2x1080 R-1551(W)", 0.138, 4.3),
        ("B.Cu", 0.035, None),
    ),
    table={50: {"w": 0.295}, 90: {"w": 0.26, "s": 0.135}, 100: {"w": 0.22, "s": 0.15}},
)

STACKUPS: dict[str, Stackup] = {"jlc04161h-7628": JLC04161H_7628, "pcbway-4l-1.6mm": PCBWAY_4L_1P6, "aisler-4l-1.6mm": AISLER_4L_1P6}
# short names accepted by get_stackup
STACKUP_ALIASES: dict[str, str] = {"jlcpcb": "jlc04161h-7628", "jlc": "jlc04161h-7628", "pcbway": "pcbway-4l-1.6mm", "aisler": "aisler-4l-1.6mm"}


def get_stackup(name: str) -> Stackup:
    key = name.lower().replace("_", "-")
    key = STACKUP_ALIASES.get(key, key)
    if key not in STACKUPS:
        raise LayerError(INVALID_ARGUMENT, f"unknown stack-up {name!r}", hint=f"known: {', '.join(sorted(STACKUPS))}")
    return STACKUPS[key]


def stackup_info(name: str) -> StackupInfo:
    s = get_stackup(name)
    return StackupInfo(
        name=s.name,
        thickness_mm=s.thickness,
        layers=[{"layer": n, "thickness_mm": th, "er": er} for n, th, er in s.layers],
        table={str(k): v for k, v in s.table.items()},
        source=s.source,
        presets=sorted(STACKUPS),
    )


# --------------------------------------------------------------------------------------
# impedance estimates
# --------------------------------------------------------------------------------------

ETA0 = 376.730313


def microstrip_z0(w: float, h: float, t: float, er: float) -> tuple[float, float]:
    """Single microstrip over a plane: Hammerstad and Jensen (1980) with the thickness correction.

    Returns (Z0 in ohm, effective permittivity). Inputs in mm."""
    if w <= 0 or h <= 0 or er <= 1:
        raise LayerError(INVALID_ARGUMENT, "width, height and permittivity must be positive (er > 1)")
    u = w / h
    if t > 0:
        th = t / h
        coth = 1.0 / math.tanh(math.sqrt(6.517 * u))
        du1 = th / math.pi * math.log(1.0 + 4.0 * math.e / (th * coth * coth))
        dur = 0.5 * (1.0 + 1.0 / math.cosh(math.sqrt(er - 1.0))) * du1
        u = u + dur
    a = 1.0 + math.log((u**4 + (u / 52.0) ** 2) / (u**4 + 0.432)) / 49.0 + math.log(1.0 + (u / 18.1) ** 3) / 18.7
    b = 0.564 * ((er - 0.9) / (er + 3.0)) ** 0.053
    er_eff = (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * (1.0 + 10.0 / u) ** (-a * b)
    f = 6.0 + (2.0 * math.pi - 6.0) * math.exp(-((30.666 / u) ** 0.7528))
    z0 = ETA0 / (2.0 * math.pi * math.sqrt(er_eff)) * math.log(f / u + math.sqrt(1.0 + (2.0 / u) ** 2))
    return z0, er_eff


def coupled_microstrip_zdiff(w: float, s: float, h: float, t: float, er: float) -> float:
    """Edge-coupled microstrip differential impedance: 2 Z0 (1 - 0.48 exp(-0.96 s/h)).

    The classic closed form; within about ten percent of a field solver for 0.1 < s/h < 3."""
    z0, _ = microstrip_z0(w, h, t, er)
    return 2.0 * z0 * (1.0 - 0.48 * math.exp(-0.96 * s / h))


def impedance(width_mm: float, gap_mm: float | None = None, stackup: str = "jlc04161h-7628") -> ImpedanceResult:
    s = get_stackup(stackup)
    h, er = s.outer_dielectric
    t = s.outer_copper
    z0, er_eff = microstrip_z0(width_mm, h, t, er)
    zdiff = coupled_microstrip_zdiff(width_mm, gap_mm, h, t, er) if gap_mm else None
    match = None
    for target, geo in s.table.items():
        if abs(geo["w"] - width_mm) <= 0.005 and ((gap_mm is None and "s" not in geo) or (gap_mm is not None and "s" in geo and abs(geo["s"] - gap_mm) <= 0.005)):
            match = {"target_ohm": target, **geo}
    return ImpedanceResult(
        stackup=s.name,
        layer="outer, referenced to the first inner plane",
        width_mm=width_mm,
        gap_mm=gap_mm,
        dielectric_mm=h,
        er=er,
        er_effective=round(er_eff, 3),
        single_ended_ohm=round(z0, 1),
        differential_ohm=round(zdiff, 1) if zdiff is not None else None,
        table_match=match,
        method="Hammerstad-Jensen microstrip" + ("; coupled-line correction 2 Z0 (1 - 0.48 exp(-0.96 s/h))" if zdiff is not None else ""),
        uncertainty="closed forms, about +-10 percent; when table_match is set the fab's number is the one to trust",
        source=s.source,
    )


def suggest_geometry(target_ohm: float, stackup: str = "jlc04161h-7628", gap_mm: float | None = None, differential: bool = True) -> dict[str, Any]:
    """The fab's table entry for the target, or a width solved from the estimate at the given gap."""
    s = get_stackup(stackup)
    key = int(round(target_ohm))
    if key in s.table and (("s" in s.table[key]) == differential):
        return {"target_ohm": key, **s.table[key], "from": "fab table", "source": s.source}
    h, er = s.outer_dielectric
    t = s.outer_copper
    gap = gap_mm if gap_mm is not None else 0.15
    lo, hi = 0.08, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        z = coupled_microstrip_zdiff(mid, gap, h, t, er) if differential else microstrip_z0(mid, h, t, er)[0]
        if z > target_ohm:
            lo = mid  # wider lowers the impedance
        else:
            hi = mid
    out = {"target_ohm": target_ohm, "w": round((lo + hi) / 2, 4), "from": "closed-form estimate, verify with the fab's calculator"}
    if differential:
        out["s"] = gap
    return out


# --------------------------------------------------------------------------------------
# pairs
# --------------------------------------------------------------------------------------

PAIR_SUFFIXES: tuple[tuple[str, str], ...] = (("_P", "_N"), ("_DP", "_DN"), ("+", "-"), ("P", "N"))

# (regex on the pair's name, target impedance, max intra-pair skew mm, source)
INTERFACE_RULES: tuple[tuple[str, float, float, str], ...] = (
    (r"(ETH|TRD|MDI)", 100.0, 0.15, "CM5 datasheet 2.2.1: Ethernet 100 ohm, pairs matched within 0.15 mm"),
    (r"PCIE|PCI_?E|PET|PER|REFCLK", 90.0, 0.10, "CM5 datasheet 2.3.1: PCIe 90 ohm, within 0.1 mm"),
    (r"USB3|SS(RX|TX)|SSRX|SSTX", 90.0, 0.10, "CM5 datasheet 2.4.1: USB 3.0 90 ohm, within 0.1 mm"),
    (r"USB", 90.0, 0.15, "CM5 datasheet 2.4.2: USB 2.0 90 ohm, within 0.15 mm"),
    (r"DSI|CAM|CSI|MIPI|DPHY", 100.0, 0.15, "CM5 datasheet 2.5.2: MIPI 100 ohm, within 0.15 mm"),
    (r"HDMI|TMDS", 100.0, 0.15, "CM5 datasheet 2.5.1: HDMI 100 ohm, within 0.15 mm"),
)


def pair_name(net: str) -> str:
    return net.rsplit("/", 1)[-1]


def find_pairs(nets: list[str]) -> tuple[list[tuple[str, str, str]], list[str]]:
    """(name, positive net, negative net) for every pair, plus lone nets that look like a half."""
    by_local = {pair_name(n): n for n in nets}
    pairs: list[tuple[str, str, str]] = []
    used: set[str] = set()
    for local, net in sorted(by_local.items()):
        for sp, sn in PAIR_SUFFIXES:
            if local.endswith(sp) and len(local) > len(sp):
                base = local[: -len(sp)]
                mate = base + sn
                if mate in by_local and net not in used and by_local[mate] not in used:
                    if sp == "P" and not (base.endswith("_") or base[-1:].isdigit() or base.upper().endswith(("D", "TX", "RX", "CLK"))):
                        continue  # avoid pairing arbitrary words ending in P and N
                    pairs.append((base.rstrip("_"), net, by_local[mate]))
                    used.update({net, by_local[mate]})
                    break
    lone = []
    for local, net in sorted(by_local.items()):
        if net in used:
            continue
        if re.search(r"(_P|_N|_DP|_DN)$", local):
            lone.append(net)
    return pairs, lone


def rule_for(name: str, rules: tuple[tuple[str, float, float, str], ...] = INTERFACE_RULES) -> tuple[float, float, str] | None:
    for pattern, z, skew, source in rules:
        if re.search(pattern, name, re.I):
            return z, skew, source
    return None


# --------------------------------------------------------------------------------------
# net classes from the project file
# --------------------------------------------------------------------------------------


def load_netclasses(project: Path | None) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str]]]:
    """Classes by name and the (pattern, class) assignment list, from a .kicad_pro."""
    if project is None or not project.is_file():
        return {}, []
    raw = json.loads(project.read_text(encoding="utf-8"))
    ns = raw.get("net_settings") or {}
    classes = {c.get("name", ""): c for c in ns.get("classes") or [] if isinstance(c, dict)}
    assignments: list[tuple[str, str]] = []
    for a in ns.get("netclass_assignments") or []:
        if isinstance(a, dict) and a.get("pattern") and a.get("netclass"):
            assignments.append((a["pattern"], a["netclass"]))
    for a in ns.get("netclass_patterns") or []:
        if isinstance(a, dict) and a.get("pattern") and a.get("netclass"):
            assignments.append((a["pattern"], a["netclass"]))
    return classes, assignments


def netclass_for(net: str, classes: dict[str, dict[str, Any]], assignments: list[tuple[str, str]]) -> tuple[str, dict[str, Any]]:
    for pattern, cls in assignments:
        if fnmatch.fnmatchcase(net, pattern) or fnmatch.fnmatchcase(pair_name(net), pattern):
            return cls, classes.get(cls, {})
    return "Default", classes.get("Default", {})


# --------------------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------------------


def _length(s: SegGeo) -> float:
    return math.hypot(s.x2 - s.x1, s.y2 - s.y1)


def _parallel_overlap(a: SegGeo, b: SegGeo, angle_tol_deg: float = 5.0) -> tuple[float, float] | None:
    """(overlap length, centre-line distance) when b runs parallel to a and overlaps it in projection."""
    ax, ay = a.x2 - a.x1, a.y2 - a.y1
    la = math.hypot(ax, ay)
    lb = math.hypot(b.x2 - b.x1, b.y2 - b.y1)
    if la < 1e-6 or lb < 1e-6:
        return None
    ux, uy = ax / la, ay / la
    bx, by = (b.x2 - b.x1) / lb, (b.y2 - b.y1) / lb
    cross = abs(ux * by - uy * bx)
    if cross > math.sin(math.radians(angle_tol_deg)):
        return None
    # projections of b's ends onto a's axis
    t1 = (b.x1 - a.x1) * ux + (b.y1 - a.y1) * uy
    t2 = (b.x2 - a.x1) * ux + (b.y2 - a.y1) * uy
    lo, hi = max(0.0, min(t1, t2)), min(la, max(t1, t2))
    if hi - lo <= 0.05:
        return None
    dist = abs((b.x1 - a.x1) * (-uy) + (b.y1 - a.y1) * ux)
    return hi - lo, dist


def analyse(bm: BoardModel, project: Path | None = None, *, rules: tuple[tuple[str, float, float, str], ...] = INTERFACE_RULES,
            skew_limit_mm: float | None = None, via_length_mm: float | None = None, gap_tol_mm: float = 0.02, width_tol_mm: float = 0.005) -> RouteReport:
    classes, assignments = load_netclasses(project)
    via_len = via_length_mm if via_length_mm is not None else 1.6
    nets = sorted({s.net for s in bm.segments if s.net} | {p.net for f in bm.footprints for p in f.pads if p.net})
    pairs, lone = find_pairs(nets)
    segs_by_net: dict[str, list[SegGeo]] = {}
    for s in bm.segments:
        if s.net:
            segs_by_net.setdefault(s.net, []).append(s)
    vias_by_net: dict[str, int] = {}
    for v in bm.vias:
        if v.net:
            vias_by_net[v.net] = vias_by_net.get(v.net, 0) + 1

    reports: list[DiffPairReport] = []
    for name, p, n in pairs:
        cls_name, cls = netclass_for(p, classes, assignments)
        cls_n, _ = netclass_for(n, classes, assignments)
        rule = rule_for(name, rules)
        target_z = rule[0] if rule else None
        limit = skew_limit_mm if skew_limit_mm is not None else (rule[1] if rule else None)
        sp, sn = segs_by_net.get(p, []), segs_by_net.get(n, [])
        lp = sum(_length(s) for s in sp) + vias_by_net.get(p, 0) * via_len
        ln = sum(_length(s) for s in sn) + vias_by_net.get(n, 0) * via_len
        notes: list[str] = []
        if cls_name != cls_n:
            notes.append(f"halves are in different net classes: {p} in {cls_name}, {n} in {cls_n}")
        if rule:
            notes.append(rule[2])
        gap_target = cls.get("diff_pair_gap")
        width_target = cls.get("diff_pair_width") or cls.get("track_width")
        coupled = 0.0
        gap_dev = 0
        for a in sp:
            for b in sn:
                if a.layer != b.layer:
                    continue
                po = _parallel_overlap(a, b)
                if po is None:
                    continue
                overlap, dist = po
                edge_gap = dist - (a.width + b.width) / 2.0
                if gap_target is None or abs(edge_gap - gap_target) <= gap_tol_mm:
                    coupled += overlap
                else:
                    gap_dev += 1
        width_dev = 0
        if width_target is not None:
            width_dev = sum(1 for s in sp + sn if abs(s.width - width_target) > width_tol_mm)
        layers = sorted({s.layer for s in sp + sn})
        shorter = min(lp, ln)
        coupled_fraction = round(min(coupled / shorter, 1.0), 3) if shorter > 0 else None
        if lp == 0 and ln == 0:
            status = "unrouted"
        elif lp == 0 or ln == 0:
            status = "partial"
            notes.append("only one half is routed")
        else:
            status = "ok"
            if limit is not None and abs(lp - ln) > limit:
                status = "warn"
                notes.append(f"skew {abs(lp - ln):.3f} mm exceeds {limit} mm")
            if gap_dev:
                status = "warn"
                notes.append(f"{gap_dev} parallel run(s) off the class gap of {gap_target} mm")
            if width_dev:
                status = "warn"
                notes.append(f"{width_dev} segment(s) off the class width of {width_target} mm")
            if coupled_fraction is not None and coupled_fraction < 0.8:
                status = "warn"
                notes.append(f"only {coupled_fraction:.0%} of the pair runs coupled")
            if vias_by_net.get(p, 0) != vias_by_net.get(n, 0):
                status = "warn"
                notes.append("the halves change layers a different number of times")
        reports.append(DiffPairReport(
            name=name, p_net=p, n_net=n, netclass=cls_name, target_impedance_ohm=target_z, status=status,
            p_length_mm=round(lp, 3), n_length_mm=round(ln, 3), skew_mm=round(abs(lp - ln), 3), skew_limit_mm=limit,
            p_vias=vias_by_net.get(p, 0), n_vias=vias_by_net.get(n, 0), coupled_fraction=coupled_fraction,
            gap_target_mm=gap_target, gap_deviations=gap_dev, width_target_mm=width_target, width_deviations=width_dev, layers=layers, notes=notes,
        ))
    summary = {k: sum(1 for r in reports if r.status == k) for k in ("ok", "warn", "partial", "unrouted")}
    verdict = "WARN" if summary["warn"] or summary["partial"] else ("INFO" if reports and summary["ok"] == 0 else ("PASS" if reports else "EMPTY"))
    notes = []
    if not classes:
        notes.append("no .kicad_pro given: gaps and widths were not checked against net classes")
    if lone:
        notes.append(f"{len(lone)} net(s) look like half a pair without a mate")
    return RouteReport(board_path=str(bm.path), project_path=str(project) if project else None, pairs=reports, unpaired=lone, summary=summary, verdict=verdict, notes=notes)


def route_check(board: Path, project: Path | None = None, **kw: Any) -> RouteReport:
    bm = load_board(board)
    if project is None:
        cand = board.with_suffix(".kicad_pro")
        project = cand if cand.is_file() else None
    return analyse(bm, project, **kw)
