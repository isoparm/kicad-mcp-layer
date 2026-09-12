"""The stub router connects what the DRC report leaves open, without touching existing copper, on a model built in memory."""

from __future__ import annotations

from pathlib import Path

from kicad_layer.design import copper, stubs
from kicad_layer.review import BoardModel, FpGeo, PadGeo, SegGeo, ViaGeo, ZoneGeo


def _pad(ref: str, number: str, net: str, x: float, y: float, layer: str = "F.Cu") -> PadGeo:
    return PadGeo(ref=ref, number=number, x=x, y=y, size=(0.8, 0.9), drill=None, net=net, kind="smd", layers=[layer], shape="roundrect", roundrect_ratio=0.25)


def _board(pads: list[PadGeo], segs: list[SegGeo] = (), vias: list[ViaGeo] = (), zones: list[ZoneGeo] = ()) -> BoardModel:
    fps: dict[str, FpGeo] = {}
    for p in pads:
        fps.setdefault(p.ref, FpGeo(ref=p.ref, lib_id="x", x=p.x, y=p.y, rotation=0.0, layer="F.Cu", courtyard=(p.x - 1, p.y - 1, p.x + 1, p.y + 1))).pads.append(p)
    return BoardModel(path=Path("memory.kicad_pcb"), copper_layers=4, outline=(0, 0, 40, 40), footprints=list(fps.values()),
                      segments=list(segs), vias=list(vias), zones=list(zones), texts=[], design_rules={})


def _open(net: str, a, b, la="F.Cu", lb="F.Cu", what_a="Pad 1 [R1]", what_b="Pad 2 [R2]") -> stubs.Open:
    return stubs.Open(net, a, b, la, lb, what_a, what_b)


def _all_clear(res: stubs.Result, bm: BoardModel) -> bool:
    model = copper.Model(bm, copper.Rules(), None)
    for s in res.routes.segments:
        if model.check_segment(s.net, s.layer, (s.x1, s.y1), (s.x2, s.y2), s.width):
            return False
    for v in res.routes.vias:
        if model.check_circle(v.net, model.copper, v.x, v.y, v.size / 2) or model.check_circle(v.net, model.copper, v.x, v.y, v.drill / 2, is_hole=True):
            return False
    return True


def test_a_free_line_is_one_straight_segment():
    bm = _board([_pad("R1", "1", "N", 10, 10), _pad("R2", "2", "N", 20, 10)])
    res = stubs.route_stubs(bm, copper.Rules(), [_open("N", (10, 10), (20, 10))])
    assert res.routed == 1 and len(res.routes.segments) == 1 and not res.routes.vias
    assert "straight" in res.lines[0] and _all_clear(res, bm)


def test_a_track_in_the_way_forces_a_detour_that_keeps_clearance():
    wall = SegGeo(15, 5, 15, 15, 0.2, "F.Cu", "X")  # a foreign track across the straight line
    bm = _board([_pad("R1", "1", "N", 10, 10), _pad("R2", "2", "N", 20, 10)], segs=[wall])
    res = stubs.route_stubs(bm, copper.Rules(), [_open("N", (10, 10), (20, 10))])
    assert res.routed == 1 and res.failed == 0
    assert len(res.routes.segments) >= 2 and _all_clear(res, bm)


def test_ends_on_different_layers_get_a_via():
    bm = _board([_pad("R1", "1", "N", 10, 10), _pad("R2", "2", "N", 20, 12, "B.Cu")])
    res = stubs.route_stubs(bm, copper.Rules(), [_open("N", (10, 10), (20, 12), "F.Cu", "B.Cu")])
    assert res.routed == 1 and len(res.routes.vias) == 1 and _all_clear(res, bm)
    assert {s.layer for s in res.routes.segments} <= {"F.Cu", "B.Cu"}


def test_an_existing_via_of_the_net_is_a_joint_the_router_may_use():
    # the start pad is walled in on F.Cu, but the net already has a via right below it: the router goes down there and runs on B.Cu
    walls = [SegGeo(8.2, 8.2, 12, 8.2, 0.2, "F.Cu", "X"), SegGeo(12, 8.2, 12, 12, 0.2, "F.Cu", "X"), SegGeo(12, 12, 8.2, 12, 0.2, "F.Cu", "X"), SegGeo(8.2, 12, 8.2, 8.2, 0.2, "F.Cu", "X")]
    own = [SegGeo(10, 10, 10, 10.8, 0.15, "F.Cu", "N")]
    bm = _board([_pad("R1", "1", "N", 10, 10), _pad("R2", "2", "N", 20, 10)], segs=walls + own, vias=[ViaGeo(10, 10.8, 0.6, 0.3, "N")])
    res = stubs.route_stubs(bm, copper.Rules(), [_open("N", (10, 10), (20, 10))])
    assert res.routed == 1 and _all_clear(res, bm)
    assert any(s.layer == "B.Cu" for s in res.routes.segments) and len(res.routes.vias) == 1


def test_a_pad_on_a_plane_net_gets_a_via_beside_it():
    zone = ZoneGeo(net="GND", layers=["In1.Cu"], name="GND plane", fill_requested=True, filled=True)
    bm = _board([_pad("R1", "2", "GND", 10, 10)], zones=[zone])
    res = stubs.route_stubs(bm, copper.Rules(), [_open("GND", (10, 10), (30, 30), "F.Cu", "In1.Cu", "Pad 2 [R1]", "Zone [GND]")])
    assert res.routed == 1 and len(res.routes.vias) == 1 and "plane" in res.lines[0] and _all_clear(res, bm)


def test_pairs_are_skipped_and_the_open_list_reads_the_report():
    bm = _board([_pad("R1", "1", "/X_P", 10, 10), _pad("R2", "2", "/X_P", 20, 10)])
    res = stubs.route_stubs(bm, copper.Rules(), [_open("/X_P", (10, 10), (20, 10))])
    assert res.skipped == 1 and not res.routes.segments
    drc = {"unconnected_items": [{"items": [{"description": "Pad 1 [/Audio/I2S_DIN] of R25 on F.Cu", "pos": {"x": 1.0, "y": 2.0}},
                                            {"description": "Track [/Audio/I2S_DIN] on B.Cu", "pos": {"x": 3.0, "y": 4.0}}]}]}
    (o,) = stubs.open_connections(drc)
    assert (o.net, o.a, o.b, o.layer_a, o.layer_b) == ("/Audio/I2S_DIN", (1.0, 2.0), (3.0, 4.0), "F.Cu", "B.Cu")


def test_a_boxed_in_pad_fails_with_the_blocker_named():
    ring = [SegGeo(8, 8, 12, 8, 0.2, "F.Cu", "X"), SegGeo(12, 8, 12, 12, 0.2, "F.Cu", "X"), SegGeo(12, 12, 8, 12, 0.2, "F.Cu", "X"), SegGeo(8, 12, 8, 8, 0.2, "F.Cu", "X")]
    ring_b = [SegGeo(s.x1, s.y1, s.x2, s.y2, 0.2, "B.Cu", "X") for s in ring]
    bm = _board([_pad("R1", "1", "N", 10, 10), _pad("R2", "2", "N", 20, 10)], segs=ring + ring_b)
    res = stubs.route_stubs(bm, copper.Rules(), [_open("N", (10, 10), (20, 10))])
    assert res.failed == 1 and "blocked by track X" in res.lines[0]
