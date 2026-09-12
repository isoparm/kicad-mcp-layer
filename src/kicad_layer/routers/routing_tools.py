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
from ..models import AutorouteReport, PairRouteReport, PairRouted, StitchReport
from ..paths import display
from .ses import Routes


def _routes_path(board: Path, routes_out: Path | None) -> Path:
    return routes_out or board.with_name(board.stem + "-routes.json")


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
                  keepouts: list[tuple[float, float, float, float]] | None) -> StitchReport:
    existing = _existing(routes_in)
    st = stitch_mod.stitch_planes(board, project, existing=existing, plane_nets=set(plane_nets) if plane_nets else None, keepouts=list(keepouts or ()))
    merged = routes_mod.merge(existing, st.routes, replace_nets=False)
    out = _routes_path(board, routes_out)
    routes_mod.save(merged, out)
    return StitchReport(board_path=display(board), routes_path=display(out), stitched=st.stitched, skipped=st.skipped, segments=len(merged.segments), vias=len(merged.vias))


def autoroute(board: Path, project: Path | None, *, routes_in: Path | None, routes_out: Path | None, plane_layers: dict[str, str] | None,
              routable_layers: list[str] | None, passes: int, timeout_s: float) -> AutorouteReport:
    existing = _existing(routes_in)
    work = board.with_suffix("")
    dsn_path, ses_path = work.with_name(work.name + ".dsn"), work.with_name(work.name + ".ses")
    opts = dsn_mod.DsnOptions(plane_layers=plane_layers or {}, routable_layers=routable_layers, protect_existing=True)
    dsn_mod.write_dsn(board, dsn_path, project, options=opts)
    run = freerouting.run(dsn_path, ses_path, max_passes=passes, improvement_threshold=0.5, timeout_s=timeout_s)
    new = ses_mod.parse_ses(ses_path)
    merged = routes_mod.merge(existing, new, replace_nets=True)
    out = _routes_path(board, routes_out)
    routes_mod.save(merged, out)
    return AutorouteReport(board_path=display(board), dsn_path=display(dsn_path), ses_path=display(ses_path), routes_path=display(out), seconds=run.seconds,
                           returncode=run.returncode, session_segments=len(new.segments), session_vias=len(new.vias), session_nets=len(new.nets),
                           segments=len(merged.segments), vias=len(merged.vias), nets=len(merged.nets), log_tail=run.log_tail[-800:])
