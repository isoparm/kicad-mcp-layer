"""Design review on the fixture boards and on a freshly generated Hello World."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from kicad_layer import review
from kicad_layer.config import load_settings, set_settings
from kicad_layer.fab_limits import JLCPCB_2L_1OZ, limits_for
from tests.conftest import FIXTURES, copy_project, real_kicad


def test_fab_limits_are_sourced():
    assert JLCPCB_2L_1OZ.source.startswith("https://jlcpcb.com")
    assert limits_for("jlcpcb", 4).layers == 4 and limits_for("jlcpcb", 2).layers == 2
    aisler = limits_for("aisler", 4)
    assert aisler.layers == 4 and aisler.min_track_mm == 0.125 and aisler.min_via_drill_mm == 0.25 and "aisler" in aisler.source
    assert limits_for("aisler-4l", 2) is aisler  # only the 4-layer product is entered
    with pytest.raises(KeyError):
        limits_for("nobody", 2)


def test_board_model_reads_geometry(workspace):
    bm = review.load_board(FIXTURES / "pic_programmer" / "pic_programmer.kicad_pcb")
    assert bm.copper_layers == 2 and bm.outline is not None
    assert len(bm.footprints) > 50 and len(bm.segments) > 300
    pads = [p for fp in bm.footprints for p in fp.pads]
    assert any(p.kind == "thru_hole" and p.drill for p in pads)
    assert bm.design_rules.get("min_track_width") == 0.25


def test_identical_findings_are_grouped():
    from kicad_layer.models import ReviewFinding

    fs = [ReviewFinding(check="dfm", severity="error", message="via annular ring 0.125 mm below the absolute minimum 0.18 mm", x_mm=float(i), y_mm=0.0) for i in range(705)]
    fs += [ReviewFinding(check="dfm", severity="warning", message="silkscreen text 'x' height 0.8 mm below 1.0 mm", x_mm=1.0, y_mm=2.0)] * 3
    fs += [ReviewFinding(check="dfm", severity="error", message="one-off error")]
    chk = review._check("dfm", "DFM", fs, "test")
    assert [f.message[:8] for f in chk.findings] == ["via annu", "one-off ", "silkscre"]  # errors first, then most frequent
    assert chk.findings[0].count == 705 and chk.findings[0].x_mm == 0.0 and chk.findings[2].count == 3
    assert chk.verdict == "FAIL" and not chk.truncated
    assert review._counts([chk]) == {"errors": 706, "warnings": 3, "unverified": 0, "checks": 1}


def test_inner_layers_of_any_copper_type_are_counted():
    """KiCad's layer table types inner layers signal, power, mixed or jumper; all are copper."""
    root = Path(__file__).parent.parent.parent / "research"
    minima = root / "fixtures" / "cm5_minima" / "CM5_MINIMA_3.kicad_pcb"
    cm5io = root / "references" / "cm5io" / "CM5IO.kicad_pcb"
    if not minima.exists() or not cm5io.exists():
        pytest.skip("research reference boards not on this machine")
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(root)}))
    try:
        assert review.load_board(cm5io).copper_layers == 4  # In1/In2 typed 'power'
        bm = review.load_board(minima)
    finally:
        set_settings(None)
    assert bm.copper_layers == 6
    dfm = review.check_dfm(bm, limits_for("jlcpcb", 6))
    assert any("6 copper layers" in f.message for f in dfm.findings), "a 6-layer board must be told it got the 4-layer limits"


def test_off_board_and_dfm_without_kicad(workspace):
    bm = review.load_board(FIXTURES / "pic_programmer" / "pic_programmer.kicad_pcb")
    off = review.check_off_board(bm)
    assert off.verdict in ("PASS", "WARN") and not [f for f in off.findings if f.severity == "error"]
    dfm = review.check_dfm(bm, JLCPCB_2L_1OZ)
    assert dfm.verdict in ("PASS", "WARN"), [f.message for f in dfm.findings if f.severity == "error"]
    assert dfm.limit_source and "jlcpcb" in dfm.limit_source


@real_kicad
def test_review_board_fixture(workspace):
    rep = review.review_board(FIXTURES / "pic_programmer" / "pic_programmer.kicad_pcb", fab="jlcpcb")
    ids = [c.id for c in rep.checks]
    assert ids == ["board", "drc", "unrouted", "zone_fills", "off_board", "dfm", "power_tracks", "stitching", "decoupling", "diff_pairs"]
    by = {c.id: c for c in rep.checks}
    assert by["drc"].verdict == "PASS" and by["unrouted"].verdict == "PASS"
    assert rep.verdict in ("PASS", "WARN")
    assert all(c.evidence for c in rep.checks)


@real_kicad
def test_review_flags_the_unrouted_board(workspace):
    rep = review.review_board(FIXTURES / "multichannel" / "multichannel_mixer-unrouted.kicad_pcb", fab="jlcpcb", parity=False)
    by = {c.id: c for c in rep.checks}
    assert by["unrouted"].verdict == "FAIL" and rep.verdict == "FAIL"
    assert by["unrouted"].findings and by["unrouted"].findings[0].x_mm is not None


@real_kicad
def test_review_schematic_fixture(workspace):
    rep = review.review_schematic(FIXTURES / "complex_hierarchy" / "complex_hierarchy.kicad_sch")
    by = {c.id: c for c in rep.checks}
    assert by["erc"].verdict == "PASS"
    assert by["footprints"].verdict == "PASS" and by["annotation"].verdict == "PASS"
    assert by["power_sources"].verdict == "PASS", by["power_sources"]
    assert "spice" in rep.unverified and "no ngspice" in by["spice"].summary


@real_kicad
def test_power_sources_follow_erc_not_the_netlist(tmp_path):
    """PWR_FLAGs are invisible to the netlist: with them the check passes, without them it fails on the right nets."""
    from kicad_layer.sch_edit import Schematic

    work = copy_project("pic_programmer", tmp_path / "pic_programmer")
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    try:
        before = review.review_schematic(work / "pic_programmer.kicad_sch")
        by = {c.id: c for c in before.checks}
        assert by["power_sources"].verdict == "PASS", by["power_sources"]
        assert "GND" in by["power_sources"].data["flag_only"]  # GND has no output pin, only a flag
        removed = 0
        for sheet in work.glob("*.kicad_sch"):
            sch = Schematic(sheet)
            for v in list(sch.placed()):
                if v.lib_id.endswith("PWR_FLAG"):
                    sch.delete(v.uuid)
                    removed += 1
            if sch.is_modified():
                sch.save(force=True)
        assert removed >= 3
        after = review.review_schematic(work / "pic_programmer.kicad_sch")
    finally:
        set_settings(None)
    ps = {c.id: c for c in after.checks}["power_sources"]
    assert ps.verdict == "FAIL" and after.verdict == "FAIL"
    nets = {f.net for f in ps.findings}
    assert "GND" in nets and "VPP" in nets and "/pic_sockets/VCC_PIC" in nets, nets
    assert all(f.sheet and f.x_mm is not None for f in ps.findings)


@real_kicad
@pytest.mark.slow
def test_hello_world_report_lists_every_check(tmp_path):
    """The milestone's definition of done: the Hello World report covers what a fab reviewer asks."""
    build = Path(__file__).parent.parent / "examples" / "hello_world" / "build.py"
    out = tmp_path / "hello-world"
    r = subprocess.run([sys.executable, str(build), str(out), "hello-world"], capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    try:
        rep = review.review_project(out, fab="jlcpcb")
    finally:
        set_settings(None)
    by = {c.id: c for c in rep.checks}
    assert {"erc", "footprints", "values", "annotation", "power_sources", "decoupling_sch", "bom", "spice", "board", "drc", "unrouted", "zone_fills", "off_board", "dfm", "power_tracks", "stitching", "decoupling"} <= set(by)
    assert by["drc"].verdict == "PASS" and by["erc"].verdict == "PASS"
    # +5V and GND come from the USB connector and carry PWR_FLAGs; the first release called them undriven
    assert by["power_sources"].verdict == "PASS", by["power_sources"]
    assert set(by["power_sources"].data["flag_only"]) == {"+5V", "GND"}
    assert by["off_board"].verdict == "PASS" and by["zone_fills"].verdict == "PASS"
    assert by["dfm"].verdict == "PASS", [f.message for f in by["dfm"].findings]
    # a real finding: the ATtiny's decoupling capacitor sits about 11 mm away
    dec = by["decoupling"]
    assert any(f.ref == "U1" and (f.value or 0) > 5 for f in dec.findings), dec
    assert rep.unverified == ["spice"]
