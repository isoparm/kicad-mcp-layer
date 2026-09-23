"""Field findings from the notch_board (Audio_Tester), 2026-09-22: one reproduction per confirmed finding.

Each test states the behaviour the finding asks for and is marked ``xfail(strict=True)``: the suite stays
green while the defect is there, and the test turns into an XPASS failure the day it is fixed, so the
mark (and the entry in docs/field-findings.md) goes with the fix. Numbers refer to that document.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_layer.config import load_settings, set_settings
from kicad_layer.design import board as board_mod
from kicad_layer.design import build, copper, stubs
from kicad_layer.design.circuit import Circuit
from kicad_layer.design.lint import lint
from kicad_layer.design.module_sheet import ModuleSheet, build_module_sheet
from kicad_layer.design.parts import Part
from kicad_layer.design.project import Project, RootLayout, Rules, write_project_file
from kicad_layer.design.render import At, Layout, render
from kicad_layer.design.signals import Signal
from kicad_layer.errors import KICAD_CLI_NOT_FOUND, LayerError
from kicad_layer.ids import IdFactory
from kicad_layer.review import BoardModel, FpGeo, PadGeo
from kicad_layer.sch_writer import SchematicBuilder
from kicad_layer.sexpr import child
from tests.conftest import FIXTURES

TEMPLATE_PRO = FIXTURES / "pic_programmer" / "pic_programmer.kicad_pro"

OPAMP = Part("OPAMP", ("Amplifier_Operational", "OPA1678"), "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "OPA1678", "Texas Instruments", "OPA1678IDR")
HOLE = Part("HOLE", ("Mechanical", "MountingHole"), "MountingHole:MountingHole_3.2mm_M3", "M3", "-", "-")
HDR3 = Part("HDR3", ("Connector_Generic", "Conn_01x03"), "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical", "HDR", "-", "-")
HDR2 = Part("HDR2", ("Connector_Generic", "Conn_01x02"), "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", "HDR", "-", "-")


def xfail(n: int, condition: bool = True):
    return pytest.mark.xfail(condition, strict=True, reason=f"docs/field-findings.md #{n}")


def _need_symbol(lib: str, name: str) -> None:
    from kicad_layer.kicad_libs import load_symbol

    try:
        load_symbol(lib, name)
    except Exception as exc:  # the library is not installed here
        pytest.skip(f"symbol {lib}:{name} not available: {exc}")


def _need_footprint(lib: str, name: str):
    from kicad_layer.kicad_libs import load_footprint

    try:
        return load_footprint(lib, name)
    except Exception as exc:
        pytest.skip(f"footprint {lib}:{name} not available: {exc}")


def _sch() -> SchematicBuilder:
    return SchematicBuilder("t", ids=IdFactory("t"))


def _power_groups(sch: SchematicBuilder) -> list[set[str]]:
    """Power-symbol names per connected group of wires, power pins and flags (endpoints only, as KiCad joins them)."""
    parent: dict = {}

    def find(p):
        parent.setdefault(p, p)
        while parent[p] != p:
            parent[p] = parent[parent[p]]
            p = parent[p]
        return p

    for node in sch.items:
        if str(node[0]) == "wire":
            pts = child(node, "pts")
            a, b = [(round(float(q[1]), 3), round(float(q[2]), 3)) for q in pts[1:3]]
            parent[find(a)] = find(b)
    names: dict = {}
    for pl in sch.placed:
        if pl.symbol.lib_id.startswith("power:") and pl.symbol.name != "PWR_FLAG":
            key = find((round(pl.at[0], 3), round(pl.at[1], 3)))
            names.setdefault(key, set()).add(pl.symbol.name)
    return list(names.values())


# ---------------------------------------------------------------- the design package: sheets


def test_1_a_dual_opamp_is_drawn_with_every_unit():
    _need_symbol("Amplifier_Operational", "OPA1678")
    c = Circuit("T", [])
    u = c.part("U1", OPAMP)
    c.net(u["1"], u["2"], name="A_OUT")
    c.net(u["3"], u["7"], name="B_OUT")
    c.net(u["5"], u["6"], name="B_IN")
    c.rail("+12V", u["8"])
    c.gnd(u["4"])
    assert c.check() == []
    sch = _sch()
    render(c, Layout(), sch)
    assert {pl.unit for pl in sch.placed if pl.ref == "U1"} == {1, 2, 3}


def test_2_lint_reads_only_the_pins_of_the_placed_unit():
    """Unit 2's pin 5 sits where unit 1's pin 3 does: counted as a second terminal, it hides a pin that meets a wire mid-span."""
    _need_symbol("Amplifier_Operational", "OPA1678")
    sch = _sch()
    u = sch.place("Amplifier_Operational", "OPA1678", "U1", (101.6, 101.6), unit=1)
    x, y = u.pin("3")
    sch.wire((x - 2.54, y), (x + 2.54, y))  # through pin 3, no junction: KiCad does not connect it
    errors, _ = lint(sch)
    assert any("U1 pin 3 meets a wire" in e for e in errors), errors


def test_4_the_flow_layout_takes_a_symbol_without_pins():
    _need_symbol("Mechanical", "MountingHole")
    c = Circuit("T", [])
    c.part("H1", HOLE)
    sch = _sch()
    render(c, Layout(), sch)
    assert [pl.ref for pl in sch.placed] == ["H1"]


def test_6_a_pwr_flag_lead_does_not_short_the_next_connector_pin():
    _need_symbol("Connector_Generic", "Conn_01x03")
    c = Circuit("T", [Signal("SIG", "1", "out", "T")])
    j = c.part("J1", HDR3)
    c.rail("+12V", j["1"])
    c.gnd(j["2"])
    c.signal("SIG", j["3"])
    c.flag("+12V")
    sch = _sch()
    render(c, Layout(parts={"J1": At(50.8, 50.8)}), sch)
    shorted = [g for g in _power_groups(sch) if len(g) > 1]
    assert not shorted, f"power nets joined by the drawing: {shorted}"


def test_14_a_circuit_note_is_drawn():
    _need_symbol("Mechanical", "MountingHole")
    c = Circuit("T", [])
    c.part("H1", HOLE)
    c.note("NOTCH AT 1 KHZ")
    sch = _sch()
    render(c, Layout(parts={"H1": At(50.8, 50.8)}), sch)
    assert any(str(n[0]) == "text" and str(n[1]) == "NOTCH AT 1 KHZ" for n in sch.items)


@xfail(20)
def test_20_two_pin_parts_hanging_from_one_label_are_drawn_apart():
    """R1, C1 and R2 all run from the signal S to ground: the flow hangs each from the same tap on J1's stub, one on another."""
    _need_symbol("Connector_Generic", "Conn_01x03")
    _need_symbol("Device", "C")
    r = Part("R", ("Device", "R"), "Resistor_SMD:R_0805_2012Metric", "10k", "-", "-")
    cap = Part("C", ("Device", "C"), "Capacitor_SMD:C_0805_2012Metric", "100nF", "-", "-")
    c = Circuit("T", [Signal("S", "1", "out", "T")])
    j = c.part("J1", HDR3)
    r1, c1, r2 = c.part("R1", r), c.part("C1", cap), c.part("R2", r)
    c.signal("S", j["1"], r1["1"], c1["1"], r2["1"])
    c.gnd(j["2"], r1["2"], c1["2"], r2["2"])
    c.nc(j, "3")
    sch = _sch()
    render(c, Layout(), sch)  # a plain sheet: no places, the flow draws it
    at = {pl.ref: pl.at for pl in sch.placed if pl.ref in ("R1", "C1", "R2")}
    assert len(set(at.values())) == 3, at
    assert lint(sch)[0] == []


def _hier_labels(sch: SchematicBuilder) -> list[tuple[str, float, str]]:
    """(name, x, shape) of every hierarchical label on a sheet."""
    return sorted((str(n[1]), float(child(n, "at")[1]), str(child(n, "shape")[1])) for n in sch.items if str(n[0]) == "hierarchical_label")


def test_12_a_signal_names_one_connector_of_a_two_connector_module():
    """Signals were keyed by pin number only, so pin 1 of every module connector carried the label."""
    _need_symbol("Connector_Generic", "Conn_01x02")
    parts = (("J1", HDR2, (101.6, 101.6), (101.6, 96.52), (101.6, 106.68)),
             ("J2", HDR2, (152.4, 101.6), (152.4, 96.52), (152.4, 106.68)))
    m = ModuleSheet(parts=parts, signals=[Signal("SIG", "J1.1", "out", "X"), Signal("RET", "1", "in", "X", ref="J2")], no_connect=["J1.2", "J2.2"], power=())
    sch = _sch()
    build_module_sheet(m, sch)
    (ret, sig) = _hier_labels(sch)
    assert (sig[0], ret[0]) == ("SIG", "RET") and sig[1] < 127.0 < ret[1], (sig, ret)  # each on its own connector
    with pytest.raises(ValueError, match=r"pin 1 is on J1 and J2; name the connector \(J1.1\)"):
        build_module_sheet(ModuleSheet(parts=parts, signals=[Signal("SIG", "1", "out", "X")], no_connect=["J1.2", "J2.2"], power=()), _sch())


def test_12_ground_pins_by_number_or_by_any_ground_name():
    """Ground was a pin named GND*: a header's Pin_2 or a VSS pin was refused."""
    from kicad_layer.design import verify
    from kicad_layer.design.module_sheet import is_ground

    _need_symbol("Connector_Generic", "Conn_01x04")
    hdr4 = Part("HDR4", ("Connector_Generic", "Conn_01x04"), "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical", "HDR", "-", "-")
    m = ModuleSheet(parts=(("J1", hdr4, (101.6, 101.6), (101.6, 93.98), (101.6, 111.76)),), signals=[Signal("A", "1", "out", "X"), Signal("B", "3", "in", "X")],
                    no_connect=[], power=(), gnd_pins=("2", "J1.4"))
    sch = _sch()
    build_module_sheet(m, sch)
    assert [pl.symbol.name for pl in sch.placed if pl.symbol.lib_id.startswith("power:")] == ["GND"]
    assert verify.module_groups(m) == {frozenset({("J1", "2"), ("J1", "4")})}
    assert all(is_ground(n) for n in ("GND", "GNDA", "AGND", "PGND", "SIG_GND", "VSS", "VSSA", "0V"))
    assert not any(is_ground(n) for n in ("VBUS", "Pin_2", "+3V3", "SENSE"))


def test_12_signal_table_keys_pins_by_connector():
    from kicad_layer.design.signals import by_sheet, check_table

    table = [Signal("A", "J1.1", "out", "X"), Signal("B", "1", "in", "X", ref="J2"), Signal("C", "", "out", "X", to="Y")]
    check_table(table, {"J1.2": "spare"}, ["J2.2"])
    with pytest.raises(AssertionError, match="J2.1 used twice"):
        check_table(table + [Signal("D", "J2.1", "in", "X")], {}, [])
    with pytest.raises(ValueError):
        Signal("E", "3", "out", "X", to="Y")  # a sheet-to-sheet signal has no module pin
    (c_y,) = by_sheet(table, "Y")
    assert (c_y.sheet, c_y.to, c_y.direction, c_y.consumer_shape) == ("Y", "X", "in", "output")  # Y drives C into X
    assert [s.consumer_shape for s in by_sheet(table, "X")] == ["input", "output", "input"]


def _two_sheet_project(tmp_path, module_sheet=None):
    """Osc drives CLK and shares DATA with Dsp; no module, or a module sheet carrying one more signal."""
    from kicad_layer.design.render import Described
    from kicad_layer.design.signals import by_sheet

    _need_symbol("Connector_Generic", "Conn_01x03")
    _need_symbol("Connector_Generic", "Conn_01x04")
    hdr4 = Part("HDR4", ("Connector_Generic", "Conn_01x04"), "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical", "HDR", "-", "-")
    signals = [Signal("CLK", "", "out", "Dsp", to="Osc"), Signal("DATA", "", "bi", "Osc", to="Dsp")]
    if module_sheet:
        signals.append(Signal("RST", "5", "out", "Dsp"))
    osc = Circuit("Osc", by_sheet(signals, "Osc"))
    j = osc.part("J1", HDR3)
    osc.signal("CLK", j["1"])
    osc.signal("DATA", j["2"])
    osc.gnd(j["3"])
    dsp = Circuit("Dsp", by_sheet(signals, "Dsp"))
    j2 = dsp.part("J2", hdr4)
    dsp.signal("CLK", j2["1"])
    dsp.signal("DATA", j2["2"])
    dsp.gnd(j2["3"])
    if module_sheet:
        dsp.signal("RST", j2["4"])
    else:
        dsp.nc(j2, "4")
    sheets = {"Osc": Described(osc, Layout()), "Dsp": Described(dsp, Layout())}
    project = Project(name="t", dir=tmp_path, title="t", date="2026-09-22", rev="1", company="", signals=signals,
                      root=RootLayout(module_sheet=module_sheet, left_sheets=["Osc"], right_sheets=["Dsp"]),
                      rules=Rules(template=TEMPLATE_PRO, rules={}, classes=[{"name": "Default"}], assignments=[], track_widths=[], via_dimensions=[],
                                  diff_pair_dimensions=[]),
                      sheets=lambda: dict(sheets))
    return project, osc, dsp


def test_12_a_project_without_a_module_sheet(tmp_path):
    """Two consumer sheets and the signals between them, no module: the root joins them by net labels."""
    from kicad_layer.design.root import build_design

    project, osc, dsp = _two_sheet_project(tmp_path)
    assert osc.check() == [] and dsp.check() == []
    root, children = build_design(project)
    assert [s.name for s in root.sheets] == ["Osc", "Dsp"] and set(children) == {"Osc", "Dsp"}
    labels = sorted(str(n[1]) for n in root.items if str(n[0]) == "label")
    assert labels == ["CLK", "CLK", "DATA", "DATA"]
    assert [n for n in root.items if str(n[0]) == "wire"] and not [n for n in root.items if str(n[0]) == "hierarchical_label"]
    assert [(n, sh) for n, _, sh in _hier_labels(children["Osc"])] == [("CLK", "output"), ("DATA", "bidirectional")]
    assert [(n, sh) for n, _, sh in _hier_labels(children["Dsp"])] == [("CLK", "input"), ("DATA", "bidirectional")]
    for sheet in root.sheets:  # every sheet pin has its label, and the files write
        children[sheet.name].write(str(tmp_path / sheet.file))
    root.write(str(tmp_path / "t.kicad_sch"))
    write_project_file(project, tmp_path / "t.kicad_pro", root.sheet_list(children))
    assert [name for _, name in root.sheet_list(children)] == ["Root", "Osc", "Dsp"]


def test_12_sheet_to_sheet_signals_beside_a_module(tmp_path):
    from kicad_layer.design.root import build_root

    project, _, _ = _two_sheet_project(tmp_path, module_sheet="Main")
    root, children = build_root(project)
    main = next(s for s in root.sheets if s.name == "Main")
    assert set(main.pins) == {"RST"} and "Main" in children  # the module carries only its own signal
    assert sorted(str(n[1]) for n in root.items if str(n[0]) == "label") == ["CLK", "CLK", "DATA", "DATA"]
    project.root.module_sheet = None
    with pytest.raises(ValueError, match="RST: on module pin 5, but the root has no module sheet"):
        build_root(project)


def test_13_a_part_can_be_placed_do_not_populate():
    _need_symbol("Device", "R")
    sch = _sch()
    sch.place("Device", "R", "R1", (50.8, 50.8), dnp=True)
    assert str(child(sch.items[-1], "dnp")[1]) == "yes"


def test_13_a_board_text_has_a_layer_and_a_rotation():
    t = board_mod.Text("REV A", (10.0, 10.0), layer="B.SilkS", rot=90)
    assert (t.layer, t.rot) == ("B.SilkS", 90)


# ---------------------------------------------------------------- the design package: board


def test_5_courtyard_bbox_measures_a_circle():
    fp = _need_footprint("MountingHole", "MountingHole_3.2mm_M3")
    x0, y0, x1, y1 = board_mod.courtyard_bbox(fp, (0.0, 0.0), 0.0)
    assert x1 - x0 > 6.0 and y1 - y0 > 6.0, (x0, y0, x1, y1)  # a 3.45 mm radius courtyard, not the one point on its rim


def test_5_load_board_reads_a_circular_courtyard(tmp_path):
    from kicad_layer.pcb_writer import BoardBuilder
    from kicad_layer.review import load_board

    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    fp = _need_footprint("TestPoint", "TestPoint_Pad_D1.5mm")
    pcb = BoardBuilder(sheetfile="t.kicad_sch", title="t", copper_layers=2)
    pcb.rounded_rect_outline(0, 0, 20, 20, 1)
    pcb.footprint(fp, "TP1", "TP", (10.0, 10.0), 0, pad_nets={"1": "N"})
    path = tmp_path / "t.kicad_pcb"
    pcb.write(str(path))
    try:
        (tp,) = [f for f in load_board(path).footprints if f.ref == "TP1"]
    finally:
        set_settings(None)
    assert tp.courtyard is not None


def _hiz_rules(tmp_path: Path) -> copper.Rules:
    pro = tmp_path / "hiz.kicad_pro"
    pro.write_text(json.dumps({"net_settings": {
        "classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3},
                    {"name": "HiZ", "clearance": 0.5, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3}],
        "netclass_patterns": [{"netclass": "HiZ", "pattern": "HIZ*"}]}}), encoding="utf-8")
    pro.with_suffix(".kicad_dru").write_text('(version 1)\n(rule "hiz_no_via"\n  (constraint disallow via)\n  (condition "A.NetClass == \'HiZ\'"))\n',
                                             encoding="utf-8")
    return copper.Rules.load(pro)


def test_16_the_stub_router_keeps_a_disallow_via_rule(tmp_path):
    def pad(ref, x, y, layer):
        return PadGeo(ref=ref, number="1", x=x, y=y, size=(0.8, 0.9), drill=None, net="HIZ1", kind="smd", layers=[layer], shape="roundrect", roundrect_ratio=0.25)

    pads = [pad("R1", 10, 10, "F.Cu"), pad("R2", 20, 12, "B.Cu")]
    fps = [FpGeo(ref=p.ref, lib_id="x", x=p.x, y=p.y, rotation=0.0, layer="F.Cu", courtyard=(p.x - 1, p.y - 1, p.x + 1, p.y + 1), pads=[p]) for p in pads]
    bm = BoardModel(path=Path("memory.kicad_pcb"), copper_layers=2, outline=(0, 0, 40, 40), footprints=fps, segments=[], vias=[], zones=[], texts=[], design_rules={})
    res = stubs.route_stubs(bm, _hiz_rules(tmp_path), [stubs.Open("HIZ1", (10, 10), (20, 12), "F.Cu", "B.Cu", "Pad 1 [R1]", "Pad 1 [R2]")])
    assert not res.routes.vias, [(v.x, v.y) for v in res.routes.vias]


# ---------------------------------------------------------------- the design package: build pipeline


class _Stop(Exception):
    """Raised by a stand-in to end the pipeline at a known step."""


@pytest.fixture
def mini(tmp_path, monkeypatch):
    """A project with a module sheet and one placeholder sheet on one signal, and the pipeline's kicad-cli steps replaced by stand-ins."""
    sheets: dict = {}
    project = Project(name="t", dir=tmp_path / "t", title="t", date="2026-09-22", rev="1", company="", signals=[Signal("S", "1", "out", "Io")],
                      root=RootLayout(module_sheet="Main", left_sheets=["Io"], right_sheets=[]),
                      rules=Rules(template=TEMPLATE_PRO, rules={}, classes=[{"name": "Default"}], assignments=[], track_widths=[],
                                  via_dimensions=[], diff_pair_dimensions=[]),
                      sheets=lambda: dict(sheets))
    project.dir.mkdir()
    monkeypatch.setattr(build, "find_kicad_cli", lambda: SimpleNamespace(version="10.0.6", path="kicad-cli"))
    monkeypatch.setattr(build.reports, "run_erc", lambda *a, **k: SimpleNamespace(verdict="PASS", counts={}, findings=[]))
    monkeypatch.setattr(build.netlist_mod, "export_netlist", lambda *a, **k: (tmp_path / "none.xml", None, None))
    monkeypatch.setattr(build.netlist_mod, "load_netlist", lambda *a, **k: SimpleNamespace(component_count=0, net_count=0))
    yield SimpleNamespace(project=project, sheets=sheets)
    set_settings(None)


def test_3_a_multi_unit_symbol_takes_the_path_of_its_first_unit(mini):
    _need_symbol("Amplifier_Operational", "OPA1678")
    units: dict = {}

    def main_sheet(sch: SchematicBuilder) -> None:
        for unit, x in ((1, 101.6), (2, 152.4)):
            units[unit] = sch.place("Amplifier_Operational", "OPA1678", "U1", (x, 101.6), unit=unit)

    got: dict = {}

    def board_builder(pcb_path, sheetfile, net, symbol_paths, setup_template, **kw):
        got.update(symbol_paths)
        raise _Stop

    mini.sheets["Main"] = main_sheet
    mini.project.board_builder = board_builder
    with pytest.raises(_Stop):
        build.main(mini.project, ["--no-render"])
    assert got["U1"][0].endswith(units[1].uuid), (got["U1"], units[1].uuid, units[2].uuid)


def test_9_the_schematic_is_written_without_kicad_cli(mini, monkeypatch):
    def missing():
        raise LayerError(KICAD_CLI_NOT_FOUND, "no kicad-cli here")

    monkeypatch.setattr(build, "find_kicad_cli", missing)
    try:
        build.main(mini.project, ["--sch-only"])
    except LayerError:
        pass
    assert (mini.project.dir / "t.kicad_sch").is_file() and (mini.project.dir / "Main.kicad_sch").is_file()


def test_10_route_stubs_runs_on_a_board_without_routes_json(mini, monkeypatch):
    out = mini.project.dir

    def board_builder(pcb_path, *a, **k):
        pcb_path.write_text("(kicad_pcb)\n", encoding="utf-8")
        return {}

    def fake_run(cmd, **k):
        (out / "_drc.json").write_text(json.dumps({"unconnected_items": []}), encoding="utf-8")
        return SimpleNamespace(returncode=5)

    def reached(*a, **k):
        raise _Stop

    mini.project.board_builder = board_builder
    monkeypatch.setattr(build.runner, "run", fake_run)
    monkeypatch.setattr(build.reports, "run_drc", lambda *a, **k: SimpleNamespace(verdict="FAIL", counts={"unconnected": 3}, findings=[]))
    monkeypatch.setattr("kicad_layer.review.load_board", lambda *a, **k: None)
    monkeypatch.setattr(stubs, "route_stubs", reached)
    assert not (out / "routing" / "routes.json").exists()
    with pytest.raises(_Stop):
        build.main(mini.project, ["--route-stubs", "--no-render"])


def test_11_out_builds_a_project_without_its_own_libraries(mini, monkeypatch, tmp_path):
    def erc_reached(*a, **k):
        raise _Stop

    monkeypatch.setattr(build.reports, "run_erc", erc_reached)
    assert not (mini.project.dir / "sym-lib-table").exists() and not mini.project.lib_dir.exists()
    with pytest.raises(_Stop):
        build.main(mini.project, ["--out", str(tmp_path / "scratch"), "--sch-only"])
    assert (tmp_path / "scratch" / "t.kicad_sch").is_file()


def test_18_rules_set_drc_severities(tmp_path):
    rules = Rules(template=TEMPLATE_PRO, rules={}, classes=[{"name": "Default"}], assignments=[], track_widths=[], via_dimensions=[],
                  diff_pair_dimensions=[], rule_severities={"silk_overlap": "warning", "silk_over_copper": "warning"})
    project = SimpleNamespace(name="t", rules=rules)
    write_project_file(project, tmp_path / "t.kicad_pro", [])
    sev = json.loads((tmp_path / "t.kicad_pro").read_text(encoding="utf-8"))["board"]["design_settings"]["rule_severities"]
    assert (sev["silk_overlap"], sev["silk_over_copper"]) == ("warning", "warning")


# ---------------------------------------------------------------- the library around it


def test_8_the_api_sheet_shortens_pathlib_on_every_supported_python():
    from kicad_layer.design import api

    def f(p: Path) -> None:
        """."""

    assert api._sig(f) == "(p: Path) -> None"


def test_17_the_dropped_copper_warning_does_not_blame_old_freerouting(tmp_path, monkeypatch):
    from kicad_layer.routers import routing_tools
    from kicad_layer.routes import RouteSegment, Routes

    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    try:
        exp = SimpleNamespace(ignore_classes=["HiZ_excluded"], excluded={"HIZ1": "disallow via"}, warnings=[], class_rules=[], keepouts=[])
        monkeypatch.setattr(routing_tools.dsn_mod, "write_dsn_export", lambda *a, **k: exp)
        monkeypatch.setattr(routing_tools.freerouting, "run", lambda *a, **k: SimpleNamespace(seconds=1.0, returncode=0, log_tail=""))
        monkeypatch.setattr(routing_tools.ses_mod, "parse_ses", lambda *a, **k: Routes(segments=[RouteSegment("HIZ1", "F.Cu", 0.2, 0, 0, 1, 0)], nets={"HIZ1"}))
        monkeypatch.setattr(routing_tools, "_rule_warnings", lambda *a, **k: [])
        rep = routing_tools.autoroute(tmp_path / "b.kicad_pcb", None, routes_in=None, routes_out=tmp_path / "r.json", plane_layers=None,
                                      routable_layers=None, passes=1, timeout_s=10)
        (w,) = [w for w in rep.warnings if "excluded nets anyway" in w]
        assert rep.dropped_segments == 1 and "before 2.4" not in w, w
    finally:
        set_settings(None)
