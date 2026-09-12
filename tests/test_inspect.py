"""The inspect command answers board questions from the file alone, in a few lines."""

from __future__ import annotations

from pathlib import Path

from kicad_layer.design import inspect as ins
from tests.conftest import FIXTURES

BOARD = FIXTURES / "complex_hierarchy" / "complex_hierarchy.kicad_pcb"


def test_parts_lists_every_footprint_once():
    lines = ins.answer([str(BOARD), "parts"])
    refs = [l.split()[0] for l in lines]
    assert len(refs) == len(set(refs)) > 20
    assert all(("rot" in l and "Cu" in l) for l in lines)


def test_part_and_pin_and_net_agree():
    part_lines = ins.answer([str(BOARD), "part", "C101"])
    assert part_lines[0].startswith("C101 ") and any("pad 1" in l for l in part_lines)
    pin_lines = ins.answer([str(BOARD), "pin", "C101", "1"])
    assert pin_lines[0].startswith("C101.1 at (")
    net_name = pin_lines[0].split(" on ")[-1]
    net_lines = ins.answer([str(BOARD), "net", net_name.rsplit("/", 1)[-1]])
    assert net_lines[0].startswith(net_name) and any(l.strip().startswith("C101.1") for l in net_lines)
    assert net_lines[-1].strip().startswith("copper:")


def test_classes_read_the_project_file():
    lines = ins.answer([str(BOARD), "classes"])
    assert lines and lines[0].startswith("Default") and "clearance" in lines[0]


def test_missing_report_is_named_not_faked(tmp_path):
    import shutil

    work = tmp_path / "b"
    work.mkdir()
    shutil.copy2(BOARD, work / BOARD.name)
    assert ins.answer([str(work), "drc"]) == ["no _drc.json next to the board; run the build"]
    assert ins.answer([str(work), "unrouted"])[0].startswith("no _drc.json")
    assert ins.answer([str(work), "classes"]) == ["no .kicad_pro next to the board"]


def test_folder_argument_finds_the_board():
    lines = ins.answer([str(BOARD.parent), "bbox", "C101", "NOPE"])
    assert lines[0].startswith("outline (") and any(l.startswith("C101 ") for l in lines) and "no footprint NOPE" in lines


def test_usage_on_bad_question():
    assert "parts" in ins.answer([str(BOARD)])[0] and "bbox" in ins.answer([str(BOARD), "what"])[0]
