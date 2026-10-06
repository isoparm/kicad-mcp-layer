"""The layout rules of review_board, each on a small board whose distances the test states."""

from __future__ import annotations

import pytest

from kicad_layer import layout_rules as lr
from kicad_layer import review
from kicad_layer.config import load_settings, set_settings
from kicad_layer.fab_limits import JLCPCB_2L_1OZ
from kicad_layer.geometry import FillIndex, ampacity, width_for
from tests.synth_board import Board, project


@pytest.fixture
def ws(tmp_path):
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    yield tmp_path
    set_settings(None)


def _bm(ws, board: Board):
    return review.load_board(project(ws, board))


def _soic_with_ep(ref="U1", x=25.0, y=20.0, ep_net="GND", nets=("VIN", "GND", "SW", "FB")):
    pads = [(str(i + 1), nets[i % len(nets)], -2.7, -1.905 + 1.27 * i, 1.6, 0.6) for i in range(4)]
    pads += [(str(8 - i), nets[(i + 2) % len(nets)], 2.7, -1.905 + 1.27 * i, 1.6, 0.6) for i in range(4)]
    pads.append(("9", ep_net, 0, 0, 2.4, 3.1))
    return Board().footprint(ref, x, y, pads)


def test_via_in_signal_pad_is_flagged_thermal_vias_are_not(ws):
    b = _soic_with_ep().footprint("R1", 10, 10, [("1", "A", -0.95, 0, 1.0, 1.4), ("2", "B", 0.95, 0, 1.0, 1.4)])
    b.via("A", 9.05, 10.0).via("GND", 25.0, 20.0).via("GND", 25.0, 21.0)
    chk = lr.check_via_in_pad(_bm(ws, b))
    msgs = [(f.severity, f.message) for f in chk.findings]
    assert chk.verdict == "WARN"
    assert any(s == "warning" and "R1.1" in m for s, m in msgs)
    assert any(s == "info" and "2 via(s) in exposed pads" in m for s, m in msgs)


def test_test_points_name_the_rails_without_one(ws):
    b = Board().footprint("U1", 20, 20, [("1", "+3V3", 0, 0, 1, 1), ("2", "GND", 2, 0, 1, 1), ("3", "SWDIO", 4, 0, 1, 1), ("4", "VDDA", 6, 0, 1, 1)])
    b.footprint("C1", 10, 10, [("1", "+3V3", 0, 0, 1, 1), ("2", "GND", 2, 0, 1, 1)]).footprint("C2", 10, 30, [("1", "VDDA", 0, 0, 1, 1), ("2", "GND", 2, 0, 1, 1)])
    b.footprint("R1", 30, 30, [("1", "SWDIO", 0, 0, 1, 1), ("2", "+3V3", 2, 0, 1, 1)])
    chk = lr.check_test_points(_bm(ws, b))
    text = " | ".join(f.message for f in chk.findings)
    assert chk.verdict == "WARN" and "no test points" in text and "+3V3" in text and "SWDIO" in text
    b.footprint("TP1", 40, 10, [("1", "+3V3", 0, 0, 1, 1, "smd", "circle")], lib="TestPoint:TestPoint_Pad_D1.0mm")
    b.footprint("TP2", 40, 15, [("1", "VDDA", 0, 0, 1, 1, "smd", "circle")], lib="TestPoint:TestPoint_Pad_D1.0mm")
    b.footprint("J1", 45, 30, [("1", "SWDIO", 0, 0, 1.7, 1.7, "thru_hole", "circle", 1.0), ("2", "GND", 0, 2.54, 1.7, 1.7, "thru_hole", "circle", 1.0)],
                lib="Connector_PinHeader_2.54mm:PinHeader_1x02")
    chk = lr.check_test_points(review.load_board(project(ws, b)))
    assert chk.verdict == "PASS", [f.message for f in chk.findings]


def test_exposed_pad_wants_vias_and_copper_under_it(ws):
    b = _soic_with_ep()
    bm = _bm(ws, b)
    chk = lr.check_thermal_pads(bm, FillIndex.from_zones(bm.zones))
    assert chk.verdict == "WARN" and "no thermal vias" in chk.findings[0].message
    for x in (24.4, 25.6):
        for y in (19.2, 20.0, 20.8):
            b.via("GND", x, y)
    b.zone("GND", "B.Cu", [(1, 1), (49, 1), (49, 39), (1, 39)])
    bm = review.load_board(project(ws, b))
    chk = lr.check_thermal_pads(bm, FillIndex.from_zones(bm.zones))
    assert chk.verdict == "PASS", [f.message for f in chk.findings]
    assert chk.data["U1"]["vias"] == 6


def test_fast_net_near_the_edge(ws):
    b = Board().track("SPI_SCK", "F.Cu", 0.25, (5, 1.0), (40, 1.0)).track("SLOW", "F.Cu", 0.25, (5, 1.5), (40, 1.5))
    bm = _bm(ws, b)
    chk = lr.check_fast_edge(bm, bm.path.read_text(encoding="utf-8"))
    assert chk.verdict == "WARN" and [f.net for f in chk.findings] == ["SPI_SCK"]
    assert chk.findings[0].limit == pytest.approx(4 * 1.51, abs=0.01)  # four times the stack-up's 1.51 mm core
    b2 = Board().track("SPI_SCK", "F.Cu", 0.25, (10, 20), (40, 20))
    bm2 = review.load_board(project(ws, b2, "u"))
    assert lr.check_fast_edge(bm2, bm2.path.read_text(encoding="utf-8")).verdict == "PASS"


def _buck(cap_at: tuple[float, float]):
    b = Board(60, 40)
    b.footprint("U1", 30, 20, [("1", "VIN", -2.7, -0.6, 1.6, 0.6), ("2", "GND", -2.7, 0.6, 1.6, 0.6), ("3", "SW", 2.7, -0.6, 1.6, 0.6), ("4", "FB", 2.7, 0.6, 1.6, 0.6)])
    b.footprint("L1", 40, 20, [("1", "SW", -2.5, 0, 2, 3), ("2", "VOUT", 2.5, 0, 2, 3)])
    b.footprint("D1", 34, 14, [("1", "SW", 1.5, 0, 1.5, 1.5), ("2", "GND", -1.5, 0, 1.5, 1.5)])
    b.footprint("C1", *cap_at, [("1", "VIN", 0, -0.95, 1.4, 1.0), ("2", "GND", 0, 0.95, 1.4, 1.0)])
    return b


def test_switcher_hot_loop_input_capacitor_distance(ws):
    bm = _bm(ws, _buck((10, 30)))
    chk = lr.check_switcher_loop(bm, FillIndex.from_zones(bm.zones))
    assert chk.verdict == "WARN" and any("C1" in f.message and "VIN" in f.message for f in chk.findings)
    bm = review.load_board(project(ws, _buck((26.0, 18.6)), "near"))
    chk = lr.check_switcher_loop(bm, FillIndex.from_zones(bm.zones))
    assert chk.data["SW"]["cap"] == "C1" and chk.data["SW"]["catch_diode"] == "D1"
    assert chk.data["SW"]["cap_to_pin_mm"] < 3.0


def test_power_tracks_against_current():
    assert ampacity(1.0) == pytest.approx(2.0, rel=0.25)  # IPC-2221 outer, 1 oz, 10 C: about 2 A at 1 mm
    assert width_for(ampacity(0.8)) == pytest.approx(0.8, rel=1e-6)


def test_power_tracks_with_currents(ws):
    b = Board().track("VIN", "F.Cu", 0.3, (5, 5), (20, 5)).track("VIN", "B.Cu", 2.0, (20, 5), (40, 5)).via("VIN", 20, 5)
    bm = _bm(ws, b)
    chk = lr.check_power_tracks(bm, currents={"VIN": 3.0})
    msgs = " | ".join(f.message for f in chk.findings)
    assert chk.verdict == "FAIL" and "0.3 mm" in msgs and "via" in msgs


def test_antenna_without_keepout(ws):
    b = Board().footprint("U5", 20, 20, [("1", "GND", 0, 0, 1, 1)], lib="RF_Module:ESP32-WROOM-32")
    bm = _bm(ws, b)
    chk = lr.check_antenna(bm, bm.path.read_text(encoding="utf-8"), FillIndex.from_zones(bm.zones))
    assert chk.verdict == "WARN" and "keep-out" in chk.findings[0].message
    assert lr.check_antenna(review.load_board(project(ws, Board(), "x")), "", FillIndex()).verdict == "INFO"


def test_signal_over_a_slot_in_the_plane(ws):
    plane = [(1, 1), (49, 1), (49, 39), (1, 39)]
    slot_fill = [[(1, 1), (24, 1), (24, 39), (1, 39)], [(27, 1), (49, 1), (49, 39), (27, 39)]]  # a 3 mm slot at x 24..27
    b = Board().zone("GND", "B.Cu", plane, slot_fill).track("SPI_SCK", "F.Cu", 0.25, (10, 20), (40, 20)).track("SLOW", "F.Cu", 0.25, (10, 25), (40, 25))
    bm = _bm(ws, b)
    chk = lr.check_plane_reference(bm, FillIndex.from_zones(bm.zones))
    fast = [f for f in chk.findings if f.severity == "warning"]
    assert chk.verdict == "WARN" and fast[0].net == "SPI_SCK" and fast[0].value == pytest.approx(3.0, abs=0.6)
    assert any(f.severity == "info" and "SLOW" in f.message for f in chk.findings)
    unfilled = Board().zone("GND", "B.Cu", plane, []).track("SPI_SCK", "F.Cu", 0.25, (10, 20), (40, 20))
    bm2 = review.load_board(project(ws, unfilled, "u"))
    assert lr.check_plane_reference(bm2, FillIndex.from_zones(bm2.zones)).verdict == "UNVERIFIED"


def test_stitching_edge_gap_on_a_plane_pair(ws):
    plane = [(0.5, 0.5), (49.5, 0.5), (49.5, 39.5), (0.5, 39.5)]
    b = Board().zone("GND", "F.Cu", plane).zone("GND", "B.Cu", plane).via("GND", 5, 1.5)
    bm = _bm(ws, b)
    chk = lr.check_stitching(bm, FillIndex.from_zones(bm.zones), edge_spacing_mm=15)
    assert chk.verdict == "WARN" and "along the edge" in chk.findings[0].message


def test_decoupling_falls_back_to_supply_names_and_checks_the_ground_via(ws):
    b = Board().footprint("U1", 20, 20, [("1", "+3V3", 0, 0, 0.6, 0.6), ("2", "GND", 1, 0, 0.6, 0.6), ("3", "VSENSE_ADC", 2, 0, 0.6, 0.6)])
    b.footprint("C1", 21, 23, [("1", "+3V3", -0.95, 0, 1.0, 1.4), ("2", "GND", 0.95, 0, 1.0, 1.4)])
    bm = _bm(ws, b)
    chk = review.check_decoupling(bm, None)
    assert "supply-named" in chk.evidence
    assert any("ground pad has no via" in f.message for f in chk.findings)
    assert not any("VSENSE" in (f.net or "") for f in chk.findings), "a sense node is not a supply"
    b.via("GND", 23.0, 23.0)
    chk = review.check_decoupling(review.load_board(project(ws, b)), None)
    assert chk.verdict == "PASS", [f.message for f in chk.findings]


def test_usb_pair_found_and_speaker_skipped():
    from kicad_layer.routing import find_pairs

    pairs, _ = find_pairs(["USB_DP", "USB_DM", "SPK_P", "SPK_N"])
    assert ("USB", "USB_DP", "USB_DM") in pairs
    assert review.LOW_SPEED_PAIR.search("SPK") and not review.LOW_SPEED_PAIR.search("USB")


def test_via_ring_uses_the_fabs_via_rule(ws):
    b = Board().via("A", 10, 10, 0.6, 0.3).via("B", 20, 10, 0.35, 0.3)
    dfm = review.check_dfm(_bm(ws, b), JLCPCB_2L_1OZ)
    errs = [f for f in dfm.findings if f.severity == "error"]
    assert len(errs) == 1 and errs[0].net == "B"  # 0.6/0.3 meets JLCPCB's "0.1 mm larger" rule; 0.35/0.3 does not


def test_copper_to_edge_is_measured_on_the_edge_not_its_box(ws):
    # a tall pad 1.55 mm from the right edge: the old bounding-box estimate took its 2.7 mm half height and said 0.3 mm
    b = Board().footprint("L1", 47.0, 20, [("1", "A", 0, 0, 2.9, 5.4)])
    worst = review.copper_to_edge(_bm(ws, b))
    assert worst is not None and worst[0] == pytest.approx(50 - 47 - 1.45, abs=0.01)


def test_edge_connector_overhang_is_info(ws):
    b = Board().footprint("J1", 49, 20, [("1", "VBUS", -1, 0, 0.6, 1.2)], lib="Connector_USB:USB_C_Receptacle", court=(-3, -4, 3, 4))
    chk = review.check_off_board(_bm(ws, b))
    assert chk.verdict == "PASS" and chk.findings[0].severity == "info"


def test_layout_checks_run_together(ws):
    b = _buck((26.0, 18.6)).track("SPI_SCK", "F.Cu", 0.25, (5, 1.0), (40, 1.0))
    ids = [c.id for c in lr.layout_checks(_bm(ws, b))]
    assert ids == ["via_in_pad", "test_points", "thermal_pads", "fast_edge", "stitching", "switcher_loop", "power_tracks", "antenna", "plane_reference"]
