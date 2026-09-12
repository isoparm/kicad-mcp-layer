"""Blocks: members placed from the anchor, grouped, on a KiCad-formatted board file."""

from __future__ import annotations

from pathlib import Path

from kicad_layer.design.blocks import Block, apply, footprints, place

BOARD = """(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general
\t\t(thickness 1.6)
\t)
\t(layers
\t\t(0 "F.Cu" signal)
\t)
\t(net 0 "")
\t(footprint "CM5IO:Raspberry-Pi-5-Compute-Module_GPIO"
\t\t(layer "F.Cu")
\t\t(uuid "aaaa-1")
\t\t(at 99.5 121 90)
\t\t(property "Reference" "MOD1"
\t\t\t(at 0 -2 0)
\t\t\t(layer "F.SilkS")
\t\t)
\t\t(pad "1" smd rect
\t\t\t(at 0.4 -2.5)
\t\t)
\t)
\t(footprint "CM5IO:Raspberry-Pi-5-Compute-Module_HSS"
\t\t(layer "F.Cu")
\t\t(uuid "bbbb-2")
\t\t(at 140 30)
\t\t(property "Reference" "MOD2"
\t\t\t(at 0 -2 0)
\t\t)
\t)
\t(footprint "Resistor_SMD:R_0603_1608Metric"
\t\t(layer "F.Cu")
\t\t(uuid "cccc-3")
\t\t(at 10 10 180)
\t\t(property "Reference" "R1"
\t\t\t(at 0 -1 0)
\t\t)
\t)
\t(group "CM5 module"
\t\t(uuid "old-group")
\t\t(members "aaaa-1")
\t)
)
"""


def test_footprints_are_found_with_their_position_and_uuid():
    fps = footprints(BOARD)
    assert set(fps) == {"MOD1", "MOD2", "R1"}
    assert fps["MOD1"]["at"] == (99.5, 121.0, 90.0) and fps["MOD1"]["uuid"] == "aaaa-1"
    assert fps["MOD2"]["at"] == (140.0, 30.0, 0.0)


def test_place_rotates_the_offset_with_the_anchor():
    assert place((10, 10, 0), (5, 0, 0)) == (15, 10, 0)
    assert place((10, 10, 90), (5, 0, 0)) == (10, 5, 90), "to the right of an anchor turned 90 degrees is up on screen"
    assert place((10, 10, 180), (5, 2, 45)) == (5, 8, 225)


def test_apply_moves_members_and_replaces_the_group(tmp_path: Path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text(BOARD, encoding="utf-8")
    report = apply(board, [Block("CM5 module", "MOD1", {"MOD2": (0, 0, 0)})])
    text = board.read_text(encoding="utf-8")
    fps = footprints(text)
    assert fps["MOD2"]["at"] == (99.5, 121.0, 90.0), report
    assert fps["MOD1"]["at"] == (99.5, 121.0, 90.0) and fps["R1"]["at"] == (10.0, 10.0, 180.0), "only members move"
    assert text.count('(group "CM5 module"') == 1, "the old group is replaced, not duplicated"
    assert '(members "aaaa-1" "bbbb-2")' in text
    assert "old-group" not in text
    assert text.endswith(")\n") and text.count("(footprint ") == 3
    assert "(at 0.4 -2.5)" in text, "pads and texts are untouched"
    # a second run changes nothing but the group's uuid
    before = text
    apply(board, [Block("CM5 module", "MOD1", {"MOD2": (0, 0, 0)})])
    after = board.read_text(encoding="utf-8")
    assert footprints(after) == {k: {**v, "span": v["span"], "at_span": v["at_span"]} for k, v in footprints(before).items()}


def test_apply_reports_a_missing_anchor_and_leaves_the_file(tmp_path: Path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text(BOARD, encoding="utf-8")
    report = apply(board, [Block("PoE", "U3", {"J7": (0, 30, 0)})])
    assert any("not on the board yet" in line for line in report)
    assert board.read_text(encoding="utf-8") == BOARD
