"""Differential pairs, length matching and impedance estimates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_layer import routing
from kicad_layer.config import load_settings, set_settings
from kicad_layer.pcb_writer import BoardBuilder
from kicad_layer.review import load_board

W, GAP = 0.1722, 0.15  # JLCPCB's 100 ohm geometry on JLC04161H-7628


@pytest.fixture(autouse=True)
def workspace(tmp_path):
    """Board files live in tmp_path, so the workspace must be there too."""
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    try:
        yield
    finally:
        set_settings(None)


def _project(tmp_path: Path) -> Path:
    pro = {
        "board": {"design_settings": {"rules": {}}},
        "net_settings": {
            "classes": [
                {"name": "Default", "clearance": 0.125, "track_width": 0.15, "diff_pair_width": 0.15, "diff_pair_gap": 0.2},
                {"name": "100R", "clearance": 0.15, "track_width": W, "diff_pair_width": W, "diff_pair_gap": GAP},
                {"name": "90R", "clearance": 0.15, "track_width": 0.2332, "diff_pair_width": 0.2332, "diff_pair_gap": GAP},
            ],
            "netclass_assignments": [{"netclass": "100R", "pattern": "*DSI_*"}, {"netclass": "90R", "pattern": "*PCIE_*"}],
        },
    }
    p = tmp_path / "t.kicad_pro"
    p.write_text(json.dumps(pro), encoding="utf-8")
    return p


def _board(tmp_path: Path) -> Path:
    pcb = BoardBuilder(sheetfile="t.kicad_sch", title="t", copper_layers=4)
    pcb.rounded_rect_outline(0, 0, 60, 30, 1)
    # pair A: matched, coupled all the way at the class gap
    pcb.track([(5, 5), (45, 5)], width=W, layer="F.Cu", net="/S/DSI_D0_P")
    pcb.track([(5, 5 + W + GAP), (45, 5 + W + GAP)], width=W, layer="F.Cu", net="/S/DSI_D0_N")
    # pair B: wrong width for its 90R class, the N half 0.55 mm longer and off the gap for half its run
    pcb.track([(5, 10), (45, 10)], width=W, layer="F.Cu", net="PCIE_TX_P")
    pcb.track([(5, 10 + W + GAP), (25, 10 + W + GAP), (25, 10 + W + 0.3), (45.4, 10 + W + 0.3)], width=W, layer="F.Cu", net="PCIE_TX_N")
    # pair C: equal tracks, but the P half changes layer once
    pcb.track([(5, 15), (15, 15)], width=W, layer="F.Cu", net="USBA_DP")
    pcb.track([(5, 15.5), (15, 15.5)], width=W, layer="F.Cu", net="USBA_DN")
    pcb.via((15, 15), net="USBA_DP", layers=("F.Cu", "B.Cu"))
    # a lone half and a plain net
    pcb.track([(5, 20), (10, 20)], width=0.15, layer="F.Cu", net="LONE_P")
    pcb.track([(5, 22), (10, 22)], width=0.15, layer="F.Cu", net="GPIO4")
    path = tmp_path / "t.kicad_pcb"
    pcb.write(str(path))
    return path


def test_pairs_are_found_by_name():
    nets = ["/CM5/DSI_D0_P", "/CM5/DSI_D0_N", "ETH_TRD3_P", "ETH_TRD3_N", "/Power/USBC_DP", "/Power/USBC_DN", "POE_VIN_P", "POE_VIN_N", "LONE_P", "GND", "+5V", "SD_CMD"]
    pairs, lone = routing.find_pairs(nets)
    names = {p[0] for p in pairs}
    assert names == {"DSI_D0", "ETH_TRD3", "USBC", "POE_VIN"}
    assert lone == ["LONE_P"]
    assert routing.rule_for("DSI_D0") == (100.0, 0.15, routing.INTERFACE_RULES[4][3])
    assert routing.rule_for("PCIE_TX")[0:2] == (90.0, 0.10)
    assert routing.rule_for("USBC")[0:2] == (90.0, 0.15)
    assert routing.rule_for("POE_VIN") is None


def test_route_check_measures_skew_gap_width_and_vias(tmp_path):
    board = _board(tmp_path)
    project = _project(tmp_path)
    rep = routing.route_check(board, project)
    by = {p.name: p for p in rep.pairs}
    a = by["DSI_D0"]
    assert a.status == "ok" and a.netclass == "100R" and a.target_impedance_ohm == 100.0
    assert a.p_length_mm == 40.0 and a.skew_mm == 0.0 and a.coupled_fraction == 1.0 and a.gap_deviations == 0 and a.width_deviations == 0
    b = by["PCIE_TX"]
    assert b.status == "warn" and b.netclass == "90R"
    assert b.skew_mm == pytest.approx(0.55, abs=0.01) and b.skew_limit_mm == 0.10
    assert b.gap_deviations >= 1 and b.width_deviations == 4  # all four segments are 0.1722 wide, the class wants 0.2332
    assert b.coupled_fraction is not None and 0.45 <= b.coupled_fraction <= 0.55
    c = by["USBA"]
    assert c.status == "warn" and c.p_vias == 1 and c.n_vias == 0
    assert c.p_length_mm == pytest.approx(11.6) and any("layers" in n for n in c.notes)
    assert rep.unpaired == ["LONE_P"]
    assert rep.summary == {"ok": 1, "warn": 2, "partial": 0, "unrouted": 0} and rep.verdict == "WARN"
    # without a project the class checks are skipped and say so
    rep2 = routing.analyse(load_board(board), None)
    assert any("net classes" in n for n in rep2.notes)
    assert {p.name: p.gap_deviations for p in rep2.pairs}["PCIE_TX"] == 0


def test_impedance_estimates_and_table():
    r = routing.impedance(W, GAP)
    assert r.table_match == {"target_ohm": 100, "w": W, "s": GAP}
    assert 85 < r.differential_ohm < 125  # closed form, about ten percent above the fab's solver
    assert 60 < r.single_ended_ohm < 85
    se = routing.impedance(0.3244)
    assert se.table_match == {"target_ohm": 50, "w": 0.3244} and 42 < se.single_ended_ohm < 62 and se.differential_ohm is None
    # wider is lower, wider gap is higher
    assert routing.impedance(0.3, GAP).differential_ohm < r.differential_ohm < routing.impedance(0.12, GAP).differential_ohm
    assert routing.impedance(W, 0.3).differential_ohm > r.differential_ohm
    assert routing.suggest_geometry(90) == {"target_ohm": 90, "w": 0.2332, "s": GAP, "from": "fab table", "source": routing.JLC04161H_7628.source}
    solved = routing.suggest_geometry(75, differential=False)
    assert 0.1 < solved["w"] < 0.3 and solved["from"].startswith("closed-form")
    info = routing.stackup_info("JLC04161H_7628")
    assert info.thickness_mm == pytest.approx(1.586, abs=0.01) and info.table["100"]["w"] == W
    assert info.presets == ["aisler-4l-1.6mm", "jlc04161h-7628", "pcbway-4l-1.6mm"]
    with pytest.raises(Exception):
        routing.get_stackup("nope")


def test_pcbway_preset_is_thinner_and_has_no_table():
    info = routing.stackup_info("pcbway")
    assert info.name == "PCBWay-4L-1.6mm" and info.table == {} and info.thickness_mm == pytest.approx(1.541, abs=0.005)
    assert info.layers[1] == {"layer": "prepreg 7628 RC46%", "thickness_mm": 0.1855, "er": 4.74}
    assert routing.get_stackup("PCBWay_4L_1.6mm") is routing.PCBWAY_4L_1P6
    assert routing.get_stackup("aisler") is routing.AISLER_4L_1P6 and routing.AISLER_4L_1P6.thickness == 1.529
    assert routing.suggest_geometry(100, stackup="aisler") == {"target_ohm": 100, "w": 0.22, "s": 0.15, "from": "fab table", "source": routing.AISLER_4L_1P6.source}
    assert routing.suggest_geometry(90, stackup="aisler")["w"] == 0.26
    est = routing.impedance(0.22, 0.15, stackup="aisler")  # the closed form agrees with AISLER's published pair within its own tolerance
    assert 85 <= est.differential_ohm <= 115, est
    r = routing.impedance(W, GAP, stackup="pcbway-4l-1.6mm")
    assert r.table_match is None and r.dielectric_mm == 0.1855 and r.er == 4.74
    # thinner, higher-Dk prepreg than JLCPCB's: the same geometry reads lower
    assert r.differential_ohm < routing.impedance(W, GAP).differential_ohm
    assert 98 < r.differential_ohm < 108  # 103.0 by the closed form
    assert 86 < routing.impedance(0.2332, GAP, stackup="pcbway").differential_ohm < 94  # 89.6
    g = routing.suggest_geometry(100, stackup="pcbway")
    assert g["from"].startswith("closed-form") and g["s"] == GAP and 0.175 < g["w"] < 0.195  # 0.1842
