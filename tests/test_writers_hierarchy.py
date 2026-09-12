"""Hierarchical schematics and multi-layer boards from the writers, checked by KiCad itself."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_layer import review
from kicad_layer.cli import netlist as netlist_mod
from kicad_layer.cli import reports
from kicad_layer.config import load_settings, set_settings
from kicad_layer.pcb_writer import BoardBuilder, copper_layer_names
from kicad_layer.sch_writer import SchematicBuilder
from tests.conftest import real_kicad


def _write_project(pro: Path, sheets: list[list[str]]) -> None:
    pro.write_text(json.dumps({"meta": {"filename": pro.name, "version": 3}, "sheets": sheets}), encoding="utf-8")


def _divider_project(d: Path, name: str = "hier") -> tuple[Path, dict[str, str]]:
    """Root: +5V through sheet 'Divider' into sheet 'Load'. Divider: R1/R2 to GND. Load: R3 to GND."""
    root = SchematicBuilder(name, title="hierarchy test")
    div = root.sheet("Divider", "Divider.kicad_sch", (50.8, 50.8), (25.4, 12.7), pins_left=[("VIN", "input")], pins_right=[("VOUT", "output")])
    load = root.sheet("Load", "Load.kicad_sch", (101.6, 50.8), (25.4, 12.7), pins_left=[("IN", "input")])
    # +5V with a flag into VIN; VOUT across to the load sheet
    vin = div.pin("VIN")
    root.power("+5V", (vin[0] - 5.08, vin[1] - 2.54))
    root.pwr_flag((vin[0] - 5.08, vin[1] - 2.54))
    root.polyline((vin[0] - 5.08, vin[1] - 2.54), (vin[0] - 5.08, vin[1]), vin)
    root.wire(div.pin("VOUT"), load.pin("IN"))

    ds = root.child(div, title="divider")
    r1 = ds.place("Device", "R", "R1", (76.2, 63.5), rot=0, value_text="10k", footprint="Resistor_SMD:R_0603_1608Metric")
    r2 = ds.place("Device", "R", "R2", (76.2, 76.2), rot=0, value_text="10k", footprint="Resistor_SMD:R_0603_1608Metric")
    ds.hier_label("VIN", r1.pin("1"), shape="input", rot=90)
    ds.wire(r1.pin("2"), r2.pin("1"))
    mid = r1.pin("2")
    ds.wire(mid, (mid[0] + 7.62, mid[1]))
    ds.hier_label("VOUT", (mid[0] + 7.62, mid[1]), shape="output", rot=0)
    ds.junction(mid)
    ds.power("GND", r2.pin("2"))
    ds.pwr_flag(r2.pin("2"))

    ls = root.child(load, title="load")
    r3 = ls.place("Device", "R", "R3", (76.2, 63.5), rot=0, value_text="1k", footprint="Resistor_SMD:R_0603_1608Metric")
    ls.hier_label("IN", r3.pin("1"), shape="input", rot=90)
    ls.power("GND", r3.pin("2"))

    root.write(str(d / f"{name}.kicad_sch"))
    ds.write(str(d / "Divider.kicad_sch"))
    ls.write(str(d / "Load.kicad_sch"))
    _write_project(d / f"{name}.kicad_pro", root.sheet_list({"Divider": ds, "Load": ls}))
    paths = {p.ref: f"{b.path}/{p.uuid}" for b in (ds, ls) for p in b.placed if not p.ref.startswith("#")}
    return d / f"{name}.kicad_sch", paths


def test_sub_sheet_symbols_carry_the_sheet_path(tmp_path):
    root_sch, paths = _divider_project(tmp_path)
    root_uuid = root_sch.read_text(encoding="utf-8").split('(uuid "', 1)[1].split('"', 1)[0]
    for ref, path in paths.items():
        assert path.startswith(f"/{root_uuid}/") and path.count("/") == 3, (ref, path)
    sub = (tmp_path / "Divider.kicad_sch").read_text(encoding="utf-8")
    assert "sheet_instances" not in sub and "(hierarchical_label \"VOUT\"" in sub
    root_text = root_sch.read_text(encoding="utf-8")
    assert root_text.count("(sheet\n") == 2 and '(pin "VOUT" output' in root_text and "(sheet_instances" in root_text


@real_kicad
def test_hierarchy_passes_erc_and_connects_across_sheets(tmp_path):
    root_sch, _ = _divider_project(tmp_path)
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    try:
        erc = reports.run_erc(root_sch, severity="all")
        assert erc.verdict == "PASS", [(f.type, f.description, [i.description for i in f.items]) for f in erc.findings]
        nl = netlist_mod.load_netlist(root_sch, refresh=True, include_components=True, max_nets=100)
    finally:
        set_settings(None)
    by_pin = {(n.ref, n.pin): net.name for net in nl.nets for n in net.nodes}
    assert by_pin[("R1", "1")] == "+5V"
    assert by_pin[("R2", "1")] == by_pin[("R3", "1")] and by_pin[("R2", "1")] not in ("+5V", "GND")
    assert by_pin[("R2", "2")] == "GND" == by_pin[("R3", "2")]
    assert {c.ref for c in nl.components} == {"R1", "R2", "R3"}


def test_copper_layer_names():
    assert copper_layer_names(2) == ["F.Cu", "B.Cu"]
    assert copper_layer_names(4) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    with pytest.raises(ValueError):
        copper_layer_names(3)


@real_kicad
def test_four_layer_board_is_read_back_as_four_layers(tmp_path):
    pcb = BoardBuilder(sheetfile="x.kicad_sch", title="4L", copper_layers=4)
    pcb.rounded_rect_outline(50, 50, 80, 70, 2)
    pour = [(51, 51), (79, 51), (79, 69), (51, 69)]
    pcb.zone(net="GND", layer="In2.Cu", polygon=pour, name="GND plane")
    pcb.via((60, 60), net="GND")
    pcb.via((62, 60), net="GND", layers=("F.Cu", "In1.Cu"))
    pcb.track([(60, 60), (62, 60)], width=0.3, layer="F.Cu", net="GND")
    out = tmp_path / "four.kicad_pcb"
    pcb.write(str(out))
    text = out.read_text(encoding="utf-8")
    assert '(4 "In1.Cu" signal)' in text and '(6 "In2.Cu" signal)' in text
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    try:
        assert review.load_board(out).copper_layers == 4
        drc = reports.run_drc(out, severity="all", schematic_parity=False)
    finally:
        set_settings(None)
    assert drc.verdict in ("PASS", "WARN"), [(f.type, f.description) for f in drc.findings if f.severity == "error"]
