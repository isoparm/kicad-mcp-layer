"""The PCBWay package's pure parts: reference ranges, package names, mounting types, footprint attributes."""

from __future__ import annotations

from kicad_layer.design import fab


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
