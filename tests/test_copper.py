"""Copper operations on saved routes."""
from __future__ import annotations

from pathlib import Path

from kicad_layer.routers import copper
from kicad_layer.review import BoardModel, FpGeo, PadGeo, SegGeo, ViaGeo
from kicad_layer.routes import RouteSegment, Routes, RouteVia


def _routes(segs: list[tuple], vias: list[tuple] = ()) -> Routes:
    r = Routes()
    r.segments = [RouteSegment(*s) for s in segs]
    r.vias = [RouteVia(*v) for v in vias]
    r.nets = {s.net for s in r.segments} | {v.net for v in r.vias}
    return r


def _zigzag(net: str, layer: str, n: int, step: float) -> list[tuple]:
    """n steps alternating 45-degree and horizontal pieces of `step` mm along a diagonal."""
    segs, x, y = [], 0.0, 0.0
    for i in range(n):
        if i % 2 == 0:
            nx, ny = x + step * 0.7071, y + step * 0.7071
        else:
            nx, ny = x + step, y
        segs.append((net, layer, 0.15, x, y, nx, ny))
        x, y = nx, ny
    return segs


def test_staircases_are_found_and_straight_runs_are_not():
    r = _routes(_zigzag("A", "F.Cu", 8, 0.5) + [("B", "F.Cu", 0.15, 0, 5, 10, 5), ("B", "F.Cu", 0.15, 10, 5, 20, 15)])
    found = copper.find_staircases(r, min_steps=4, max_step=0.9)
    assert [s.net for s in found] == ["A"]
    assert found[0].steps == 8 and found[0].layer == "F.Cu"


def _bump(net: str, y_base: float, amp: float, x0: float = 2.0) -> list[tuple]:
    """A straight run with one trapezoid bump of amplitude `amp` in the middle."""
    return [
        (net, "F.Cu", 0.15, 0.0, y_base, x0, y_base),
        (net, "F.Cu", 0.15, x0, y_base, x0 + amp, y_base - amp),
        (net, "F.Cu", 0.15, x0 + amp, y_base - amp, x0 + amp + 0.4, y_base - amp),
        (net, "F.Cu", 0.15, x0 + amp + 0.4, y_base - amp, x0 + 2 * amp + 0.4, y_base),
        (net, "F.Cu", 0.15, x0 + 2 * amp + 0.4, y_base, 10.0, y_base),
    ]


def test_bump_collapses_onto_its_base_line_and_pieces_merge():
    r = _routes(_bump("N", 5.0, 0.4))
    out, rep = copper.remove_bumps(r)
    assert rep.removed == {"N": 1} and rep.kept_for_clearance == 0
    assert len(out.segments) == 1
    s = out.segments[0]
    assert (s.x1, s.y1, s.x2, s.y2) == (0.0, 5.0, 10.0, 5.0)
    assert abs(rep.length_removed["N"] - (2 * 0.4 * (2 ** 0.5) - 2 * 0.4)) < 1e-3


def test_bump_is_kept_when_the_straight_line_would_crowd_another_net():
    # a partner track runs 0.2 mm below the base line: collapsing the bump would leave 0.05 mm of copper gap
    r = _routes(_bump("N", 5.0, 0.4) + [("P", "F.Cu", 0.15, 0.0, 5.2, 10.0, 5.2)])
    out, rep = copper.remove_bumps(r, clearance=0.15)
    assert rep.removed == {} and rep.kept_for_clearance == 1
    assert len([s for s in out.segments if s.net == "N"]) == 5


def test_tall_or_lopsided_shapes_are_not_bumps():
    r = _routes(_bump("N", 5.0, 1.5))  # 1.5 mm amplitude is a real meander, not a bump
    out, rep = copper.remove_bumps(r)
    assert rep.removed == {} and len(out.segments) == 5


def _board(pads: list[PadGeo], segs: list[SegGeo] = (), vias: list[ViaGeo] = ()) -> BoardModel:
    fp = FpGeo(ref="U1", lib_id="x", x=0.0, y=0.0, rotation=0.0, layer="F.Cu", pads=list(pads))
    return BoardModel(path=Path("x.kicad_pcb"), copper_layers=2, outline=(0, 0, 50, 50), footprints=[fp], segments=list(segs), vias=list(vias), zones=[], texts=[], design_rules={})


def _pad(net: str, x: float, y: float) -> PadGeo:
    return PadGeo(ref="U1", number="1", x=x, y=y, size=(1.0, 1.0), drill=None, net=net, kind="smd", layers=["F.Cu"])


def test_prune_removes_a_spur_but_keeps_copper_that_lands_on_pads_and_vias():
    b = _board([_pad("N", 0.0, 0.0), _pad("N", 10.0, 0.0)])
    r = _routes([
        ("N", "F.Cu", 0.15, 0.0, 0.0, 10.0, 0.0),  # pad to pad: keep
        ("N", "F.Cu", 0.15, 5.0, 0.0, 5.0, 3.0),   # spur off the middle: drop
        ("N", "F.Cu", 0.15, 5.0, 3.0, 7.0, 3.0),   # continues the spur: drop on the second pass
        ("N", "F.Cu", 0.15, 10.0, 0.0, 12.0, 0.0),  # pad to via: keep
        ("N", "B.Cu", 0.15, 12.0, 0.0, 15.0, 0.0),  # via onward on the other layer, ends free: drop
    ], vias=[("N", 12.0, 0.0, 0.6, 0.3)])
    out, removed = copper.prune_dangling(r, b)
    assert len(removed) == 3 and len(out.segments) == 2
    assert all(s.layer == "F.Cu" for s in out.segments)


def test_from_board_round_trip():
    b = _board([_pad("N", 0.0, 0.0)], segs=[SegGeo(0.0, 0.0, 3.0, 0.0, 0.2, "F.Cu", "N"), SegGeo(0, 1, 2, 1, 0.2, "B.Cu", None)],
               vias=[ViaGeo(3.0, 0.0, 0.6, 0.3, "N")])
    r = copper.from_board(b)
    assert [(s.net, s.layer, s.x2) for s in r.segments] == [("N", "F.Cu", 3.0)]  # the unassigned segment is skipped
    assert [(v.net, v.x) for v in r.vias] == [("N", 3.0)] and r.nets == {"N"}
