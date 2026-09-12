"""Questions about a built board, answered in a few lines each.

These are the questions a session used to answer with Python one-liners over the board file, at a
few hundred tokens of code and output each time::

    python -m kicad_layer.design.inspect <build-dir or .kicad_pcb> parts             every footprint: ref, footprint, position, rotation, side
    python -m kicad_layer.design.inspect <build> part REF                             one footprint and its pads: number, net, position
    python -m kicad_layer.design.inspect <build> net NAME                             the pads on a net and its copper by layer
    python -m kicad_layer.design.inspect <build> pin REF NUMBER                       the pad's net and the other pads on it
    python -m kicad_layer.design.inspect <build> classes                              net classes and their pattern assignments (.kicad_pro)
    python -m kicad_layer.design.inspect <build> drc [--all]                          violations by type, unconnected count (_drc.json from the build)
    python -m kicad_layer.design.inspect <build> unrouted                             the open connections (_drc.json)
    python -m kicad_layer.design.inspect <build> bbox [REF ...]                       the outline, and the bounding box of the named parts
    python -m kicad_layer.design.inspect <build> region X0 Y0 X1 Y1 [LAYER]           the pads, tracks, vias and holes in a rectangle
    python -m kicad_layer.design.inspect <build> free REF X Y [ROT]                    would REF fit there: courtyards, edge, copper under its pads
    python -m kicad_layer.design.inspect <build> spots REF X0 Y0 X1 Y1 [ROT]           the first places in the rectangle where REF fits
    python -m kicad_layer.design.inspect <build> clear NET LAYER X1 Y1 X2 Y2 [...] [--width W]   would this track keep every clearance
    python -m kicad_layer.design.inspect <build> clear-via NET X Y [--size S --drill D]  the same for a via

A net name may omit the sheet path: ``USB_EN`` matches ``/Power/USB_EN``. The build folder holds the
board, the project file and ``_drc.json`` (written by the build's DRC).
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from kicad_layer.config import load_settings, set_settings
from kicad_layer.review import BoardModel, load_board

USAGE = __doc__.split("each time::", 1)[1].split("A net name", 1)[0]


def _ref_key(ref: str) -> tuple[str, int, str]:
    m = re.match(r"([A-Za-z_]+)(\d*)(.*)", ref)
    return (m.group(1), int(m.group(2) or 0), m.group(3)) if m else (ref, 0, "")


def _locate(arg: str) -> tuple[Path, Path | None, Path | None]:
    """The board, its project file and the build's DRC report, from a folder or a board path."""
    p = Path(arg).resolve()
    if p.is_dir():
        boards = sorted(p.glob("*.kicad_pcb"))
        if len(boards) != 1:
            raise SystemExit(f"{p}: {len(boards)} boards; name the .kicad_pcb")
        p = boards[0]
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(p.parent)}))  # a command line, not the server: the board's folder is the workspace
    pro = p.with_suffix(".kicad_pro")
    drc = p.parent / "_drc.json"
    return p, pro if pro.is_file() else None, drc if drc.is_file() else None


def _net_matches(net: str | None, wanted: str) -> bool:
    return bool(net) and (net == wanted or net.endswith("/" + wanted))


def parts(bm: BoardModel) -> list[str]:
    return [f"{f.ref:6s} {f.lib_id.split(':')[-1][:44]:44s} ({f.x:7.2f}, {f.y:7.2f}) rot {f.rotation:5.1f}  {f.layer}"
            for f in sorted(bm.footprints, key=lambda f: _ref_key(f.ref))]


def part(bm: BoardModel, ref: str) -> list[str]:
    fp = next((f for f in bm.footprints if f.ref == ref), None)
    if fp is None:
        return [f"no footprint {ref}"]
    out = [f"{fp.ref} {fp.lib_id} at ({fp.x:.2f}, {fp.y:.2f}) rot {fp.rotation:.1f} on {fp.layer}, {len(fp.pads)} pads"]
    for p in sorted(fp.pads, key=lambda p: _ref_key(p.number)):
        out.append(f"  pad {p.number:4s} {p.net or '-':30s} ({p.x:7.2f}, {p.y:7.2f}) {p.kind}")
    return out


def net(bm: BoardModel, name: str) -> list[str]:
    pads = [(f.ref, p) for f in bm.footprints for p in f.pads if _net_matches(p.net, name)]
    if not pads:
        return [f"no pad on a net named {name}"]
    full = pads[0][1].net
    out = [f"{full}: {len(pads)} pads"]
    out += [f"  {ref}.{p.number:4s} ({p.x:7.2f}, {p.y:7.2f}) {p.kind}" for ref, p in sorted(pads, key=lambda x: (_ref_key(x[0]), _ref_key(x[1].number)))]
    by_layer: dict[str, list[float]] = defaultdict(list)
    for s in bm.segments:
        if s.net == full:
            by_layer[s.layer].append(math.hypot(s.x2 - s.x1, s.y2 - s.y1))
    vias = sum(1 for v in bm.vias if v.net == full)
    if by_layer or vias:
        out.append("  copper: " + ", ".join(f"{layer} {len(ls)} segments {sum(ls):.1f} mm" for layer, ls in sorted(by_layer.items())) + f", {vias} vias")
    else:
        out.append("  copper: none")
    return out


def pin(bm: BoardModel, ref: str, number: str) -> list[str]:
    fp = next((f for f in bm.footprints if f.ref == ref), None)
    if fp is None:
        return [f"no footprint {ref}"]
    pad = next((p for p in fp.pads if p.number == number), None)
    if pad is None:
        return [f"{ref} has no pad {number}; pads: {', '.join(p.number for p in fp.pads)}"]
    if not pad.net:
        return [f"{ref}.{number} at ({pad.x:.2f}, {pad.y:.2f}): no net"]
    return [f"{ref}.{number} at ({pad.x:.2f}, {pad.y:.2f}) on {pad.net}"] + net(bm, pad.net)[1:]


def classes(pro: Path | None) -> list[str]:
    if pro is None:
        return ["no .kicad_pro next to the board"]
    data = json.loads(pro.read_text(encoding="utf-8"))
    ns = data.get("net_settings", {})
    out = []
    for c in ns.get("classes", []):
        dp = f", pair {c.get('diff_pair_width')}/{c.get('diff_pair_gap')}" if c.get("diff_pair_width") else ""
        out.append(f"{c.get('name', '?'):10s} clearance {c.get('clearance')} track {c.get('track_width')} via {c.get('via_diameter')}/{c.get('via_drill')}{dp}")
    pats = ns.get("netclass_patterns") or []
    if pats:
        by = defaultdict(list)
        for p in pats:
            by[p.get("netclass")].append(p.get("pattern"))
        out += [f"  {k}: {', '.join(v)}" for k, v in by.items()]
    return out or ["no net classes"]


def drc(report: Path | None, everything: bool = False) -> list[str]:
    if report is None:
        return ["no _drc.json next to the board; run the build"]
    data = json.loads(report.read_text(encoding="utf-8"))
    viol = data.get("violations", [])
    kinds = Counter(v.get("type", "?") for v in viol)
    out = [f"{len(viol)} violations, {len(data.get('unconnected_items', []))} unconnected"]
    out += [f"  {n:4d}  {k}" for k, n in kinds.most_common()]
    if everything:
        for v in viol:
            pos = (v.get("items") or [{}])[0].get("pos") or {}
            out.append(f"  {v.get('type')}: {v.get('description', '')[:110]} at ({pos.get('x', '?')}, {pos.get('y', '?')})")
    return out


def unrouted(report: Path | None) -> list[str]:
    if report is None:
        return ["no _drc.json next to the board; run the build"]
    data = json.loads(report.read_text(encoding="utf-8"))
    items = data.get("unconnected_items", [])
    out = [f"{len(items)} open connections"]
    for u in items:
        ends = [i.get("description", "?") for i in u.get("items", [])][:2]
        out.append("  " + "  <->  ".join(ends))
    return out


def bbox(bm: BoardModel, refs: list[str]) -> list[str]:
    out = []
    if bm.outline:
        x0, y0, x1, y1 = bm.outline
        out.append(f"outline ({x0:.2f}, {y0:.2f}) to ({x1:.2f}, {y1:.2f}): {x1 - x0:.1f} x {y1 - y0:.1f} mm")
    for ref in refs:
        fp = next((f for f in bm.footprints if f.ref == ref), None)
        if fp is None:
            out.append(f"no footprint {ref}")
            continue
        if fp.courtyard:
            x0, y0, x1, y1 = fp.courtyard
            out.append(f"{ref} courtyard ({x0:.2f}, {y0:.2f}) to ({x1:.2f}, {y1:.2f})")
        elif fp.pads:
            xs = [p.x for p in fp.pads]
            ys = [p.y for p in fp.pads]
            out.append(f"{ref} pads ({min(xs):.2f}, {min(ys):.2f}) to ({max(xs):.2f}, {max(ys):.2f})")
        else:
            out.append(f"{ref} at ({fp.x:.2f}, {fp.y:.2f}), no pads")
    return out


def answer(argv: list[str]) -> list[str]:
    if len(argv) < 2:
        return [USAGE.strip("\n")]
    board, pro, report = _locate(argv[0])
    q, args = argv[1], argv[2:]
    if q == "classes":
        return classes(pro)
    if q == "drc":
        return drc(report, "--all" in args)
    if q == "unrouted":
        return unrouted(report)
    bm = load_board(board)
    if q == "parts":
        return parts(bm)
    if q == "part" and len(args) == 1:
        return part(bm, args[0])
    if q == "net" and len(args) == 1:
        return net(bm, args[0])
    if q == "pin" and len(args) == 2:
        return pin(bm, args[0], args[1])
    if q == "bbox":
        return bbox(bm, args)
    if q in ("region", "free", "spots", "clear", "clear-via"):
        return _copper_question(bm, board, pro, q, args)
    return [USAGE.strip("\n")]


def _copper_question(bm: BoardModel, board: Path, pro: Path | None, q: str, args: list[str]) -> list[str]:
    from . import copper

    opts: dict[str, str] = {}
    plain: list[str] = []
    k = 0
    while k < len(args):
        if args[k].startswith("--") and k + 1 < len(args):
            opts[args[k][2:]] = args[k + 1]
            k += 2
        else:
            plain.append(args[k])
            k += 1
    model = copper.Model(bm, copper.Rules.load(pro), board)
    try:
        if q == "region" and len(plain) in (4, 5):
            return copper.region(model, *map(float, plain[:4]), plain[4] if len(plain) == 5 else None)
        if q == "free" and len(plain) in (3, 4):
            return copper.free(model, plain[0], float(plain[1]), float(plain[2]), float(plain[3]) if len(plain) == 4 else None)
        if q == "spots" and len(plain) in (5, 6):
            return copper.spots(model, plain[0], *map(float, plain[1:5]), float(plain[5]) if len(plain) == 6 else None,
                                step=float(opts.get("step", 0.5)), n=int(opts.get("n", 5)))
        if q == "clear" and len(plain) >= 6 and len(plain) % 2 == 0:
            pts = [(float(plain[i]), float(plain[i + 1])) for i in range(2, len(plain), 2)]
            return copper.clear(model, plain[0], plain[1], pts, float(opts["width"]) if "width" in opts else None)
        if q == "clear-via" and len(plain) == 3:
            return copper.clear_via(model, plain[0], float(plain[1]), float(plain[2]), float(opts["size"]) if "size" in opts else None,
                                    float(opts["drill"]) if "drill" in opts else None)
    except ValueError as e:
        return [f"bad number: {e}"]
    return [USAGE.strip("\n")]


def main(argv: list[str] | None = None) -> int:
    lines = answer(sys.argv[1:] if argv is None else argv)
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
