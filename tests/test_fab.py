"""The PCBWay package: reference ranges, package names, mounting types, footprint attributes, do-not-populate parts left out."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_layer.cli import exports
from kicad_layer.config import load_settings, set_settings
from kicad_layer.design import fab
from kicad_layer.kicad_libs import load_footprint
from kicad_layer.pcb_writer import BoardBuilder
from kicad_layer.sexpr import child


def test_grouped_references_expand():
    assert fab.expand_refs("C1,C5-C7,C10") == ["C1", "C5", "C6", "C7", "C10"]
    assert fab.expand_refs("R1-R3, U2") == ["R1", "R2", "R3", "U2"]
    assert fab.expand_refs("") == []


def test_package_name_prefers_the_explicit_field_then_the_imperial_size():
    assert fab.package_name("Capacitor_SMD:C_0402_1005Metric", "") == "0402"
    assert fab.package_name("Resistor_SMD:R_0603_1608Metric", "0603 thin film") == "0603 thin film"
    assert fab.package_name("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "") == "SOIC-8_3.9x4.9mm_P1.27mm"


def test_mount_type_from_the_attr_line():
    assert fab.mount_type("smd") == "SMD"
    assert fab.mount_type("through_hole exclude_from_pos_files") == "THT"
    assert fab.mount_type("") == "SMD"


def test_footprint_attrs_read_from_a_board(tmp_path):
    pcb = tmp_path / "x.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (footprint "A:B" (attr smd) (property "Reference" "C1" (at 0 0)))'
        ' (footprint "C:D" (attr through_hole) (property "Reference" "J1" (at 0 0)))'
        ' (footprint "E:F" (property "Reference" "H1" (at 0 0))))',
        encoding="utf-8",
    )
    assert fab.footprint_attrs(pcb) == {"C1": "smd", "J1": "through_hole", "H1": ""}


def test_a_dnp_footprint_carries_the_attribute_and_stays_smd():
    try:
        fp = load_footprint("Resistor_SMD", "R_0805_2012Metric")
    except Exception as exc:  # the library is not installed here
        pytest.skip(f"footprint not available: {exc}")
    pcb = BoardBuilder(sheetfile="t.kicad_sch", title="t", copper_layers=2)
    fitted = pcb.footprint(fp, "R1", "10k", (10.0, 10.0), 0, pad_nets={})
    left_out = pcb.footprint(fp, "R2", "10k", (20.0, 10.0), 0, pad_nets={}, dnp=True)
    assert [str(a) for a in child(fitted, "attr")[1:]] == ["smd"]
    assert [str(a) for a in child(left_out, "attr")[1:]] == ["smd", "dnp"]
    assert fab.mount_type("smd dnp") == "SMD"


def test_the_package_leaves_dnp_parts_out_of_the_bom_and_the_positions(tmp_path, monkeypatch):
    commands = []

    def run(cmd, **kw):
        commands.append([str(c) for c in cmd])
        out = cmd[cmd.index("-o") + 1]
        if str(out).endswith(".csv"):
            Path(out).write_text("Reference,Value\nR1,10k\n", encoding="utf-8")
        return SimpleNamespace(ok=True, returncode=0, command=[str(c) for c in cmd], stdout="", stderr="", tail=lambda: "")

    monkeypatch.setattr(exports, "find_kicad_cli", lambda: SimpleNamespace(path="kicad-cli", pcb_export_verb=lambda a, b: a))
    monkeypatch.setattr(exports.runner, "run", run)
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path)}))
    try:
        (tmp_path / "b.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
        (tmp_path / "b.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
        exports.export_fab(tmp_path / "b.kicad_pcb", output_dir=str(tmp_path / "fab"), gerbers=False, drill=False, position=True, exclude_dnp=True)
        exports.export_bom(tmp_path / "b.kicad_sch", output_path=str(tmp_path / "bom.csv"), exclude_dnp=True)
        exports.export_bom(tmp_path / "b.kicad_sch", output_path=str(tmp_path / "bom2.csv"))
    finally:
        set_settings(None)
    pos, bom, plain = commands
    assert "--exclude-dnp" in pos and "--exclude-dnp" in bom and "--exclude-dnp" not in plain
