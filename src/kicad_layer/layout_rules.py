"""Layout rules beyond DRC: the checks a reviewer applies by eye, made measurable.

Each check reads the board model (and the zone fills KiCad wrote into the file), states what it
looked at, and returns a ``ReviewCheck``. They cover the common layout rules a design review
asks about:

1. ``via_in_pad``       vias drilled into surface-mount pads (solder wicks away)
2. ``test_points``      supply rails, ground, programming and bus nets reachable by a probe
3. ``thermal_pads``     exposed pads with thermal vias and copper under them
4. ``fast_edge``        fast nets kept away from the board edge (a multiple of the plane height)
5. ``stitching``        zone nets reachable on every layer, plane pairs stitched along the edge,
                        return vias beside fast-net layer changes
6. ``switcher_loop``    the switching regulator's hot loop: input capacitor at the IC, return short
7. diff pairs live in ``review.check_diff_pairs``
8. ``power_tracks``     power track widths against IPC-2221 ampacity, vias at layer changes
9. ``antenna``          antenna keep-outs free of copper
10. decoupling lives in ``review.check_decoupling``
11. ``plane_reference`` signals run over a continuous reference plane, no gaps or slots under them

The limits are rules of thumb, not physics: each check names its source, and anything it cannot
see (unfilled zones, no stack-up) makes it UNVERIFIED instead of silently passing.
"""

from __future__ import annotations

import fnmatch
import math
import re
from collections import defaultdict
from statistics import median
from typing import Any

from kicad_layer.geometry import FillIndex, Ring, ampacity, runs, sample_segment, via_ampacity
from kicad_layer.models import ReviewCheck, ReviewFinding
from kicad_layer.review import (
    POWER_NET,
    BoardModel,
    FpGeo,
    PadGeo,
    _check,
    _finding,
    _info,
    _unverified,
    is_connector,
)
from kicad_layer.sexpr import child, children, parse, tag, value

GROUND = re.compile(r"GND|VSS|EARTH|^0V$", re.IGNORECASE)
FAST_NET = re.compile(
    r"CLK|SCK|SCLK|BCLK|MCLK|LRCLK|\bWS\b|OSC|XTAL|XIN|XOUT|USB_?D|_DP$|_DM$|_DN$|D[+-]$|ETH|RMII|RGMII|MDI|SDIO|SD_(CMD|D)|QSPI|"
    r"MOSI|MISO|HDMI|TMDS|LVDS|MIPI|DSI|CSI|PCIE|DDR|I2S|SPDIF|SWCLK|TCK|PWM|_SW$|^SW$|LX",
    re.IGNORECASE,
)
PROGRAMMING = re.compile(r"SWDIO|SWCLK|\bSWO\b|NRST|^RESET|MCLR|^RST_?N?$|BOOT\d|BOOTSEL|BOOT_?MODE|^BOOT$|\bTCK\b|\bTMS\b|\bTDI\b|\bTDO\b|JTAG|UPDI|\bPDI\b|SWIM",
                         re.IGNORECASE)
# supply-named nets that are measurement or reference nodes, not rails to probe or decouple
NOT_A_RAIL = re.compile(r"ADC|SENS|DIV|_FB|FB_|REF|_DET|SHUNT", re.IGNORECASE)
BUS = re.compile(r"SDA|SCL|MOSI|MISO|SCK|\bCS\b|_CS|TXD?\b|RXD?\b|UART|I2S|CAN[_HL]|SPI|I2C|DIN|DOUT|LRCLK|BCLK", re.IGNORECASE)
ANTENNA = re.compile(r"^ANT\d|(^|[_:])ANT(_|$)|ANTENNA|WROOM|WROVER|ESP32|ESP-?12|NRF5\d+.*MOD|LORA|RFM9|SX12\d\d|CC1101|XBEE|BLUETOOTH|WIFI|CHIP_?ANT",
                     re.IGNORECASE)
SWITCH_NODE = re.compile(r"(^|_)(SW|LX|PH|PHASE)(\d?|_.*)$|_SW\d?$|SWITCH", re.IGNORECASE)
NOT_INPUT = re.compile(r"(^|_)(SS|SOFT|COMP|FB|RT|EN|BOOT|BST|CB|PG|SYNC|VCC_?INT|REF|ILIM|TRK|ADC|SENSE)(\d|_|$)", re.IGNORECASE)


# ---------------------------------------------------------------- shared facts
def plane_nets(bm: BoardModel) -> set[str]:
    """Nets with a copper zone: the planes and pours a signal can reference."""
    return {z.net for z in bm.zones if z.net and not z.rule_area}


def copper_order(bm: BoardModel) -> list[str]:
    n = bm.copper_layers
    return ["F.Cu"] + [f"In{i}.Cu" for i in range(1, n - 1)] + ["B.Cu"] if n > 2 else ["F.Cu", "B.Cu"]


def adjacent(bm: BoardModel, layer: str) -> list[str]:
    order = copper_order(bm)
    if layer not in order:
        return []
    i = order.index(layer)
    return [order[j] for j in (i - 1, i + 1) if 0 <= j < len(order)]


def dielectric_to_plane(board_text: str, layers: int) -> tuple[float, str]:
    """Thickness (mm) between an outer layer and the next copper, from the board's stack-up, and where it came from."""
    try:
        root = parse(board_text)
        setup = child(root, "setup")
        st = child(setup, "stackup") if setup is not None else None
        if st is not None:
            seen_copper = False
            for layer in children(st, "layer"):
                t = value(layer, "type") or ""
                name = str(layer[1]) if len(layer) > 1 else ""
                if name.endswith(".Cu"):
                    if seen_copper:
                        break
                    seen_copper = True
                    continue
                if seen_copper and t in ("core", "prepreg"):
                    th = child(layer, "thickness")
                    if th is not None and len(th) > 1:
                        try:
                            return float(th[1]), "board stack-up"
                        except (TypeError, ValueError):
                            pass
    except Exception:  # noqa: BLE001 - a stack-up we cannot read falls back to the defaults below
        pass
    return (1.51, "default 1.6 mm two-layer board") if layers <= 2 else (0.21, "default four-layer stack-up")


def is_fast(net: str | None, extra: list[str] | None = None) -> bool:
    if not net:
        return False
    if extra and any(fnmatch.fnmatch(net, p) for p in extra):
        return True
    return bool(FAST_NET.search(net)) and not GROUND.search(net)


def _pad_layer(p: PadGeo) -> str:
    return next((l for l in p.layers if l.endswith(".Cu") and "*" not in l), "F.Cu")


def _pad_area(p: PadGeo) -> float:
    if p.shape == "circle":
        return math.pi * (p.size[0] / 2) ** 2
    return p.size[0] * p.size[1]


def exposed_pad(fp: FpGeo) -> PadGeo | None:
    """The exposed (thermal) pad of an IC package (see ``design.copper.exposed_pad``)."""
    from kicad_layer.design.copper import exposed_pad as _ep

    return _ep(fp)


def pad_gap(a: PadGeo, b: PadGeo) -> float:
    """Copper-to-copper distance between two pads by their real shapes (0 when they touch)."""
    from kicad_layer.design.copper import _rect_to_item, pad_item

    ia, ib = pad_item(a, ("F.Cu", "B.Cu")), pad_item(b, ("F.Cu", "B.Cu"))
    if ia is None or ib is None:
        return math.hypot(a.x - b.x, a.y - b.y)
    if ia.shape == "rect":
        return max(0.0, _rect_to_item(*ia.geom, ib) - ia.radius)
    if ia.shape == "circle":
        return max(0.0, ib.distance_to_point(*ia.geom) - ia.radius)
    return max(0.0, ib.distance_to_segment(ia.geom[:2], ia.geom[2:]) - ia.radius)


def _pad_items(bm: BoardModel):
    from kicad_layer.design.copper import copper_layers, pad_item

    copper = copper_layers(bm.copper_layers)
    out = []
    for fp in bm.footprints:
        for p in fp.pads:
            it = pad_item(p, copper)
            if it is not None:
                out.append((fp, p, it))
    return out


# ---------------------------------------------------------------- 1. via in pad
def check_via_in_pad(bm: BoardModel) -> ReviewCheck:
    cid, name = "via_in_pad", "Vias in surface-mount pads"
    items = [(fp, p, it) for fp, p, it in _pad_items(bm) if p.kind == "smd"]
    eps = {id(exposed_pad(fp)) for fp in bm.footprints if exposed_pad(fp) is not None}
    f: list[ReviewFinding] = []
    thermal = 0
    for v in bm.vias:
        for fp, p, it in items:
            x0, y0, x1, y1 = it.bbox
            if not (x0 - 1 <= v.x <= x1 + 1 and y0 - 1 <= v.y <= y1 + 1):
                continue
            if it.distance_to_point(v.x, v.y) < v.drill / 2:
                if id(p) in eps:
                    thermal += 1
                else:
                    f.append(_finding(cid, "warning", f"via drilled into pad {p.ref}.{p.number}: solder wicks into the hole; move it beside the pad or order filled and capped vias",
                                      ref=p.ref, net=v.net, x_mm=v.x, y_mm=v.y, value=v.drill))
                break
    if thermal:
        f.append(_finding(cid, "info", f"{thermal} via(s) in exposed pads: thermal vias as intended; keep drills at 0.3 mm or less, or ask the fab for plugged or tented vias",
                          value=thermal))
    return _check(cid, name, f, f"{len(bm.vias)} vias against {len(items)} surface-mount pads by their real shape",
                  limit_source="IPC-7095 and common assembly guidance: no open vias in SMD pads except planned thermal vias",
                  summary="no vias in signal pads" if not any(x.severity == "warning" for x in f) else f"{sum(1 for x in f if x.severity == 'warning')} via(s) in signal pads")


# ---------------------------------------------------------------- 2. test points
def check_test_points(bm: BoardModel) -> ReviewCheck:
    cid, name = "test_points", "Test points on rails, programming and buses"
    tps: set[str] = set()
    access: set[str] = set()
    n_tp = 0
    pads_by_net: dict[str, int] = defaultdict(int)
    for fp in bm.footprints:
        is_tp = fp.ref.upper().startswith("TP") or "testpoint" in fp.lib_id.lower()
        n_tp += int(is_tp)
        for p in fp.pads:
            if not p.net:
                continue
            pads_by_net[p.net] += 1
            if is_tp:
                tps.add(p.net)
            elif is_connector(fp):
                access.add(p.net)
    reach = tps | access
    nets = [n for n, k in pads_by_net.items() if k >= 2 and not n.startswith("unconnected")]
    rails = sorted(n for n in nets if POWER_NET.search(n) and not GROUND.search(n) and not NOT_A_RAIL.search(n))
    grounds = sorted(n for n in nets if GROUND.search(n))
    prog = sorted(n for n in nets if PROGRAMMING.search(n))
    buses = sorted(n for n in nets if BUS.search(n) and n not in prog)
    f: list[ReviewFinding] = []
    if n_tp == 0:
        f.append(_finding(cid, "warning", "no test points on the board: add pads on the supply rails, ground and the signals you will want to probe"))
    miss_rails = [n for n in rails if n not in reach]
    if miss_rails:
        f.append(_finding(cid, "warning", f"supply rails with no test point or connector pin: {', '.join(miss_rails)}", value=len(miss_rails)))
    if grounds and not any(g in reach for g in grounds):
        f.append(_finding(cid, "warning", "no ground test point or connector ground pin to clip a probe to"))
    miss_prog = [n for n in prog if n not in reach]
    if miss_prog:
        f.append(_finding(cid, "warning", f"programming or reset nets with no header or test point: {', '.join(miss_prog)}", value=len(miss_prog)))
    miss_bus = [n for n in buses if n not in reach]
    if miss_bus:
        f.append(_finding(cid, "info", f"bus signals with no test point: {', '.join(miss_bus[:12])}" + (" ..." if len(miss_bus) > 12 else ""), value=len(miss_bus)))
    data = {"test_points": n_tp, "nets_with_tp": sorted(tps), "rails": rails, "programming": prog, "buses": buses}
    return _check(cid, name, f, f"{n_tp} test point footprint(s); connector pins count as access; rails by name, programming and bus nets by name",
                  limit_source="design-for-test practice: every rail, ground, programming port and bus reachable by a probe",
                  summary="rails, ground and programming reachable" if not f else f"{len([x for x in f if x.severity != 'info'])} gap(s) in probe access", data=data)


# ---------------------------------------------------------------- 3. thermal pads
def check_thermal_pads(bm: BoardModel, fills: FillIndex) -> ReviewCheck:
    cid, name = "thermal_pads", "Exposed pads: thermal vias and copper"
    f: list[ReviewFinding] = []
    data: dict[str, Any] = {}
    from kicad_layer.design.copper import copper_layers, pad_item

    copper = copper_layers(bm.copper_layers)
    n = 0
    for fp in bm.footprints:
        ep = exposed_pad(fp)
        if ep is None:
            continue
        n += 1
        it = pad_item(ep, copper)
        inside = [v for v in bm.vias if it is not None and it.distance_to_point(v.x, v.y) <= 0.05]
        same = [v for v in inside if v.net == ep.net]
        area = _pad_area(ep)
        data[fp.ref] = {"pad": ep.number, "area_mm2": round(area, 2), "vias": len(same), "net": ep.net}
        if not same:
            f.append(_finding(cid, "warning", f"{fp.ref}'s exposed pad {ep.number} ({ep.size[0]:g} x {ep.size[1]:g} mm, {ep.net}) has no thermal vias; "
                              "the datasheet layout usually wants a grid of 0.3 mm vias into the plane", ref=fp.ref, net=ep.net, x_mm=ep.x, y_mm=ep.y, value=0))
        elif len(same) < max(2, int(area / 2.0)):
            f.append(_finding(cid, "info", f"{fp.ref}'s exposed pad has {len(same)} thermal via(s) for {area:.1f} mm2; check the datasheet's count",
                              ref=fp.ref, net=ep.net, x_mm=ep.x, y_mm=ep.y, value=len(same)))
        # copper under the pad on the other side
        if ep.net and not fills.empty:
            other = "B.Cu" if _pad_layer(ep) == "F.Cu" else "F.Cu"
            if fills.covered(ep.x, ep.y, other, {ep.net}) is None:
                f.append(_finding(cid, "warning", f"no {ep.net} copper under {fp.ref}'s exposed pad on {other}: the vias have nowhere to spread the heat",
                                  ref=fp.ref, net=ep.net, x_mm=ep.x, y_mm=ep.y))
    if n == 0:
        return _info(cid, name, "no part with an exposed pad", "largest pad of each part with five or more pads")
    return _check(cid, name, f, f"{n} exposed pad(s): vias inside the pad on its net; fill of its net on the other side",
                  limit_source="manufacturers' layout guidelines for exposed-pad packages (e.g. TI SLMA002, SLUA271)",
                  summary="every exposed pad has thermal vias" if not any(x.severity == "warning" for x in f) else f"{sum(1 for x in f if x.severity == 'warning')} exposed pad(s) to fix",
                  data=data)


# ---------------------------------------------------------------- 4. fast nets near the edge
def check_fast_edge(bm: BoardModel, board_text: str, *, factor: float = 4.0, fast_nets: list[str] | None = None) -> ReviewCheck:
    cid, name = "fast_edge", "Fast signals away from the board edge"
    from kicad_layer.design.copper import d_seg_seg, edge_segments

    edges = edge_segments(bm.path)
    if not edges:
        return _unverified(cid, name, "the board has no Edge.Cuts outline")
    h, src = dielectric_to_plane(board_text, bm.copper_layers)
    limit = round(factor * h, 2)
    conns = [fp.courtyard for fp in bm.footprints if is_connector(fp) and fp.courtyard]

    def at_connector(x: float, y: float) -> bool:
        return any(c[0] - 2 <= x <= c[2] + 2 and c[1] - 2 <= y <= c[3] + 2 for c in conns)

    worst: dict[str, tuple[float, float, float]] = {}
    for s in bm.segments:
        if not s.layer.endswith(".Cu") or not is_fast(s.net, fast_nets):
            continue
        if at_connector(s.x1, s.y1) and at_connector(s.x2, s.y2):
            continue  # the fan-out of an edge connector cannot keep away from the edge
        d = min(d_seg_seg((s.x1, s.y1), (s.x2, s.y2), a, b) for a, b in edges) - s.width / 2
        if d < limit and (s.net not in worst or d < worst[s.net][0]):
            worst[s.net] = (d, s.x1, s.y1)
    f = [_finding(cid, "warning", f"{n} runs {d:.1f} mm from the board edge (keep {limit} mm: {factor:g} x {h:g} mm to the plane)", net=n, x_mm=x, y_mm=y,
                  value=round(d, 2), limit=limit) for n, (d, x, y) in sorted(worst.items(), key=lambda kv: kv[1][0])]
    return _check(cid, name, f, f"tracks of fast nets (by name{', plus ' + ', '.join(fast_nets) if fast_nets else ''}) against the Edge.Cuts segments; "
                  f"plane height {h:g} mm from the {src}; edge-connector fan-outs exempt",
                  limit_source=f"EMC layout practice: {factor:g} to 5 times the height above the reference plane",
                  summary="fast nets keep away from the edge" if not f else f"{len(f)} fast net(s) near the edge")


# ---------------------------------------------------------------- 5. stitching
def check_stitching(bm: BoardModel, fills: FillIndex, *, edge_spacing_mm: float = 15.0, return_via_mm: float = 2.0, fast_nets: list[str] | None = None) -> ReviewCheck:
    cid, name = "stitching", "Stitching vias"
    nets_with_zones = plane_nets(bm)
    if not nets_with_zones:
        return _info(cid, name, "no copper zones on this board", "board file")
    f: list[ReviewFinding] = []
    data: dict[str, Any] = {}
    for n in sorted(nets_with_zones):
        count = sum(1 for v in bm.vias if v.net == n)
        data[n] = {"vias": count}
        if count == 0 and bm.copper_layers >= 2:
            f.append(_finding(cid, "warning", f"zone net {n} has no vias; the pour is reachable only on its own layer", net=n, value=0))
    # plane pairs: a net filled on two or more layers wants stitching vias along the edge
    from kicad_layer.design.copper import edge_segments

    edges = edge_segments(bm.path)
    for n in sorted(nets_with_zones):
        layers = fills.layers_of(n)
        if len(layers) < 2 or not edges:
            continue
        vias = [(v.x, v.y) for v in bm.vias if v.net == n]
        pts: list[tuple[float, float]] = []
        for a, b in edges:
            pts += sample_segment(a, b, 1.0)[:-1]
        gap = 0.0
        worst = (0.0, 0.0, 0.0)
        for x, y in pts:
            # an edge point counts when both layers carry the net 1.5 mm inside the board here
            covered = sum(1 for l in layers if any(fills.covered(x + dx, y + dy, l, {n}) for dx, dy in ((1.5, 0), (-1.5, 0), (0, 1.5), (0, -1.5))))
            if covered < 2:
                gap = 0.0
                continue
            if any(math.hypot(vx - x, vy - y) <= 3.0 for vx, vy in vias):
                gap = 0.0
                continue
            gap += 1.0
            if gap > worst[0]:
                worst = (gap, x, y)
        data[n]["edge_gap_mm"] = worst[0]
        if worst[0] > edge_spacing_mm:
            f.append(_finding(cid, "warning", f"{n} is poured on {', '.join(sorted(layers))} but goes {worst[0]:.0f} mm along the edge without a stitching via",
                              net=n, x_mm=worst[1], y_mm=worst[2], value=worst[0], limit=edge_spacing_mm))
    # return vias beside layer changes of fast nets
    grounds = [(v.x, v.y) for v in bm.vias if v.net and GROUND.search(v.net)]
    lonely = []
    for v in bm.vias:
        if is_fast(v.net, fast_nets) and grounds:
            d = min(math.hypot(gx - v.x, gy - v.y) for gx, gy in grounds)
            if d > return_via_mm:
                lonely.append((v.net, d, v.x, v.y))
    if lonely:
        per: dict[str, int] = defaultdict(int)
        for net, *_ in lonely:
            per[net] += 1
        net0, d0, x0, y0 = min(lonely, key=lambda t: -t[1])
        f.append(_finding(cid, "info", f"{len(lonely)} fast-net via(s) with no ground via within {return_via_mm} mm (a return via beside each keeps the loop small): "
                          + ", ".join(f"{n} x{k}" for n, k in sorted(per.items(), key=lambda kv: -kv[1])), net=net0, x_mm=x0, y_mm=y0, value=len(lonely)))
    return _check(cid, name, f, f"vias per zone net; edge walked at 1 mm where a net is poured on two layers; ground vias within {return_via_mm} mm of fast-net vias",
                  limit_source=f"EMC practice: stitching every {edge_spacing_mm:g} mm or closer (lambda/20 at the highest frequency of interest), a return via at each signal via",
                  summary="zones stitched" if not any(x.severity == "warning" for x in f) else f"{sum(1 for x in f if x.severity == 'warning')} stitching gap(s)", data=data)


# ---------------------------------------------------------------- 6. switching regulator hot loop
def check_switcher_loop(bm: BoardModel, fills: FillIndex, *, cap_mm: float = 3.0, return_mm: float = 5.0) -> ReviewCheck:
    cid, name = "switcher_loop", "Switching regulator hot loop"
    by_net: dict[str, list[PadGeo]] = defaultdict(list)
    fp_of: dict[str, FpGeo] = {fp.ref: fp for fp in bm.footprints}
    for fp in bm.footprints:
        for p in fp.pads:
            if p.net:
                by_net[p.net].append(p)
    sw_nets = []
    for net, pads in by_net.items():
        refs = {p.ref for p in pads}
        if any(r.startswith("L") for r in refs) and any(r.startswith("U") for r in refs) and not POWER_NET.search(net) and (SWITCH_NODE.search(net) or len(refs) <= 5):
            if any(r.startswith(("D", "Q")) for r in refs) or SWITCH_NODE.search(net):
                sw_nets.append(net)
    if not sw_nets:
        return _info(cid, name, "no switch node found (a net joining an IC, an inductor and a diode or named SW/LX)", "pad nets")
    f: list[ReviewFinding] = []
    data: dict[str, Any] = {}
    for sw in sw_nets:
        pads = by_net[sw]
        ic = next(p.ref for p in pads if p.ref.startswith("U"))
        ind = next(p for p in pads if p.ref.startswith("L"))
        out_net = next((p.net for p in fp_of[ind.ref].pads if p.net and p.net != sw), None)
        diode = next((p for p in pads if p.ref.startswith("D")), None)
        ic_fp = fp_of[ic]
        ic_gnd = [p for p in ic_fp.pads if p.net and GROUND.search(p.net)]
        # the input: an IC net with a capacitor to ground, not the output, not a control pin
        cands: list[tuple[int, str]] = []
        for p in ic_fp.pads:
            n = p.net
            if not n or n in (sw, out_net) or GROUND.search(n) or NOT_INPUT.search(n):
                continue
            caps = [q for q in by_net[n] if q.ref.startswith("C") and any(o.net and GROUND.search(o.net) for o in fp_of[q.ref].pads if o is not q)]
            if caps:
                cands.append(((2 if POWER_NET.search(n) else 0) + len({q.ref for q in caps}), n))
        if not cands:
            f.append(_finding(cid, "warning", f"{ic} ({sw}): no input capacitor to ground found on its supply pins", ref=ic, net=sw))
            continue
        vin = max(cands)[1]
        vin_pads = [p for p in ic_fp.pads if p.net == vin]
        caps = {q.ref: q for q in by_net[vin] if q.ref.startswith("C")}
        ret_targets = ([p for p in fp_of[diode.ref].pads if p.net and GROUND.search(p.net)] if diode else []) or ic_gnd
        best = None
        for q in caps.values():
            q_gnd = next((o for o in fp_of[q.ref].pads if o is not q and o.net and GROUND.search(o.net)), None)
            if q_gnd is None:
                continue
            d_in_q = min(pad_gap(q, v) for v in vin_pads)
            d_ret_q, ret_q = min(((pad_gap(q_gnd, t), t) for t in ret_targets), key=lambda t: t[0]) if ret_targets else (99.0, None)
            if best is None or d_in_q + d_ret_q < best[0]:
                best = (d_in_q + d_ret_q, q, q_gnd, d_in_q, d_ret_q, ret_q)
        if best is None:
            continue
        _, cap, cap_gnd, d_in, d_ret, ret = best
        same_layer = False
        if ret is not None and not fills.empty:
            lay = _pad_layer(cap_gnd)
            r1 = fills.ring_at(cap_gnd.x, cap_gnd.y, lay, cap_gnd.net)
            r2 = fills.ring_at(ret.x, ret.y, _pad_layer(ret), ret.net)
            same_layer = r1 is not None and r1 is r2
        sw_far = next((p for p in pads if p.ref == (diode.ref if diode else ind.ref)), ind)
        sw_ic = min(pad_gap(p, sw_far) for p in pads if p.ref == ic)
        loop = d_in + sw_ic + d_ret
        data[sw] = {"ic": ic, "input": vin, "cap": cap.ref, "cap_to_pin_mm": round(d_in, 2), "return_mm": round(d_ret, 2), "catch_diode": diode.ref if diode else None,
                    "loop_estimate_mm": round(loop, 1), "return_on_one_layer": same_layer}
        if d_in > cap_mm:
            f.append(_finding(cid, "warning", f"{ic}: input capacitor {cap.ref} is {d_in:.1f} mm from the {vin} pin (keep under {cap_mm} mm)", ref=cap.ref, net=vin,
                              x_mm=cap.x, y_mm=cap.y, value=round(d_in, 2), limit=cap_mm))
        if ret is not None and d_ret > return_mm:
            what = f"{diode.ref}'s ground pad" if diode else f"{ic}'s ground"
            f.append(_finding(cid, "warning", f"{ic}: {cap.ref}'s ground is {d_ret:.1f} mm from {what} (keep under {return_mm} mm)", ref=cap.ref, net=cap_gnd.net,
                              x_mm=cap_gnd.x, y_mm=cap_gnd.y, value=round(d_ret, 2), limit=return_mm))
        if ret is not None and not same_layer and not fills.empty:
            f.append(_finding(cid, "info", f"{ic}: the loop's ground ({cap.ref} to {ret.ref}) closes through vias, not on one layer's pour", ref=cap.ref, net=cap_gnd.net))
    return _check(cid, name, f, "switch nodes by name or an IC + inductor + diode net; input capacitor = the one on the IC's supply net with the shortest loop; distances copper to copper",
                  limit_source="regulator layout guides (TI SNVA021, Analog AN-136): input capacitor at the VIN and GND pins, hot loop as small as possible",
                  summary="hot loops compact" if not any(x.severity == "warning" for x in f) else f"{sum(1 for x in f if x.severity == 'warning')} item(s) in the hot loop",
                  data=data)


# ---------------------------------------------------------------- 8. power tracks by current
def check_power_tracks(bm: BoardModel, *, currents: dict[str, float] | None = None, oz: float = 1.0, rise_c: float = 10.0, min_width: float = 0.25) -> ReviewCheck:
    cid, name = "power_tracks", "Power tracks and vias against current"
    nets: dict[str, list] = defaultdict(list)
    for s in bm.segments:
        if s.net:
            nets[s.net].append(s)
    currents = dict(currents or {})
    targets = sorted({n for n in nets if POWER_NET.search(n)} | set(currents))
    f: list[ReviewFinding] = []
    data: dict[str, Any] = {}
    for n in targets:
        segs = nets.get(n, [])
        if not segs:
            continue
        w = min(s.width for s in segs)
        ext = all(s.layer in ("F.Cu", "B.Cu") for s in segs if s.width == w)
        amp = ampacity(w, oz=oz, rise_c=rise_c, external=ext)
        layers = {s.layer for s in segs}
        vias = [v for v in bm.vias if v.net == n]
        data[n] = {"narrowest_mm": w, "ampacity_a": round(amp, 2), "vias": len(vias)}
        i = currents.get(n)
        if i is None:
            if w < min_width - 1e-6 and not GROUND.search(n):
                f.append(_finding(cid, "warning", f"power net {n} has tracks down to {w} mm (about {amp:.1f} A at {rise_c:g} C rise); give its current to check it",
                                  net=n, value=w, limit=min_width))
            continue
        data[n]["current_a"] = i
        if amp < i:
            f.append(_finding(cid, "error" if amp < 0.7 * i else "warning", f"{n} carries {i:g} A but its narrowest track ({w} mm) holds about {amp:.1f} A at {rise_c:g} C rise",
                              net=n, value=round(amp, 2), limit=i))
        if len(layers) > 1 and vias:
            per = via_ampacity(min(v.drill for v in vias), rise_c=rise_c)
            need = math.ceil(i / per)
            # the vias of one layer change: count the vias on the net as an upper bound for any one transition
            if len(vias) < need:
                f.append(_finding(cid, "warning", f"{n} changes layer through {len(vias)} via(s) of about {per:.1f} A each for {i:g} A; use at least {need} in parallel",
                                  net=n, value=len(vias), limit=need))
    src = "IPC-2221 conductor chart fit (k 0.048 outer, 0.024 inner); vias as 25 um barrels"
    return _check(cid, name, f, f"{len(targets)} net(s): power-named plus those given currents; {oz:g} oz copper, {rise_c:g} C rise",
                  limit_source=src, summary="tracks and vias carry their current" if not f else f"{len(f)} power net(s) to look at", data=data)


# ---------------------------------------------------------------- 9. antenna keep-outs
def check_antenna(bm: BoardModel, board_text: str, fills: FillIndex) -> ReviewCheck:
    cid, name = "antenna", "Antenna keep-out"
    ants = [fp for fp in bm.footprints if ANTENNA.search(fp.lib_id) or ANTENNA.search(fp.ref)]
    if not ants:
        return _info(cid, name, "no antenna or radio module on the board", "footprint library ids and references")
    root = parse(board_text)
    keepouts: dict[str, list[list[tuple[float, float]]]] = defaultdict(list)
    for node in children(root, "footprint"):
        props = {str(p[1]): str(p[2]) for p in children(node, "property") if len(p) > 2}
        ref = props.get("Reference", "")
        for z in children(node, "zone"):
            if child(z, "keepout") is None:
                continue
            poly = child(z, "polygon")
            pts = [(float(xy[1]), float(xy[2])) for xy in children(child(poly, "pts"), "xy")] if poly is not None and child(poly, "pts") is not None else []
            if len(pts) >= 3:
                keepouts[ref].append(pts)
    for z in bm.zones:  # board-level keep-outs over or beside an antenna count too
        if not (z.rule_area and z.polygon):
            continue
        zx0, zy0 = min(p[0] for p in z.polygon), min(p[1] for p in z.polygon)
        zx1, zy1 = max(p[0] for p in z.polygon), max(p[1] for p in z.polygon)
        for a in ants:
            c = a.courtyard or (a.x - 5, a.y - 5, a.x + 5, a.y + 5)
            if zx0 <= c[2] + 10 and zx1 >= c[0] - 10 and zy0 <= c[3] + 10 and zy1 >= c[1] - 10:
                keepouts[a.ref].append(z.polygon)
    f: list[ReviewFinding] = []
    for a in ants:
        kos = keepouts.get(a.ref, [])
        if not kos:
            f.append(_finding(cid, "warning", f"{a.ref} ({a.lib_id}) has no keep-out area: draw the module's antenna keep-out from its datasheet", ref=a.ref, x_mm=a.x, y_mm=a.y))
            continue
        for pts in kos:
            ring = Ring.build(pts)
            hits = 0
            for s in bm.segments:
                if any(ring.contains(x, y) for x, y in sample_segment((s.x1, s.y1), (s.x2, s.y2), 0.5)):
                    hits += 1
            hits += sum(1 for v in bm.vias if ring.contains(v.x, v.y))
            hits += sum(1 for fp in bm.footprints if fp.ref != a.ref for p in fp.pads if ring.contains(p.x, p.y))
            for (net, layer), rings in fills.rings.items():
                for r in rings:
                    if any(ring.contains(x, y) for x, y in r.points[:: max(1, len(r.points) // 400)]):
                        hits += 1
                        break
            if hits:
                f.append(_finding(cid, "error", f"{hits} copper item(s) or pour(s) inside {a.ref}'s antenna keep-out", ref=a.ref, x_mm=a.x, y_mm=a.y, value=hits))
    return _check(cid, name, f, f"{len(ants)} antenna footprint(s); keep-out zones in the footprint or on the board around it; tracks, vias, pads and pours tested",
                  limit_source="radio module datasheets: no copper, parts or metal in the antenna keep-out", summary="antenna keep-outs clear" if not f else f"{len(f)} keep-out problem(s)")


# ---------------------------------------------------------------- 11. reference plane under signals
def check_plane_reference(bm: BoardModel, fills: FillIndex, *, step: float = 0.25, gap_mm: float = 1.0, fast_nets: list[str] | None = None,
                          report: int = 15) -> ReviewCheck:
    cid, name = "plane_reference", "Signals over a continuous reference plane"
    if any(z.fill_requested and not z.filled for z in bm.zones) or fills.empty:
        return _unverified(cid, name, "zone fills are missing from the file: refill (pcb_refill_zones) and review again", "filled_polygon data")
    planes = plane_nets(bm)
    ref_nets = {n for n in planes if GROUND.search(n) or POWER_NET.search(n)} or planes
    f: list[ReviewFinding] = []
    per_net: dict[str, dict[str, Any]] = {}
    for s in bm.segments:
        if not s.net or s.net in planes or POWER_NET.search(s.net) or not s.layer.endswith(".Cu"):
            continue
        refs = adjacent(bm, s.layer)
        pts = sample_segment((s.x1, s.y1), (s.x2, s.y2), step)
        flags = [not any(fills.covered(x, y, l, ref_nets) for l in refs) for x, y in pts]
        bare = runs(flags, step)
        if not bare:
            continue
        rec = per_net.setdefault(s.net, {"bare_mm": 0.0, "longest_mm": 0.0, "x": s.x1, "y": s.y1, "layer": s.layer})
        rec["bare_mm"] += sum(bare)
        if max(bare) > rec["longest_mm"]:
            k = flags.index(True)
            rec.update(longest_mm=max(bare), x=pts[k][0], y=pts[k][1], layer=s.layer)
    fast = {n: r for n, r in per_net.items() if is_fast(n, fast_nets) and r["longest_mm"] > gap_mm}
    slow = {n: r for n, r in per_net.items() if n not in fast and r["bare_mm"] > gap_mm}
    for n, r in sorted(fast.items(), key=lambda kv: -kv[1]["bare_mm"])[:report]:
        f.append(_finding(cid, "warning", f"{n} runs {r['bare_mm']:.1f} mm on {r['layer']} with no plane under it (longest stretch {r['longest_mm']:.1f} mm): "
                          "its return current detours around the gap", net=n, x_mm=r["x"], y_mm=r["y"], value=round(r["bare_mm"], 1), limit=gap_mm))
    if slow:
        top = sorted(slow.items(), key=lambda kv: -kv[1]["bare_mm"])[:report]
        f.append(_finding(cid, "info", f"{len(slow)} slower net(s) also leave the plane, most: " + ", ".join(f"{n} {r['bare_mm']:.0f} mm" for n, r in top),
                          value=round(sum(r["bare_mm"] for r in slow.values()), 1)))
    total = sum(r["bare_mm"] for r in per_net.values())
    return _check(cid, name, f, f"signal tracks sampled every {step} mm against the fills of {', '.join(sorted(ref_nets))} on the adjacent layer",
                  limit_source="return-current practice: a signal's reference plane continuous under its whole length, no slots or splits",
                  summary="every signal has its plane under it" if not f else f"{len(fast)} fast net(s) over gaps; {total:.0f} mm of signal track without a plane in all",
                  data={"bare_mm_by_net": {n: round(r["bare_mm"], 1) for n, r in sorted(per_net.items(), key=lambda kv: -kv[1]["bare_mm"])[:40]}})


# ---------------------------------------------------------------- all of them
def layout_checks(bm: BoardModel, *, currents: dict[str, float] | None = None, fast_nets: list[str] | None = None,
                  edge_factor: float = 4.0, stitch_spacing_mm: float = 15.0) -> list[ReviewCheck]:
    text = bm.path.read_text(encoding="utf-8", errors="replace")
    fills = FillIndex.from_zones(bm.zones)
    out: list[ReviewCheck] = []
    for fn in (
        lambda: check_via_in_pad(bm),
        lambda: check_test_points(bm),
        lambda: check_thermal_pads(bm, fills),
        lambda: check_fast_edge(bm, text, factor=edge_factor, fast_nets=fast_nets),
        lambda: check_stitching(bm, fills, edge_spacing_mm=stitch_spacing_mm, fast_nets=fast_nets),
        lambda: check_switcher_loop(bm, fills),
        lambda: check_power_tracks(bm, currents=currents),
        lambda: check_antenna(bm, text, fills),
        lambda: check_plane_reference(bm, fills, fast_nets=fast_nets),
    ):
        try:
            out.append(fn())
        except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
            out.append(ReviewCheck(id="layout_rule_error", name="A layout check failed", verdict="UNVERIFIED", summary=f"{type(exc).__name__}: {exc}",
                                   evidence="internal error; the other checks ran"))
    return out
