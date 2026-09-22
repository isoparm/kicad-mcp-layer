"""Plane stitching only puts a via where the via's plane has copper, on a small synthetic 4-layer board."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_layer.config import load_settings, set_settings
from kicad_layer.routers.plane_cover import PlaneCoverage, Region
from kicad_layer.routers.stitch import stitch_planes
from kicad_layer.review import load_board


@pytest.fixture(autouse=True)
def ws(tmp_path):
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": tmp_path.anchor, "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    yield
    set_settings(None)


LAYERS = '(layers (0 "F.Cu" signal) (4 "In1.Cu" power) (6 "In2.Cu" power) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'


def _pts(ring) -> str:
    return "(pts " + " ".join(f"(xy {x} {y})" for x, y in ring) + ")"


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _fp(ref: str, x: float, y: float, net: str) -> str:
    # one small SMD pad; its via goes 0.55 to 2 mm past the pad end, well inside a 6 mm window
    return (f'(footprint "t:pad" (layer "F.Cu") (at {x} {y}) (property "Reference" "{ref}")'
            f' (pad "1" smd rect (at 0 0) (size 1.0 0.6) (layers "F.Cu" "F.Mask") (net "{net}")))')


def _zone(net: str, layer: str, rings, *, priority: int = 0, fills=None, keepout: str | None = None) -> str:
    polys = " ".join(f"(polygon {_pts(r)})" for r in rings)
    filled = " ".join(f'(filled_polygon (layer "{layer}") {_pts(r)})' for r in (fills or []))
    ko = f"(keepout (tracks allowed) (vias {keepout}) (pads allowed) (copperpour allowed) (footprints allowed))" if keepout else ""
    net_node = f'(net "{net}")' if net else ""
    return f'(zone {net_node} (layer "{layer}") (priority {priority}) {ko} (fill yes) {polys} {filled})'


def _write(tmp: Path, footprints: list[str], zones: list[str]) -> Path:
    text = (f'(kicad_pcb (version 20241229) (generator "pcbnew") {LAYERS}'
            f' (gr_rect (start 0 0) (end 60 30) (layer "Edge.Cuts"))'
            + " ".join(footprints) + " " + " ".join(zones) + ")\n")
    p = tmp / "t.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return p


BOARD = _rect(0.5, 0.5, 59.5, 29.5)
CUTOUT = _rect(22, 7, 38, 23)  # a hole in the GND plane around U2


def _parts():
    return [_fp("U1", 10, 15, "GND"), _fp("U2", 30, 15, "GND"), _fp("U3", 50, 15, "GND"), _fp("U4", 10, 5, "SIG")]


def test_region_reads_a_fractured_fill_with_a_hole():
    # outer ring with the hole joined by a slit, as KiCad writes a filled polygon
    ring = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 5), (3, 5), (3, 7), (7, 7), (7, 3), (3, 3), (3, 5), (0, 5)]
    r = Region([ring])
    assert r.contains(1, 1) and r.contains(8, 5) and not r.contains(5, 5)
    assert r.covers(1.5, 8.5, 0.5) and not r.covers(2.5, 5.0, 1.0)
    assert r.covers(1.0, 5.0, 0.5), "the slit is not an edge"


def test_unfilled_zones_use_outlines_minus_cutouts_and_other_nets(tmp_path):
    board = _write(tmp_path, _parts(), [
        _zone("GND", "In1.Cu", [BOARD, CUTOUT]),
        _zone("-9V", "In1.Cu", [_rect(44, 8, 56, 22)], priority=1),  # an island of another net under U3
    ])
    res = stitch_planes(board, plane_nets={"GND"})
    via_refs = {(round(v.x), round(v.y)) for v in res.routes.vias}
    assert res.stitched == 1 and len(res.routes.vias) == 1
    assert any(abs(x - 10) <= 3 for x, _ in via_refs), "U1 sits over solid plane"
    rejected = " ".join(res.rejected)
    assert "U2-1 GND" in rejected and "U3-1 GND" in rejected
    assert any("unfilled" in w for w in res.warnings)


def test_filled_zones_are_the_truth(tmp_path):
    # the outline covers everything, the fill does not reach U3 (another net took it when KiCad filled)
    fill = [_rect(0.5, 0.5, 40, 29.5)]
    board = _write(tmp_path, _parts(), [_zone("GND", "In1.Cu", [BOARD], fills=fill)])
    res = stitch_planes(board, plane_nets={"GND"})
    assert res.stitched == 2
    assert [r.split(":")[0] for r in res.rejected] == ["U3-1 GND"]
    assert not any("unfilled" in w for w in res.warnings)


def test_a_via_keepout_refuses_the_via(tmp_path):
    board = _write(tmp_path, _parts(), [_zone("GND", "In1.Cu", [BOARD]), _zone("", "In2.Cu", [_rect(4, 9, 16, 21)], keepout="not_allowed")])
    res = stitch_planes(board, plane_nets={"GND"})
    assert res.stitched == 2 and any(s.startswith("U1-1") and "keep-out" in s for s in res.skipped)


def test_plane_layers_and_nets_without_a_plane(tmp_path):
    board = _write(tmp_path, _parts(), [_zone("GND", "F.Cu", [BOARD])])
    # a GND pour on the pad's own layer is no plane to stitch to
    res = stitch_planes(board, plane_nets={"GND", "SIG"}, fanout_nets=set())
    assert res.stitched == 0
    assert any(r.startswith("U4-1 SIG") and "no SIG plane on any layer" in r for r in res.rejected)
    # plane_layers names In2 as GND with no zone there: taken as solid, and said so
    res = stitch_planes(board, plane_nets={"GND"}, plane_layers={"In2.Cu": "GND"})
    assert res.stitched == 3 and any("solid plane" in w for w in res.warnings)
    # the legacy call (no fanout_nets) still fans out a plane-less net
    res = stitch_planes(board, plane_nets={"SIG"})
    assert res.stitched == 1 and not res.rejected


def test_coverage_on_a_real_board_reads_every_zone():
    board = Path(__file__).parent / "fixtures" / "multichannel" / "multichannel_mixer.kicad_pcb"
    cov = PlaneCoverage(load_board(board))
    assert "GND" in cov.nets and not cov.unfilled
    assert len(cov.via_keepouts) == 0, "the demo's rule areas allow vias"
