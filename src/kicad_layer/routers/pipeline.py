"""The routing pipeline: a board routed in passes on a throwaway build, the copper saved as design data.

A project describes its board in a ``RoutingPlan`` and calls ``main(plan, argv)`` from a short script.
Three passes, each on a throwaway build in the plan's work folder:

1. **Differential pairs** with ``pairrouter``: every pair the netclasses declare, in the plan's order,
   except the pairs the plan lists as loose (left to the autorouter as ordinary nets).
2. **Plane stitching** with ``stitch``: a stub and via from every surface-mount pad on a plane net and
   on every net of the plan's fan-out classes (a wide rail track cannot enter a small pad; the via
   lets the autorouter reach the net on the far side with the class width).
3. **Everything else** with FreeRouting through ``dsn`` / ``freerouting`` / ``ses``, the earlier passes
   exported as protected wiring; then the clean-up router for the connections it left open.

The merged routes go to the plan's routes file, which the design's board builder re-applies on every
build (routes of nets whose pads moved are dropped there). Run the project's build afterwards to
write the real project.

Usage, through the project's script::

    route.py [--pairs-only] [--no-autoroute] [--passes N] [--rounds N] [--only PAIR ...]
    route.py --reroute PAIR ...      rip up and route again only these pairs, all else kept
    route.py --stairs                report staircase runs in the saved routes
    route.py --straighten [NET ...]  collapse tuning bumps, clearance-checked; pairs by default
    route.py --prune                 drop copper with a free end, against the built board
    route.py --import-board PCB      make a hand-edited board's copper the saved routes
    route.py --cleanup-only [--drop-nets NET ...]

``--cleanup-only`` runs only the last step on the saved routes: the connections the DRC lists as
unconnected are closed one track at a time by ``cleanup``. ``--drop-nets`` rips up the named nets
first (their routes leave the file) so the clean-up router places them again, after everything else:
for a net whose autorouted copper blocks the only exit of another net's pad.

Frozen with the rest of the routers. The intended loop routes by hand in KiCad and imports the
copper (``--import-board``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import review
from .. import routes as routes_mod
from ..review import load_board
from . import cleanup, copper, freerouting, pairrouter, plot
from . import dsn as dsn_mod
from . import ses as ses_mod
from . import stitch as stitch_mod

Rect = tuple[float, float, float, float]


@dataclass
class RoutingPlan:
    """What the pipeline needs to know about one board. Paths default to the project's layout."""

    name: str  # project name: <name>.kicad_pcb / .kicad_pro in the work folder
    project_dir: Path
    build_script: Path | None = None  # the project's build.py, run with --out <work> [--no-routes]; default design/build.py
    work: Path | None = None  # the throwaway build folder; default <project>/_routing-work
    routes: Path | None = None  # the routes JSON the board builder re-applies; default <project>/routing/routes.json
    pair_order: list[str] = field(default_factory=list)  # pairs in routing order, named as the netclasses name them (no _P/_N)
    loose_pairs: list[str] = field(default_factory=list)  # pairs the pair router leaves to the autorouter
    planes: dict[str, str] = field(default_factory=dict)  # copper layer -> plane net, for the DSN
    plane_nets: set[str] = field(default_factory=set)  # nets fanned out with a stub and via per surface-mount pad
    fanout_classes: tuple[str, ...] = ()  # netclasses whose nets are fanned out the same way
    body_keepout_refs: tuple[str, ...] = ()  # connectors whose interior (between the pin rows) the pair router leaves alone
    keepouts: list[Rect] = field(default_factory=list)  # rectangles no router copper may enter, e.g. a slot
    routable_layers: list[str] = field(default_factory=lambda: ["F.Cu", "B.Cu"])
    plot_region: Rect | None = None  # a copper plot of this region after the clean-up pass
    plot_scale: float = 7.0
    workspace: Path | None = None  # KICAD_LAYER_WORKSPACE when the environment does not set it; default the project's parent

    def __post_init__(self) -> None:
        self.project_dir = Path(self.project_dir)
        self.build_script = Path(self.build_script) if self.build_script else self.project_dir / "design" / "build.py"
        self.work = Path(self.work) if self.work else self.project_dir / "_routing-work"
        self.routes = Path(self.routes) if self.routes else self.project_dir / "routing" / "routes.json"
        self.workspace = Path(self.workspace) if self.workspace else self.project_dir.parent

    @property
    def pcb(self) -> Path:
        return self.work / f"{self.name}.kicad_pcb"

    @property
    def pro(self) -> Path:
        return self.work / f"{self.name}.kicad_pro"


def stitch_nets(plan: RoutingPlan) -> set[str]:
    """The plane nets plus every net of a fan-out class that reaches a surface-mount pad."""
    from ..routing import load_netclasses, netclass_for

    classes, assignments = load_netclasses(plan.pro)
    bm = load_board(plan.pcb)
    nets = set(plan.plane_nets)
    for f in bm.footprints:
        for pad in f.pads:
            if pad.net and pad.kind == "smd" and netclass_for(pad.net, classes, assignments)[0] in plan.fanout_classes:
                nets.add(pad.net)
    return nets


def body_interiors(plan: RoutingPlan) -> list[Rect]:
    """Rectangles between a keep-out connector's two pin rows (or columns), clear of the pad ends by half a millimetre."""
    bm = load_board(plan.pcb)
    rects: list[Rect] = []
    for f in bm.footprints:
        if f.ref not in plan.body_keepout_refs:
            continue
        pads = [p for p in f.pads if p.kind == "smd"]
        xs, ys = sorted({round(p.x, 1) for p in pads}), sorted({round(p.y, 1) for p in pads})
        if len(xs) <= 3 and len(ys) > 3:  # two columns of pins
            rects.append((xs[0] + 0.6, min(p.y for p in pads), xs[-1] - 0.6, max(p.y for p in pads)))
        elif len(ys) >= 2:  # a pin row and the mechanical pads
            rects.append((min(p.x for p in pads) - 0.5, ys[0] + 0.5, max(p.x for p in pads) + 0.5, ys[-1] - 0.9))
    return rects


def build(plan: RoutingPlan, *args: str) -> str:
    """The project's build into the work folder; the lines that matter are echoed."""
    r = subprocess.run([sys.executable, str(plan.build_script), "--out", str(plan.work), *args], capture_output=True, text=True, timeout=900)
    for line in r.stdout.splitlines():
        if any(k in line for k in ("board written", "problem", "DRC:", "   ERROR", "stopping")):
            print("  " + line[:200])
    if "board written" not in r.stdout:
        print(r.stdout[-2500:], r.stderr[-2500:])
        raise SystemExit("build failed")
    return r.stdout


def drc_summary(plan: RoutingPlan) -> dict:
    data = json.loads((plan.work / "_drc.json").read_text(encoding="utf-8"))
    kinds: dict[str, int] = {}
    for v in data.get("violations", []):
        kinds[v["type"]] = kinds.get(v["type"], 0) + 1
    return {"violations": kinds, "unconnected": len(data.get("unconnected_items", []))}


def _strip_pair(rt: routes_mod.Routes, nm: str) -> tuple[routes_mod.Routes, int, int]:
    keep_s = [s for s in rt.segments if s.net.rsplit("/", 1)[-1] not in (nm + "_P", nm + "_N")]
    keep_v = [v for v in rt.vias if v.net.rsplit("/", 1)[-1] not in (nm + "_P", nm + "_N")]
    out = routes_mod.Routes(segments=keep_s, vias=keep_v)
    out.nets = {s.net for s in keep_s} | {v.net for v in keep_v}
    return out, len(rt.segments) - len(keep_s), len(rt.vias) - len(keep_v)


def main(plan: RoutingPlan, argv: list[str]) -> int:
    os.environ.setdefault("KICAD_LAYER_WORKSPACE", str(plan.workspace))
    only = None
    if "--only" in argv:
        only = argv[argv.index("--only") + 1:]
    passes = int(argv[argv.index("--passes") + 1]) if "--passes" in argv else 40
    plan.work.mkdir(exist_ok=True)
    pcb, pro, ROUTES = plan.pcb, plan.pro, plan.routes

    if "--cleanup-only" in argv:
        # only the last step, on the routes already saved
        routes = routes_mod.load(ROUTES)
        drop: set[str] = set()
        if "--drop-nets" in argv:
            for a in argv[argv.index("--drop-nets") + 1:]:
                if a.startswith("--"):
                    break
                drop.add(a)
            before = (len(routes.segments), len(routes.vias))
            routes.segments = [s for s in routes.segments if s.net not in drop]
            routes.vias = [v for v in routes.vias if v.net not in drop]
            routes.nets = {s.net for s in routes.segments} | {v.net for v in routes.vias}
            routes_mod.save(routes, ROUTES)
            print(f"   ripped up {', '.join(sorted(drop))}: {before[0] - len(routes.segments)} segments, {before[1] - len(routes.vias)} vias dropped")
        build(plan)
        print("   DRC:", drc_summary(plan))
        return cleanup_pass(plan, routes, last=drop)

    if "--stairs" in argv:
        found = copper.find_staircases(routes_mod.load(ROUTES))
        print(f"{len(found)} staircase runs (>= 4 steps under 0.9 mm)")
        for st in found[:40]:
            print(f"  {st.net:24s} {st.layer:5s} {st.steps:3d} steps {st.length:5.1f} mm at ({st.bbox[0]:.0f}, {st.bbox[1]:.0f})")
        return 0

    if "--straighten" in argv:
        names = [a for a in argv[argv.index("--straighten") + 1:] if not a.startswith("--")]
        routes = routes_mod.load(ROUTES)
        nets = set(names) if names else {n for n in routes.nets if n.endswith(("_P", "_N"))}
        out, rep = copper.remove_bumps(routes, nets=nets)
        for net, n in sorted(rep.removed.items()):
            print(f"  {net:24s} {n} bump(s), {rep.length_removed[net]:.2f} mm removed")
        print(f"   {sum(rep.removed.values())} bumps removed, {rep.kept_for_clearance} kept for clearance; {len(routes.segments)} -> {len(out.segments)} segments")
        routes_mod.save(out, ROUTES)
        build(plan)
        print("   DRC:", drc_summary(plan))
        return 0

    if "--prune" in argv:
        routes = routes_mod.load(ROUTES)
        build(plan)
        out, removed = copper.prune_dangling(routes, review.load_board(pcb))
        for seg in removed:
            print(f"  dropped {seg.net} on {seg.layer}: ({seg.x1}, {seg.y1}) -> ({seg.x2}, {seg.y2})")
        routes_mod.save(out, ROUTES)
        build(plan)
        print("   DRC:", drc_summary(plan))
        return 0

    if "--import-board" in argv:
        src = Path(argv[argv.index("--import-board") + 1])
        imported = copper.from_board(review.load_board(src))
        print(f"   {len(imported.segments)} segments, {len(imported.vias)} vias from {src.name} -> {ROUTES.name}")
        routes_mod.save(imported, ROUTES)
        return 0

    if "--reroute" in argv:
        # rip up and route again the named pairs ONE AT A TIME: each pair gets its own freed corridor back while every
        # other track and via, including the pairs already redone, stands as an obstacle. Routing several together let
        # the first ones take the corridor the last one needed. A pair the router cannot place again keeps its copper.
        names = [a for a in argv[argv.index("--reroute") + 1:] if not a.startswith("--")]
        names = [n for n in plan.pair_order if n in names] + [n for n in names if n not in plan.pair_order]
        routes = routes_mod.load(ROUTES)
        build(plan)  # the board with the current netlist; copper comes from `routes`, not from this file
        keepouts = plan.keepouts + body_interiors(plan)
        failed = []
        for nm in names:
            t0 = time.time()
            cur, ns, nv = _strip_pair(routes, nm)
            done, results = pairrouter.route_pairs(pcb, pro, existing=cur, only=[nm], keepouts=keepouts, exclude=plan.loose_pairs, order=plan.pair_order)
            r = next((x for x in results if x.name == nm), None)
            if r is not None and r.status == "routed":
                routes = done
                print(f"     {nm:11s} routed  P={r.p_length:7.2f} N={r.n_length:7.2f} skew={r.skew:5.2f} vias={r.vias} ({ns} seg/{nv} via ripped, {time.time() - t0:.0f}s) | " + " | ".join(r.notes)[:160])
            else:
                failed.append(nm)
                print(f"  !! {nm:11s} {r.status if r else 'absent':7s}: old copper kept | " + " | ".join(r.notes if r else [])[:160])
        routes_mod.save(routes, ROUTES)
        build(plan)
        print("   DRC:", drc_summary(plan))
        return 1 if failed else 0

    print("1. bare board")
    build(plan, "--no-routes")
    t0 = time.time()
    pair_keepouts = plan.keepouts + body_interiors(plan)
    routes, results = pairrouter.route_pairs(pcb, pro, only=only, keepouts=pair_keepouts, exclude=plan.loose_pairs, order=plan.pair_order)
    ok = sum(r.status == "routed" for r in results)
    print(f"2. pairs: {ok}/{len(results)} routed in {time.time() - t0:.0f}s")
    for r in results:
        flag = "  " if r.status == "routed" else "!!"
        print(f"  {flag} {r.name:11s} {r.status:7s} P={r.p_length:7.2f} N={r.n_length:7.2f} skew={r.skew:5.2f} vias={r.vias} | " + " | ".join(r.notes)[:150])
    if "--pairs-only" not in argv:
        st = stitch_mod.stitch_planes(pcb, pro, existing=routes, plane_nets=stitch_nets(plan), keepouts=plan.keepouts)
        print(f"3. stitching: {st.stitched} pads, {len(st.skipped)} left to the autorouter")
        for line in st.skipped:
            print("     " + line[:150])
        routes = routes_mod.merge(routes, st.routes, replace_nets=False)
    routes_mod.save(routes, ROUTES)
    print("   pairs" + ("" if "--pairs-only" in argv else " + stitches") + f" saved: {len(routes.segments)} segments, {len(routes.vias)} vias")
    build(plan)
    print("   DRC:", drc_summary(plan))
    if "--pairs-only" in argv or "--no-autoroute" in argv:
        return 0

    rounds = int(argv[argv.index("--rounds") + 1]) if "--rounds" in argv else 2
    # a pair the pair router could not place must not be routed single-ended by the autorouter: leave its nets open
    failed_nets = tuple(n for r in results if r.status != "routed" for n in (r.name + "_P", r.name + "_N", r.name + "_DP", r.name + "_DN"))
    failed_nets = tuple(n for n in {pad.net for f in load_board(pcb).footprints for pad in f.pads if pad.net} if any(n.endswith("/" + x) or n == x for x in failed_nets))
    if failed_nets:
        print(f"   autorouter told to leave alone: {', '.join(failed_nets)}")
    for rnd in range(1, rounds + 1):
        print(f"4. autorouting the rest with FreeRouting (round {rnd})")
        dsn_path, ses_path = plan.work / f"{plan.name}.dsn", plan.work / f"{plan.name}.ses"
        opts = dsn_mod.DsnOptions(plane_layers=plan.planes, routable_layers=plan.routable_layers, protect_existing=True, ignore_nets=failed_nets)
        dsn_mod.write_dsn(pcb, dsn_path, pro, options=opts)
        run = freerouting.run(dsn_path, ses_path, max_passes=passes, improvement_threshold=0.5, timeout_s=3000)
        print(f"   FreeRouting exit {run.returncode} in {run.seconds:.0f}s")
        new = ses_mod.parse_ses(ses_path)
        print(f"   session: {len(new.segments)} segments, {len(new.vias)} vias on {len(new.nets)} nets")
        routes = routes_mod.merge(routes, new, replace_nets=True)
        routes_mod.save(routes, ROUTES)
        for p in (dsn_path, ses_path):
            shutil.copy2(p, ROUTES.parent / p.name)
        print(f"   {ROUTES.name}: {len(routes.segments)} segments, {len(routes.vias)} vias, {len(routes.nets)} nets")
        build(plan)
        summary = drc_summary(plan)
        print("   DRC:", summary)
        if summary["unconnected"] == 0:
            break
    # fan-out vias the autorouter did not use are dangling copper: drop them and their stubs, rebuild
    drc = json.loads((plan.work / "_drc.json").read_text(encoding="utf-8"))
    dangling = [(v["items"][0]["pos"]["x"], v["items"][0]["pos"]["y"]) for v in drc.get("violations", []) if v["type"] == "via_dangling" and v.get("items")]
    if dangling:
        # drop the via; drop its fan-out stub only when nothing else ends where the via was on the stub's layer
        keep_v = [v for v in routes.vias if not any(abs(v.x - x) < 0.01 and abs(v.y - y) < 0.01 for x, y in dangling)]
        gone = [v for v in routes.vias if v not in keep_v]
        routes.vias = keep_v

        def at_via(seg, v):
            return (abs(seg.x2 - v.x) < 0.01 and abs(seg.y2 - v.y) < 0.01) or (abs(seg.x1 - v.x) < 0.01 and abs(seg.y1 - v.y) < 0.01)

        removed_stubs = 0
        for v in gone:
            touching = [seg for seg in routes.segments if at_via(seg, v)]
            if len(touching) == 1:  # a lone stub to nowhere
                routes.segments.remove(touching[0])
                removed_stubs += 1
        routes_mod.save(routes, ROUTES)
        print(f"   pruned {len(gone)} dangling via(s) and {removed_stubs} lone stub(s)")
        build(plan)
        print("   DRC:", drc_summary(plan))
    return cleanup_pass(plan, routes)


def cleanup_pass(plan: RoutingPlan, routes: routes_mod.Routes, last: set[str] = frozenset()) -> int:
    """5. connections the autorouter left open: one track each with the grid search, twice if needed. The nets
    in ``last`` (ripped up on purpose) are routed after the others."""
    for attempt in range(2):
        drc = json.loads((plan.work / "_drc.json").read_text(encoding="utf-8"))
        opens = cleanup.open_connections_from_drc(drc)
        if not opens:
            break
        print(f"5. clean-up router: {len(opens)} open connection(s)" + (" (second pass)" if attempt else ""))
        cr = cleanup.route_open_connections(plan.pcb, plan.pro, opens, keepouts=plan.keepouts, last=last)
        for line in cr.routed:
            print("     routed", line)
        for line in cr.failed:
            print("     failed", line)
        if not cr.routed:
            break
        routes = routes_mod.merge(routes, cr.routes, replace_nets=False)
        routes_mod.save(routes, plan.routes)
        build(plan)
        print("   DRC:", drc_summary(plan))
    if plan.plot_region:
        plot.plot(plan.pcb, plan.work / "routed.png", region=plan.plot_region, scale=plan.plot_scale)
    return 0
