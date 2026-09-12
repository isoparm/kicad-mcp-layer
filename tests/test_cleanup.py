"""The clean-up router: single-track search, escapes checked against the real geometry, islands and pockets."""

from __future__ import annotations

import json
import math
from pathlib import Path

from kicad_layer.routers import cleanup, pairrouter as pr
from kicad_layer.review import BoardModel, FpGeo, PadGeo, SegGeo, ViaGeo
from kicad_layer.routes import Routes
from kicad_layer.routers.stitch import _rect_dist, _seg_rect_dist


def _pad(ref, number, x, y, w, h, net, *, kind="smd", drill=None, angle=0.0, clearance=0.0) -> PadGeo:
    layers = ["*.Cu", "*.Mask"] if kind != "smd" else ["F.Cu", "F.Mask", "F.Paste"]
    return PadGeo(ref=ref, number=number, x=x, y=y, size=(w, h), drill=drill, net=net, kind=kind, layers=layers, shape="rect", angle=angle, clearance=clearance)


def _board(footprints, segments=(), vias=(), outline=(0.0, 0.0, 20.0, 10.0)) -> BoardModel:
    return BoardModel(path=Path("t.kicad_pcb"), copper_layers=2, outline=outline, footprints=list(footprints), segments=list(segments), vias=list(vias),
                      zones=[], texts=[], design_rules={})


def _project(tmp_path: Path) -> Path:
    pro = {"net_settings": {
        "classes": [
            {"name": "Default", "clearance": 0.125, "track_width": 0.15, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "Power", "clearance": 0.125, "track_width": 0.5, "via_diameter": 0.7, "via_drill": 0.3},
            {"name": "POE", "clearance": 0.4, "track_width": 0.5, "via_diameter": 0.7, "via_drill": 0.3},
        ],
        "netclass_assignments": [{"netclass": "Power", "pattern": "V5*"}, {"netclass": "POE", "pattern": "POE*"}],
    }}
    p = tmp_path / "t.kicad_pro"
    p.write_text(json.dumps(pro), encoding="utf-8")
    return p


def _connected(routes: Routes, a: tuple[float, float], b: tuple[float, float]) -> bool:
    """a and b joined by the routes' segments (ends meeting, vias joining layers at one point)."""
    key = lambda x, y: (round(x, 3), round(y, 3))
    adj: dict = {}
    for s in routes.segments:
        k1, k2 = key(s.x1, s.y1), key(s.x2, s.y2)
        adj.setdefault(k1, set()).add(k2)
        adj.setdefault(k2, set()).add(k1)
    start = min(adj, key=lambda k: math.dist(k, a))
    goal = min(adj, key=lambda k: math.dist(k, b))
    seen, todo = {start}, [start]
    while todo:
        k = todo.pop()
        for n in adj[k]:
            if n not in seen:
                seen.add(n)
                todo.append(n)
    return goal in seen and math.dist(start, a) < 0.01 and math.dist(goal, b) < 0.01


# --------------------------------------------------------------------------------------
# search primitives
# --------------------------------------------------------------------------------------


def test_astar_single_goes_round_a_wall_with_vias():
    g = pr.Grid(0.0, 0.0, 10.0, 10.0, 0.2, ["F.Cu", "B.Cu"])
    for j in range(g.h):  # a wall on F.Cu only, the whole height of the board
        g.mark_disk("F.Cu", 5.0, j * 0.2, 0.15)
    s, t = g.idx(1.0, 5.0), g.idx(9.0, 5.0)
    path = pr.astar_single(g, (*s, "F.Cu"), (*t, "F.Cu"), via_clear_cells=1)
    assert path is not None and path[0][:2] == s and path[-1][:2] == t
    assert path[0][2] == "F.Cu" and path[-1][2] == "F.Cu"
    changes = sum(1 for a, b in zip(path, path[1:]) if a[2] != b[2])
    assert changes == 2  # down to B.Cu before the wall, back up after it
    for a, b in zip(path, path[1:]):
        assert max(abs(a[0] - b[0]), abs(a[1] - b[1])) <= 1  # 8-connected, a via keeps the cell
        assert not g.blocked(b[2], b[0], b[1])


def test_astar_single_prefers_straight_runs_and_reports_no_path():
    g = pr.Grid(0.0, 0.0, 10.0, 10.0, 0.2, ["F.Cu"])
    s, t = g.idx(1.0, 1.0), g.idx(9.0, 1.0)
    path = pr.simplify(pr.astar_single(g, (*s, "F.Cu"), (*t, "F.Cu")))
    assert len(path) == 2  # one straight segment, no zig-zag
    for j in range(g.h):
        g.mark_disk("F.Cu", 5.0, j * 0.2, 0.15)
    assert pr.astar_single(g, (*s, "F.Cu"), (*t, "F.Cu")) is None


def test_open_tells_a_pocket_from_open_space_and_counts_a_via_as_an_exit():
    g = pr.Grid(0.0, 0.0, 10.0, 10.0, 0.2, ["F.Cu", "B.Cu"])
    # a 3 x 3 cell pocket on F.Cu walled in by a ring of blocked cells
    for i in range(8, 15):
        for j in range(8, 15):
            if not (10 <= i <= 12 and 10 <= j <= 12):
                g.cells["F.Cu"][j * g.w + i] = 1
    assert not cleanup._open(g, "F.Cu", 11, 11)
    assert cleanup._open(g, "F.Cu", 11, 11, vias=True)  # B.Cu is free under the pocket: a via leads out
    g.cells["B.Cu"][11 * g.w + 11] = 1
    for i in range(8, 15):
        for j in range(8, 15):
            g.cells["B.Cu"][j * g.w + i] = 1
    assert not cleanup._open(g, "F.Cu", 11, 11, vias=True)
    assert cleanup._open(g, "F.Cu", 2, 2)


# --------------------------------------------------------------------------------------
# islands, clearance checks, the DRC report
# --------------------------------------------------------------------------------------


def test_island_points_follows_the_stub_and_flags_its_dangling_end():
    pad = _pad("U1", "1", 5.0, 5.0, 0.7, 0.2, "SIG")
    bm = _board([FpGeo("U1", "lib:x", 5.0, 5.0, 0.0, "F.Cu", [pad])],
                segments=[SegGeo(5.0, 5.0, 4.0, 5.0, 0.15, "F.Cu", "SIG"), SegGeo(4.0, 5.0, 3.5, 4.5, 0.15, "F.Cu", "SIG"),
                          SegGeo(15.0, 5.0, 16.0, 5.0, 0.15, "F.Cu", "SIG")])  # the last one is another island
    isl = cleanup.island_points(bm, Routes(), "SIG", (5.0, 5.0), ["F.Cu", "B.Cu"])
    pts = {p: (lays, dangling) for p, lays, dangling in isl}
    assert set(pts) == {(5.0, 5.0), (4.0, 5.0), (3.5, 4.5)}
    assert pts[(3.5, 4.5)][1] is True and pts[(4.0, 5.0)][1] is False and pts[(5.0, 5.0)][1] is False
    assert pts[(5.0, 5.0)][0] == ["F.Cu"]
    assert cleanup.island_points(bm, Routes(), "SIG", (12.0, 5.0), ["F.Cu", "B.Cu"]) == []  # nothing within 0.3 mm


def test_stub_clear_applies_the_larger_class_clearance_and_the_hole_gap(tmp_path):
    from kicad_layer.routing import load_netclasses
    classes, assignments = load_netclasses(_project(tmp_path))

    def board(net2: str) -> BoardModel:
        return _board([FpGeo("J1", "lib:x", 5.0, 5.0, 0.0, "F.Cu", [
            _pad("J1", "1", 5.0, 5.0, 0.6, 1.0, "SIG"),
            _pad("J1", "2", 5.75, 3.5, 0.6, 1.0, net2),  # 0.45 mm from a stub running up at x = 5.0
            _pad("J1", "", 5.0, 7.0, 0.65, 0.65, None, kind="np_thru_hole", drill=0.65),
        ])])

    poe = cleanup.Clearance(board("POE_A"), classes, assignments, ["F.Cu", "B.Cu"])  # pad 2's class wants 0.4 mm
    plain = cleanup.Clearance(board("OTHER"), classes, assignments, ["F.Cu", "B.Cu"])
    assert poe.stub_clear("SIG", "F.Cu", (5.0, 5.0), (5.0, 3.0), 0.15, 0.125) == "pad J1-2"  # 0.45 < 0.075 + 0.4
    assert plain.stub_clear("SIG", "F.Cu", (5.0, 5.0), (5.0, 3.0), 0.15, 0.125) is None  # 0.45 >= 0.075 + 0.125
    assert poe.stub_clear("SIG", "F.Cu", (4.9, 5.0), (4.9, 3.0), 0.15, 0.125) is None  # 0.55: enough for POE too
    # down toward the unplated hole: the stub end 0.2 mm from the hole edge is too close, 0.3 mm is fine
    assert poe.stub_clear("SIG", "F.Cu", (5.0, 5.0), (5.0, 6.475), 0.15, 0.125) == "NPTH hole of J1"
    assert poe.stub_clear("SIG", "F.Cu", (5.0, 5.0), (5.0, 6.3), 0.15, 0.125) is None
    assert poe.stub_clear("SIG", "F.Cu", (5.0, 5.0), (5.0, 0.2), 0.15, 0.125) == "board edge"
    # the exact segment-to-rectangle distance: a slanted stub passing a pad corner
    assert math.isclose(cleanup._seg_rect_gap((0.0, 0.0), (2.0, 2.0), (1.5, 0.0, 3.0, 0.5)), (1.5 - 0.5) / math.sqrt(2), rel_tol=1e-9)
    assert cleanup._seg_rect_gap((0.0, 0.0), (2.0, 2.0), (1.0, 1.0, 3.0, 3.0)) == 0.0


def test_via_clear_keeps_holes_apart_and_tracks_of_other_nets_away():
    bm = _board([], segments=[SegGeo(2.0, 8.0, 8.0, 8.0, 0.15, "B.Cu", "OTHER")], vias=[ViaGeo(5.0, 5.0, 0.6, 0.3, "SIG")])
    clr = cleanup.Clearance(bm, {}, [], ["F.Cu", "B.Cu"])
    assert clr.via_clear("SIG", 5.7, 5.0, 0.6, 0.3, 0.125) == "SIG via hole"  # 0.7 between centres: 0.4 between drills
    assert clr.via_clear("SIG", 5.85, 5.0, 0.6, 0.3, 0.125) is None
    # copper edge to track edge: 0.5 - 0.3 - 0.075 = 0.125 is the clearance exactly, and KiCad flags only a gap
    # below the required one; 0.05 mm nearer it is a violation
    assert clr.via_clear("SIG", 5.0, 7.5, 0.6, 0.3, 0.125) is None
    assert clr.via_clear("SIG", 5.0, 7.55, 0.6, 0.3, 0.125) == "OTHER track"
    assert clr.via_clear("SIG", 5.0, 7.4, 0.6, 0.3, 0.125) is None
    assert clr.via_clear("SIG", 0.5, 5.0, 0.6, 0.3, 0.125) == "board edge"


def test_open_connections_from_drc_reads_nets_layers_and_positions():
    drc = {"unconnected_items": [{"items": [
        {"description": "Pad 1 [/CM5/UART_RXD] of R20 on F.Cu", "pos": {"x": 180.175, "y": 126.5}},
        {"description": "Track [/CM5/UART_RXD] on B.Cu, length 0.7537 mm", "pos": {"x": 97.46, "y": 99.7}},
    ]}, {"items": [{"description": "Via [X] on F.Cu - B.Cu", "pos": {"x": 1, "y": 2}}]}]}
    out = cleanup.open_connections_from_drc(drc)
    assert len(out) == 1
    oc = out[0]
    assert oc.net == "/CM5/UART_RXD" and oc.a == (180.175, 126.5) and oc.b == (97.46, 99.7)
    assert oc.layer_a == "F.Cu" and oc.layer_b == "B.Cu"


# --------------------------------------------------------------------------------------
# the router on small boards
# --------------------------------------------------------------------------------------


def _connector(x: float, y0: float, nets: list[str], ref="J1") -> FpGeo:
    """A column of 0.7 x 0.2 mm pads at 0.4 mm pitch (a CM5 connector row), the footprint centre to the east."""
    pads = [_pad(ref, str(k + 1), x, y0 + 0.4 * k, 0.7, 0.2, net) for k, net in enumerate(nets)]
    return FpGeo(ref, "lib:conn", x + 2.0, y0 + 0.2 * (len(nets) - 1), 0.0, "F.Cu", pads)


def _other_pads(bm: BoardModel, net: str):
    return [(p, pr._pad_rect(p)) for f in bm.footprints for p in f.pads if p.net != net and p.kind != "np_thru_hole"]


def _assert_clear_of_pads(routes: Routes, bm: BoardModel, net: str, clearance: float = 0.125):
    for s in routes.segments:
        for p, r in _other_pads(bm, net):
            if s.layer == "F.Cu":
                assert _seg_rect_dist((s.x1, s.y1), (s.x2, s.y2), r) >= s.width / 2 + clearance - 1e-6, (s, p.number)
    for v in routes.vias:
        for p, r in _other_pads(bm, net):
            assert _rect_dist(v.x, v.y, r) >= v.size / 2 + clearance - 1e-6, (v, p.number)


def test_route_leaves_a_fine_pitch_pad_through_a_straight_stub():
    conn = _connector(5.0, 4.0, ["A", "B", "SIG", "C", "D"])
    target = FpGeo("R1", "lib:r", 15.0, 5.0, 0.0, "F.Cu", [_pad("R1", "1", 15.0, 5.0, 1.0, 1.0, "SIG")])
    bm = _board([conn, target])
    res = cleanup.route_open_connections(bm, None, [cleanup.OpenConnection("SIG", (5.0, 4.8), (15.0, 5.0), "F.Cu", "F.Cu")])
    assert res.failed == [] and len(res.routed) == 1, res
    assert "stub escape" in res.routed[0]
    assert _connected(res.routes, (5.0, 4.8), (15.0, 5.0))
    _assert_clear_of_pads(res.routes, bm, "SIG")
    stub = res.routes.segments[0]
    assert (stub.x1, stub.y1) == (5.0, 4.8) and stub.x2 < 5.0 and stub.width == 0.15  # west, away from the connector body
    assert all(s.width == 0.15 for s in res.routes.segments) and res.routes.vias == []


def test_route_necks_a_wide_rail_past_a_locating_peg(tmp_path):
    # From a real board: a USB-C VBUS pad 0.6 x 1.15 with its centre 1.52 mm from the board edge, the GND pad
    # 0.8 mm outboard, the next pad 0.65 mm inboard, and an unplated 0.65 mm peg 0.49 mm outboard and 1.08 mm below the
    # pad centre. At 0.5 mm the Power track can leave neither along the pad (the board edge one way, the peg the other)
    # nor across it (pads both ways); a 0.2 mm neck shifted to the pad's inboard side passes the peg.
    j8 = FpGeo("J8", "lib:usbc", 7.0, 5.2, 0.0, "F.Cu", [
        _pad("J8", "A1", 3.8, 1.52, 0.6, 1.15, "GND"),
        _pad("J8", "A4", 4.6, 1.52, 0.6, 1.15, "V5_USB"),
        _pad("J8", "B8", 5.25, 1.52, 0.3, 1.15, "SBU2"),
        _pad("J8", "A5", 5.75, 1.52, 0.3, 1.15, "CC1"),
        _pad("J8", "", 4.11, 2.595, 0.65, 0.65, None, kind="np_thru_hole", drill=0.65),
    ])
    target = FpGeo("C1", "lib:c", 9.0, 8.0, 0.0, "F.Cu", [_pad("C1", "1", 9.0, 8.0, 1.0, 1.4, "V5_USB")])
    bm = _board([j8, target])
    res = cleanup.route_open_connections(bm, _project(tmp_path), [cleanup.OpenConnection("V5_USB", (4.6, 1.52), (9.0, 8.0), "F.Cu", "F.Cu")])
    assert res.failed == [] and "neck escape" in res.routed[0], res
    neck = res.routes.segments[0]
    assert neck.width < 0.5 and (neck.x1, neck.y1) != (4.6, 1.52) and abs(neck.x1 - 4.6) <= 0.3 - neck.width / 2 + 1e-9  # shifted, still inside the pad
    assert neck.x1 > 4.6 and neck.y2 > neck.y1  # inboard of the centre line, heading into the board past the peg
    peg = pr._pad_rect(j8.pads[4])
    assert _seg_rect_dist((neck.x1, neck.y1), (neck.x2, neck.y2), peg) >= neck.width / 2 + cleanup.NPTH_GAP - 1e-6
    _assert_clear_of_pads(res.routes, bm, "V5_USB")
    assert any(s.width == 0.5 for s in res.routes.segments)  # the class width takes over after the neck
    assert _connected(res.routes, (neck.x1, neck.y1), (9.0, 8.0))


def test_route_takes_an_escape_via_out_of_a_closed_pocket():
    # the outer row of a two-row connector: the inner row 1 mm inboard blocks the way through the pad, the
    # neighbours 0.2 mm off block the way across it, and tracks of another net box the outboard side in on F.Cu
    # (a west wall 1.05 mm from the pad ends, top and bottom walls 0.125 mm off the row's end pads). Nothing on F.Cu
    # leads out of that pocket; B.Cu under it is free, so an escape via in the pocket does.
    outer = [_pad("J1", str(k + 1), 5.0, 4.0 + 0.4 * k, 0.7, 0.2, net) for k, net in enumerate(["A", "B", "SIG", "C", "D"])]
    inner = [_pad("J1", str(k + 6), 6.0, 4.0 + 0.4 * k, 0.7, 0.2, net) for k, net in enumerate(["E", "F", "G", "H", "I"])]
    conn = FpGeo("J1", "lib:conn", 5.5, 4.8, 0.0, "F.Cu", outer + inner)
    target = FpGeo("R1", "lib:r", 15.0, 5.0, 0.0, "F.Cu", [_pad("R1", "1", 15.0, 5.0, 1.0, 1.0, "SIG")])
    box = [SegGeo(3.6, 3.7, 3.6, 5.9, 0.15, "F.Cu", "W"),
           SegGeo(3.6, 3.7, 4.65, 3.7, 0.15, "F.Cu", "W"), SegGeo(3.6, 5.9, 4.65, 5.9, 0.15, "F.Cu", "W")]
    bm = _board([conn, target], segments=box)
    res = cleanup.route_open_connections(bm, None, [cleanup.OpenConnection("SIG", (5.0, 4.8), (15.0, 5.0), "F.Cu", "F.Cu")])
    assert res.failed == [] and "via escape" in res.routed[0], res
    assert res.routes.vias and res.routes.vias[0].x < 5.0  # the via sits in the pocket west of the pad
    assert _connected(res.routes, (5.0, 4.8), (15.0, 5.0))
    _assert_clear_of_pads(res.routes, bm, "SIG")
    for v in res.routes.vias:
        for s in box:
            assert cleanup._seg_point_dist((s.x1, s.y1), (s.x2, s.y2), (v.x, v.y)) >= v.size / 2 + s.width / 2 + 0.125 - 1e-6


def test_route_reports_why_an_end_cannot_escape():
    conn = _connector(5.0, 4.0, ["A", "B", "SIG", "C", "D"])
    target = FpGeo("R1", "lib:r", 15.0, 5.0, 0.0, "F.Cu", [_pad("R1", "1", 15.0, 5.0, 1.0, 1.0, "SIG")])
    # tracks of another net on both layers all round the pad row: no stub and no via
    walls = [SegGeo(3.9, 2.0, 3.9, 8.0, 0.3, l, "W") for l in ("F.Cu", "B.Cu")] + [SegGeo(6.1, 2.0, 6.1, 8.0, 0.3, l, "W") for l in ("F.Cu", "B.Cu")]
    walls += [SegGeo(3.9, 2.0, 6.1, 2.0, 0.3, l, "W") for l in ("F.Cu", "B.Cu")] + [SegGeo(3.9, 8.0, 6.1, 8.0, 0.3, l, "W") for l in ("F.Cu", "B.Cu")]
    bm = _board([conn, target], segments=walls)
    res = cleanup.route_open_connections(bm, None, [cleanup.OpenConnection("SIG", (5.0, 4.8), (15.0, 5.0), "F.Cu", "F.Cu")])
    assert res.routed == [] and len(res.failed) == 1
    assert res.failed[0].startswith("SIG: no escape from (5.0, 4.8)") and "W track" in res.failed[0]


def test_wider_classes_route_first_and_ripped_nets_last(tmp_path):
    a = FpGeo("R1", "lib:r", 3.0, 2.0, 0.0, "F.Cu", [_pad("R1", "1", 3.0, 2.0, 1.0, 1.0, "SIG"), _pad("R1", "2", 3.0, 8.0, 1.0, 1.0, "V5_A")])
    b = FpGeo("R2", "lib:r", 17.0, 2.0, 0.0, "F.Cu", [_pad("R2", "1", 17.0, 2.0, 1.0, 1.0, "SIG"), _pad("R2", "2", 17.0, 8.0, 1.0, 1.0, "V5_A")])
    c = FpGeo("R3", "lib:r", 10.0, 5.0, 0.0, "F.Cu", [_pad("R3", "1", 8.0, 5.0, 1.0, 1.0, "CC"), _pad("R3", "2", 12.0, 5.0, 1.0, 1.0, "CC")])
    bm = _board([a, b, c])
    opens = [cleanup.OpenConnection("CC", (8.0, 5.0), (12.0, 5.0), "F.Cu", "F.Cu"),
             cleanup.OpenConnection("SIG", (3.0, 2.0), (17.0, 2.0), "F.Cu", "F.Cu"),
             cleanup.OpenConnection("V5_A", (3.0, 8.0), (17.0, 8.0), "F.Cu", "F.Cu")]
    res = cleanup.route_open_connections(bm, _project(tmp_path), opens, last={"CC"})
    assert [r.split(":")[0] for r in res.routed] == ["V5_A", "SIG", "CC"]
    assert res.failed == []
    assert {s.width for s in res.routes.segments if s.net == "V5_A"} == {0.5}
