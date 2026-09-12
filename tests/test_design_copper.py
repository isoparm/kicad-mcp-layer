"""The copper questions agree with the board: same-net copper is free, other nets' copper is a clearance, the edge counts."""

from __future__ import annotations

import pytest

from kicad_layer.design import copper
from kicad_layer.design import inspect as ins
from kicad_layer.review import load_board
from tests.conftest import FIXTURES

BOARD = FIXTURES / "complex_hierarchy" / "complex_hierarchy.kicad_pcb"
PRO = BOARD.with_suffix(".kicad_pro")


@pytest.fixture(scope="module")
def model():
    return copper.Model(load_board(BOARD), copper.Rules.load(PRO if PRO.is_file() else None), BOARD)


def _a_track(model):
    return next(s for s in model.bm.segments if s.net)


def test_distances_are_exact():
    assert copper.d_pt_seg(0, 1, -1, 0, 1, 0) == pytest.approx(1.0)
    assert copper.d_seg_seg((0, 0), (1, 0), (0, 1), (1, 1)) == pytest.approx(1.0)
    assert copper.d_seg_seg((0, 0), (1, 1), (0, 1), (1, 0)) == 0.0
    assert copper.d_seg_rect((0, 2), (2, 2), 0, 0, 2, 2, 0) == pytest.approx(1.0)
    assert copper.d_seg_rect((0, 0), (0.5, 0.5), 0, 0, 2, 2, 0) == 0.0
    assert copper.d_pt_rect((3, 0), 0, 0, 2, 2, 90) == pytest.approx(2.0)
    assert copper.rotate(-0.78, 0, 90) == pytest.approx((0, 0.78))


def test_a_track_is_clear_of_its_own_net_but_not_of_another(model):
    s = _a_track(model)
    a, b = (s.x1, s.y1), (s.x2, s.y2)
    assert not model.check_segment(s.net, s.layer, a, b, s.width)
    other = next(n for n in {t.net for t in model.bm.segments} if n and n != s.net)
    v = model.check_segment(other, s.layer, a, b, s.width)
    assert v and any(it.kind == "track" for _, _, it in v)
    lines = copper.clear(model, other, s.layer, [a, b], s.width)
    assert lines[1].startswith("NO:")
    assert copper.clear(model, s.net, s.layer, [a, b], s.width)[1].startswith("OK:")


def test_a_via_on_top_of_another_net_copper_is_refused(model):
    holes = [v for v in model.bm.vias if v.net] or [p for f in model.bm.footprints for p in f.pads if p.drill and p.net]
    target = holes[0]
    other = next(n for n in {t.net for t in model.bm.segments} if n and n != target.net)
    lines = copper.clear_via(model, other, target.x, target.y)
    assert lines[1].startswith("NO:") and any("needs" in l for l in lines[2:])
    far = (model.bm.outline[0] - 10.0, model.bm.outline[1] - 10.0)
    assert copper.clear_via(model, other, *far)[1].startswith("NO: outside")


def test_a_rounded_pad_sees_a_via_it_would_overlap():
    from pathlib import Path

    from kicad_layer.review import BoardModel, FpGeo, PadGeo, ViaGeo

    pad = PadGeo(ref="R1", number="1", x=10.0, y=10.0, size=(0.8, 0.95), drill=None, net="A", kind="smd", layers=["F.Cu"], shape="roundrect", angle=270, roundrect_ratio=0.25)
    fp = FpGeo(ref="R1", lib_id="x", x=10.0, y=10.0, rotation=270, layer="F.Cu", pads=[pad], courtyard=(9, 9, 11, 11))
    via = ViaGeo(9.25, 10.33, 0.6, 0.3, "B")  # 0.29 mm from the pad's rounded corner, less than the 0.2 class clearance plus nothing
    bm = BoardModel(path=Path("memory.kicad_pcb"), copper_layers=2, outline=(0, 0, 20, 20), footprints=[fp], segments=[], vias=[via], zones=[], texts=[], design_rules={})
    m = copper.Model(bm, copper.Rules(), None)
    lines = copper.free(m, "R1", 10.0, 10.0, 270)
    assert lines[0].endswith("NO") and any("via" in l for l in lines[1:])
    assert copper.free(m, "R1", 12.0, 10.0, 270)[0].endswith("fits")


def test_a_footprint_fits_where_it_is_and_not_on_a_neighbour(model):
    fps = [f for f in model.bm.footprints if f.courtyard and f.pads]
    f = fps[0]
    assert copper.free(model, f.ref, f.x, f.y)[0].endswith("fits")
    g = next(o for o in fps if o.ref != f.ref)
    lines = copper.free(model, f.ref, g.x, g.y)
    assert lines[0].endswith("NO") and any("courtyard overlaps" in l for l in lines)
    assert copper.free(model, "NOPE", 0, 0) == ["no footprint NOPE on the board"]


def test_region_and_spots_answer_in_lines(model):
    f = next(f for f in model.bm.footprints if f.pads)
    lines = copper.region(model, f.x - 2, f.y - 2, f.x + 2, f.y + 2)
    assert lines[0].split()[0].isdigit() and any(l.startswith("pad") for l in lines[1:])
    only_front = copper.region(model, f.x - 2, f.y - 2, f.x + 2, f.y + 2, "F.Cu")
    assert len(only_front) <= len(lines)
    s = copper.spots(model, f.ref, f.x - 1, f.y - 1, f.x + 1, f.y + 1, step=1.0, n=2)
    assert "fit" in s[0]


def test_inspect_dispatches_the_copper_questions():
    f = next(f for f in load_board(BOARD).footprints if f.pads and f.courtyard)
    assert ins.answer([str(BOARD), "free", f.ref, str(f.x), str(f.y)])[0].endswith("fits")
    assert ins.answer([str(BOARD), "region", str(f.x - 1), str(f.y - 1), str(f.x + 1), str(f.y + 1)])[0].split()[1] == "items"
    assert ins.answer([str(BOARD), "clear-via", "GND", "0", "0"])[1].startswith("NO: outside")
