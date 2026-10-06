"""Tool-facing wrappers around the pair router, the plane stitcher and the FreeRouting pass.

All three read a board file and write copper as a routes JSON (segments and vias by net) next to
the board, never the board itself: a design-as-code project re-applies the JSON when it generates
the board, and a hand-made board takes it through ``routes_apply``. Each wrapper reports what it
did in a model the MCP layer returns as is.
"""

from __future__ import annotations

from pathlib import Path

from . import dsn as dsn_mod
from . import freerouting
from . import pairrouter
from .. import routes as routes_mod
from . import ses as ses_mod
from . import stitch as stitch_mod
from ..models import AutorouteReport, ExcludedNet, PairRouteReport, PairRouted, StitchReport
from ..paths import display
from ..project import board_rule_warnings
from .ses import Routes


def _routes_path(board: Path, routes_out: Path | None) -> Path:
    return routes_out or board.with_name(board.stem + "-routes.json")


def _rule_warnings(board: Path, project: Path | None) -> list[str]:
    """Default rules when no project is given and none sits next to the board under the board's name."""
    if project is not None:
        return [] if project.exists() else [f"project {display(project)} does not exist: the net classes are KiCad's defaults"]
    return board_rule_warnings(board)


def _existing(routes_in: Path | None) -> Routes:
    return routes_mod.load(routes_in) if routes_in and routes_in.is_file() else Routes()


def route_pairs(board: Path, project: Path | None, *, routes_in: Path | None, routes_out: Path | None, only: list[str] | None,
                exclude: list[str] | None, order: list[str] | None, keepouts: list[tuple[float, float, float, float]] | None) -> PairRouteReport:
    existing = _existing(routes_in)
    routes, results = pairrouter.route_pairs(board, project, existing=existing, only=only, exclude=exclude, order=order, keepouts=list(keepouts or ()))
    out = _routes_path(board, routes_out)
    routes_mod.save(routes, out)
    pairs = [PairRouted(name=r.name, status=r.status, p_length_mm=r.p_length, n_length_mm=r.n_length, skew_mm=r.skew, vias=r.vias, layers=r.layers, notes=r.notes) for r in results]
    routed = sum(1 for r in results if r.status == "routed")
    return PairRouteReport(board_path=display(board), routes_path=display(out), pairs=pairs, routed=routed, failed=len(results) - routed,
                           segments=len(routes.segments), vias=len(routes.vias),
                           notes=[] if routed == len(results) else ["Failed pairs are listed with the reason; move the parts or route them by hand and run again with only= for those."])


def stitch_planes(board: Path, project: Path | None, *, routes_in: Path | None, routes_out: Path | None, plane_nets: list[str] | None,
                  keepouts: list[tuple[float, float, float, float]] | None, plane_layers: dict[str, str] | None = None,
                  fanout_nets: list[str] | None = None) -> StitchReport:
    existing = _existing(routes_in)
    st = stitch_mod.stitch_planes(board, project, existing=existing, plane_nets=set(plane_nets) if plane_nets else None, keepouts=list(keepouts or ()), plane_layers=plane_layers, fanout_nets=set(fanout_nets or ()))
    merged = routes_mod.merge(existing, st.routes, replace_nets=False)
    out = _routes_path(board, routes_out)
    routes_mod.save(merged, out)
    return StitchReport(board_path=display(board), routes_path=display(out), stitched=st.stitched, skipped=st.skipped, segments=len(merged.segments), vias=len(merged.vias),
                        rejected=st.rejected, warnings=st.warnings + _rule_warnings(board, project))


def drop_nets(routes: Routes, nets: set[str]) -> tuple[Routes, int]:
    """``routes`` without the copper of ``nets``, and how many segments and vias went."""
    out = Routes(segments=[s for s in routes.segments if s.net not in nets], vias=[v for v in routes.vias if v.net not in nets])
    out.nets = {s.net for s in out.segments} | {v.net for v in out.vias}
    return out, len(routes.segments) + len(routes.vias) - len(out.segments) - len(out.vias)


def drop_existing(routes: Routes, board: Path, tol: float = 1e-3) -> tuple[Routes, int]:
    """``routes`` without the segments and vias already on the board (FreeRouting hands back protected wiring too)."""
    from ..review import load_board

    bm = load_board(board)
    segs = {(s.net, s.layer, *sorted(((round(s.x1, 3), round(s.y1, 3)), (round(s.x2, 3), round(s.y2, 3))))) for s in bm.segments}
    vias = {(v.net, round(v.x, 3), round(v.y, 3)) for v in bm.vias}
    out = Routes()
    for s in routes.segments:
        key = (s.net, s.layer, *sorted(((round(s.x1, 3), round(s.y1, 3)), (round(s.x2, 3), round(s.y2, 3)))))
        if key not in segs:
            out.segments.append(s)
    out.vias = [v for v in routes.vias if (v.net, round(v.x, 3), round(v.y, 3)) not in vias]
    out.nets = {s.net for s in out.segments} | {v.net for v in out.vias}
    return out, len(routes.segments) + len(routes.vias) - len(out.segments) - len(out.vias)


def fix_necks(routes: Routes, board: Path, project: Path | None) -> tuple[int, list[str]]:
    """Widen new segments the router drew below their net's minimum (class width, board minimum, .kicad_dru) where the
    wider track keeps every clearance; returns how many were widened and the nets of those left narrow."""
    from ..design import copper

    pro = project if project is not None and project.exists() else (board.with_suffix(".kicad_pro") if board.with_suffix(".kicad_pro").is_file() else None)
    model = copper.load(board, pro)
    for s in routes.segments:
        model.add_segment(s.net, s.layer, (s.x1, s.y1), (s.x2, s.y2), s.width)
    for v in routes.vias:
        model.add_via(v.net, v.x, v.y, v.size, v.drill)
    widened = 0
    left: dict[str, int] = {}
    for s in routes.segments:
        target = model.rules.track(s.net)
        if s.width >= target - 1e-6:
            continue
        if not model.check_segment(s.net, s.layer, (s.x1, s.y1), (s.x2, s.y2), target):
            s.width = target
            model.add_segment(s.net, s.layer, (s.x1, s.y1), (s.x2, s.y2), target)
            widened += 1
        else:
            left[s.net] = left.get(s.net, 0) + 1
    return widened, [f"{n} ({k})" for n, k in sorted(left.items())]


def autoroute(board: Path, project: Path | None, *, routes_in: Path | None, routes_out: Path | None, plane_layers: dict[str, str] | None,
              routable_layers: list[str] | None, passes: int, timeout_s: float, exclude_nets: list[str] | None = None,
              exclude_classes: list[str] | None = None, auto_exclude_ruled_nets: bool = True, force_nets: list[str] | None = None,
              pours_as_keepouts: bool = False, widen_necks: bool = True) -> AutorouteReport:
    existing = _existing(routes_in)
    work = board.with_suffix("")
    dsn_path, ses_path = work.with_name(work.name + ".dsn"), work.with_name(work.name + ".ses")
    opts = dsn_mod.DsnOptions(plane_layers=plane_layers or {}, routable_layers=routable_layers, protect_existing=True,
                              exclude_nets=tuple(exclude_nets or ()), exclude_classes=tuple(exclude_classes or ()),
                              auto_exclude_ruled_nets=auto_exclude_ruled_nets, force_nets=tuple(force_nets or ()), pours_as_keepouts=pours_as_keepouts)
    exp = dsn_mod.write_dsn_export(board, dsn_path, project, options=opts)
    run = freerouting.run(dsn_path, ses_path, max_passes=passes, improvement_threshold=0.5, timeout_s=timeout_s, ignore_classes=tuple(exp.ignore_classes))
    new = ses_mod.parse_ses(ses_path)
    # -inc is passed, but FreeRouting headless (2.4.1 included) may route the excluded nets anyway: always drop that copper
    routed_excluded = sorted(({s.net for s in new.segments} | {v.net for v in new.vias}) & set(exp.excluded))
    new, dropped = drop_nets(new, set(exp.excluded))
    warnings = list(exp.warnings)
    new, already = drop_existing(new, board) if board.is_file() else (new, 0)
    if already:
        warnings.append(f"{already} segment(s) and via(s) the session returned were already on the board; left out of the routes JSON.")
    if widen_necks and new.segments and board.is_file():
        widened, narrow = fix_necks(new, board, project)
        if widened:
            warnings.append(f"{widened} segment(s) the router drew below their net's minimum width were widened to it.")
        if narrow:
            warnings.append("Segments still below their net's minimum (no room to widen; DRC will flag them unless a neck-down rule allows them): " + ", ".join(narrow[:12]))
    if dropped:
        names = ", ".join(routed_excluded[:5]) + (", ..." if len(routed_excluded) > 5 else "")
        warnings.append(f"FreeRouting routed excluded nets anyway ({len(routed_excluded)}: {names}); the copper was dropped ({dropped} segments and vias).")
    merged = routes_mod.merge(existing, new, replace_nets=True)
    out = _routes_path(board, routes_out)
    routes_mod.save(merged, out)
    return AutorouteReport(board_path=display(board), dsn_path=display(dsn_path), ses_path=display(ses_path), routes_path=display(out), seconds=run.seconds,
                           returncode=run.returncode, session_segments=len(new.segments), session_vias=len(new.vias), session_nets=len(new.nets),
                           segments=len(merged.segments), vias=len(merged.vias), nets=len(merged.nets), log_tail=run.log_tail[-800:],
                           warnings=_rule_warnings(board, project) + warnings, excluded_nets=[ExcludedNet(net=n, reason=r) for n, r in sorted(exp.excluded.items())],
                           ignored_classes=exp.ignore_classes, class_clearances=exp.class_rules, keepouts=exp.keepouts, dropped_segments=dropped)
