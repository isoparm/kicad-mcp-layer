"""Placement: courtyards as the placement check and the board reader see them (lines, rectangles, arcs, polygons, circles), and the near/fit search."""
from __future__ import annotations

import pytest

from kicad_layer.design import board as board_mod
from kicad_layer.review import courtyard_points
from kicad_layer.sexpr import parse


def _box(pts):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return tuple(round(v, 6) for v in (min(xs), min(ys), max(xs), max(ys)))


def test_a_circle_is_its_centre_plus_and_minus_the_radius_at_any_rotation():
    g = parse('(fp_circle (center 1 0) (end 2.5 0) (layer "F.CrtYd"))')
    assert _box(courtyard_points(g)) == (-0.5, -1.5, 2.5, 1.5)
    # turned 45 degrees about the footprint origin at (10, 10): the centre moves, the box stays a 3 mm square
    x0, y0, x1, y1 = _box(courtyard_points(g, 45, 10, 10))
    assert (round(x1 - x0, 6), round(y1 - y0, 6)) == (3.0, 3.0)


def test_an_arc_reaches_the_extremes_it_sweeps_through():
    upper = parse('(fp_arc (start 1 0) (mid 0 -1) (end -1 0) (layer "F.CrtYd"))')
    assert _box(courtyard_points(upper)) == (-1.0, -1.0, 1.0, 0.0)
    lower = parse('(fp_arc (start 1 0) (mid 0 1) (end -1 0) (layer "F.CrtYd"))')
    assert _box(courtyard_points(lower)) == (-1.0, 0.0, 1.0, 1.0)
    quarter = parse('(fp_arc (start 1 0) (mid 0.7071 0.7071) (end 0 1) (layer "F.CrtYd"))')
    assert _box(courtyard_points(quarter)) == (0.0, 0.0, 1.0, 1.0)


def _fp(lib: str, name: str):
    from kicad_layer.kicad_libs import load_footprint

    try:
        return load_footprint(lib, name)
    except Exception as exc:
        pytest.skip(f"footprint {lib}:{name} not available: {exc}")


def test_the_placement_check_catches_two_round_test_points_that_overlap():
    fp = _fp("TestPoint", "TestPoint_Pad_D1.5mm")
    b = board_mod.Board(title="t", scope="t", outline=(0, 0, 20, 20), copper_layers=2)
    boxes = {"TP1": board_mod.courtyard_bbox(fp, (10.0, 10.0), 0), "TP2": board_mod.courtyard_bbox(fp, (11.5, 10.0), 0)}
    assert any("TP1 and TP2 courtyards overlap" in p for p in board_mod.check_placement(b, boxes))
    boxes["TP2"] = board_mod.courtyard_bbox(fp, (13.0, 10.0), 0)  # 3 mm apart: two 2.5 mm circles clear
    assert board_mod.check_placement(b, boxes) == []


def _search(at, near_pad, radius):
    from pathlib import Path

    from kicad_layer.design import copper
    from kicad_layer.review import BoardModel, FpGeo

    fp = _fp("TestPoint", "TestPoint_Pad_D1.5mm")
    b = board_mod.Board(title="t", scope="t", outline=(0, 0, 40, 20), copper_layers=2)
    bm = BoardModel(path=Path("memory.kicad_pcb"), copper_layers=2, outline=(0, 0, 40, 20),
                    footprints=[FpGeo(ref="TP1", lib_id="x", x=at[0], y=at[1], rotation=0.0, layer="F.Cu", courtyard=None, pads=[])],
                    segments=[], vias=[], zones=[], texts=[], design_rules={})
    model = copper.Model(bm, copper.Rules())
    p = board_mod.Place("TP1", at, near=("U1", "1"))
    return board_mod._fit_search(b, model, {}, p, fp, near_pad, radius, seed=at)


def test_near_starts_from_at_on_a_grid_through_at():
    assert _search((12.2, 10.0), (10.03, 10.0), 6.0) == (12.2, 10.0)  # at fits: it is the place, not the pad
    assert _search((30.2, 10.0), (10.0, 10.0), 6.0) == (15.7, 10.0)  # at too far: the grid point through at nearest it, within reach of the pad


def test_planes_take_a_polygon_clearance_and_priority_and_texts_a_layer_and_rotation(tmp_path):
    from kicad_layer.models import Netlist
    from kicad_layer.sexpr import child, children, parse, value

    b = board_mod.Board(title="t", scope="t", outline=(0, 0, 30, 20), copper_layers=2,
                        planes=(("B.Cu", "GND", "gnd"),
                                board_mod.Plane("F.Cu", "GND", "island", polygon=((5, 5), (15, 5), (15, 15), (5, 15)), clearance=0.5, priority=2)),
                        texts=(board_mod.Text("REV A", (10.0, 18.0), layer="B.SilkS", rot=90), board_mod.Text("TOP", (5.0, 18.0))))
    out = tmp_path / "t.kicad_pcb"
    net = Netlist(source="", netlist_path="", cache_hit=False, sheets=[], component_count=0, net_count=0, components=[], nets=[], command=[])
    board_mod.build_board(b, out, "t.kicad_sch", net, {}, None, date="", rev="", company="")
    tree = parse(out.read_text(encoding="utf-8"))
    zones = {value(z, "name"): z for z in children(tree, "zone")}
    whole = [(float(xy[1]), float(xy[2])) for xy in children(child(child(zones["gnd"], "polygon"), "pts"), "xy")]
    assert whole[0] == (0.5, 0.5) and value(zones["gnd"], "priority") is None
    island = zones["island"]
    assert [(float(xy[1]), float(xy[2])) for xy in children(child(child(island, "polygon"), "pts"), "xy")] == [(5, 5), (15, 5), (15, 15), (5, 15)]
    assert value(island, "priority") == "2" and float(child(child(island, "connect_pads"), "clearance")[1]) == 0.5
    texts = {str(t[1]): t for t in children(tree, "gr_text")}
    back = texts["REV A"]
    assert value(back, "layer") == "B.SilkS" and float(child(back, "at")[3]) == 90
    assert "mirror" in [str(a) for a in child(child(back, "effects"), "justify")[1:]]
    assert value(texts["TOP"], "layer") == "F.SilkS" and child(child(texts["TOP"], "effects"), "justify") is None


def test_the_jlcpcb_two_layer_rule_set():
    from kicad_layer.design import rules

    r = rules.jlcpcb_2l()
    assert (r.rules["min_track_width"], r.rules["min_clearance"]) == (0.15, 0.15)
    assert (r.rules["min_via_diameter"], r.rules["min_through_hole_diameter"], r.rules["min_hole_to_hole"], r.rules["min_copper_edge_clearance"]) == (0.6, 0.3, 0.5, 0.3)
    assert r.template is None and r.rule_severities["silk_overlap"] == "warning"
    for c in r.classes:
        assert c["clearance"] >= r.rules["min_clearance"] and c["track_width"] >= r.rules["min_track_width"] and c["via_diameter"] >= 0.6
