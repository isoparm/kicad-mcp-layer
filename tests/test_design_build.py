"""The build pipeline without kicad-cli: the offline build, the netlist from the descriptions, the symbol paths and ``--out``."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_layer.config import set_settings
from kicad_layer.design import build, offline
from kicad_layer.design.board import Board, Place, build_board
from kicad_layer.design.circuit import Circuit
from kicad_layer.design.module_sheet import DescribedModule, ModuleSheet
from kicad_layer.design.parts import Part
from kicad_layer.design.project import Project, RootLayout, Rules
from kicad_layer.design.render import At, Described, Layout
from kicad_layer.design.signals import Signal
from kicad_layer.errors import KICAD_CLI_NOT_FOUND, LayerError
from kicad_layer.sexpr import children, parse, value
from tests.conftest import FIXTURES

TEMPLATE_PRO = FIXTURES / "pic_programmer" / "pic_programmer.kicad_pro"
R = Part("R", ("Device", "R"), "Resistor_SMD:R_0805_2012Metric", "10k", "-", "-")
HDR2 = Part("HDR2", ("Connector_Generic", "Conn_01x02"), "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", "HDR", "-", "-")


def _need(lib: str, name: str) -> None:
    from kicad_layer.kicad_libs import load_symbol

    try:
        load_symbol(lib, name)
    except Exception as exc:
        pytest.skip(f"symbol {lib}:{name} not available: {exc}")


def _need_fp(ref: str) -> None:
    from kicad_layer.kicad_libs import load_footprint

    try:
        load_footprint(*ref.split(":", 1))
    except Exception as exc:
        pytest.skip(f"footprint {ref} not available: {exc}")


def _no_cli():
    raise LayerError(KICAD_CLI_NOT_FOUND, "no kicad-cli here")


@pytest.fixture
def project(tmp_path, monkeypatch, request):
    """A module sheet (J1: pin 1 carries S, pin 2 is ground) and a described sheet (R1 from S to ground), with a board; no kicad-cli.
    Indirect parameters: ``dnp`` marks R1 do-not-populate."""
    opts = getattr(request, "param", {})
    _need("Device", "R")
    _need("Connector_Generic", "Conn_01x02")
    _need_fp(R.footprint)
    _need_fp(HDR2.footprint)
    signals = [Signal("S", "1", "out", "Io")]
    module = ModuleSheet(parts=(("J1", HDR2, (101.6, 101.6), (101.6, 96.52), (101.6, 106.68)),), signals=signals, no_connect=[], power=(),
                         gnd_prefix="Pin_2")
    c = Circuit("Io", signals)
    r = c.part("R1", R, dnp=opts.get("dnp", False))
    c.signal("S", r["1"])
    c.gnd(r["2"])
    board = Board(title="t", scope="t", outline=(0, 0, 30, 20), copper_layers=2,
                  placements=(Place("J1", (8.0, 8.0)), Place("R1", (20.0, 10.0))))
    got: dict = {}

    def board_builder(out_path, sheetfile, netlist, symbol_paths, setup_template, with_routes=True):
        got["netlist"] = netlist
        return build_board(board, out_path, sheetfile, netlist, symbol_paths, setup_template, date="", rev="", company="", with_routes=with_routes)

    p = Project(name="t", dir=tmp_path / "t", title="t", date="2026-09-22", rev="1", company="", signals=signals,
                root=RootLayout(module_sheet="Main", left_sheets=["Io"], right_sheets=[]),
                rules=Rules(template=TEMPLATE_PRO, rules={}, classes=[{"name": "Default"}], assignments=[], track_widths=[], via_dimensions=[],
                            diff_pair_dimensions=[]),
                sheets=lambda: {"Main": DescribedModule(module), "Io": Described(c, Layout(parts={"R1": At(101.6, 101.6)}))}, board_builder=board_builder)
    p.dir.mkdir()
    monkeypatch.setattr(build, "find_kicad_cli", _no_cli)
    yield SimpleNamespace(project=p, got=got)
    set_settings(None)


def _pad_nets(pcb: Path) -> dict[tuple[str, str], str]:
    tree = parse(pcb.read_text(encoding="utf-8"))
    out = {}
    for fp in children(tree, "footprint"):
        ref = next(str(p[2]) for p in children(fp, "property") if str(p[1]) == "Reference")
        for pad in children(fp, "pad"):
            n = [x for x in pad if isinstance(x, list) and str(x[0]) == "net"]
            if n:
                out[(ref, str(pad[1]))] = str(n[0][-1])
    return out


def test_without_kicad_cli_the_build_goes_offline_and_writes_the_board(project, capsys):
    assert build.main(project.project, ["--no-render"]) == 0
    d = project.project.dir
    assert all((d / f).is_file() for f in ("t.kicad_sch", "Main.kicad_sch", "Io.kicad_sch", "t.kicad_pro", "t.kicad_pcb"))
    text = capsys.readouterr().out
    assert "WARNING offline build" in text and "ERC: UNVERIFIED" in text and "DRC: UNVERIFIED" in text
    nets = _pad_nets(d / "t.kicad_pcb")
    assert nets[("J1", "1")] == nets[("R1", "1")] == "/S"
    assert nets[("J1", "2")] == nets[("R1", "2")] == "GND"


def test_offline_flag_skips_kicad_cli_even_when_it_is_there(project, monkeypatch, capsys):
    monkeypatch.setattr(build, "find_kicad_cli", lambda: pytest.fail("looked for kicad-cli"))
    assert build.main(project.project, ["--offline", "--sch-only", "--route-stubs"]) == 0
    text = capsys.readouterr().out
    assert "--route-stubs: skipped offline" in text and "schematic only" in text
    assert not (project.project.dir / "t.kicad_pcb").exists()


def test_synth_netlist_reads_the_descriptions(project):
    net, notes = offline.synth_netlist(project.project.sheets())
    assert notes == []
    assert {c.ref: c.footprint for c in net.components} == {"J1": HDR2.footprint, "R1": R.footprint}
    assert {n.name: sorted((x.ref, x.pin) for x in n.nodes) for n in net.nets} == {"/S": [("J1", "1"), ("R1", "1")], "GND": [("J1", "2"), ("R1", "2")]}


def test_synth_netlist_takes_the_parts_of_a_hand_drawn_sheet_without_nets(project):
    from kicad_layer.ids import IdFactory
    from kicad_layer.sch_writer import SchematicBuilder

    sch = SchematicBuilder("t", ids=IdFactory("t"))
    sch.place("Device", "R", "R9", (50.8, 50.8), footprint=R.footprint)
    sch.power("GND", (60.96, 50.8))
    net, notes = offline.synth_netlist({"Hand": lambda s: None}, {"Hand": sch})
    (comp,) = net.components
    assert (comp.ref, comp.footprint, comp.pins) == ("R9", R.footprint, ["1", "2"])
    assert net.nets == [] and "Hand: no description" in notes[0]


def test_synth_netlist_takes_a_placeholder_sheets_parts(project):
    """A sheet the project has no builder for is drawn by the root as a placeholder header; KiCad's netlist would list it."""
    from kicad_layer.design.root import build_design

    p = project.project
    main_only = {"Main": p.sheets()["Main"]}
    p.sheets = lambda: dict(main_only)
    root, children = build_design(p)
    net, notes = offline.synth_netlist(p.sheets(), children, root=root)
    assert "J_IO" in {c.ref for c in net.components} and any("Io: no builder (a placeholder)" in n for n in notes), notes


@pytest.mark.parametrize("project", [{"dnp": True}], indirect=True)
def test_offline_a_dnp_part_reaches_the_board_as_a_dnp_footprint(project):
    assert build.main(project.project, ["--offline", "--no-render"]) == 0
    (r1,) = [c for c in project.got["netlist"].components if c.ref == "R1"]
    assert "dnp" in r1.properties
    tree = parse((project.project.dir / "t.kicad_pcb").read_text(encoding="utf-8"))
    attrs = {}
    for fp in children(tree, "footprint"):
        ref = next(str(p[2]) for p in children(fp, "property") if str(p[1]) == "Reference")
        attrs[ref] = [str(a) for n in children(fp, "attr") for a in n[1:]]
    assert "dnp" in attrs["R1"] and "dnp" not in attrs["J1"], attrs


def _nets(net) -> dict[str, list[tuple[str, str]]]:
    return {n.name: sorted((x.ref, x.pin) for x in n.nodes) for n in net.nets}


def test_synth_netlist_keys_module_pins_by_connector_and_joins_sheet_to_sheet_signals():
    """Two module connectors both have a pin 3, each on its own signal ("J1.3" and ref="J2"); CLK runs between two consumer
    sheets (to=) without a module pin; ground pins by gnd_pins, as build_module_sheet resolves them."""
    from kicad_layer.design.signals import by_sheet

    _need("Connector_Generic", "Conn_01x03")
    hdr3 = Part("HDR3", ("Connector_Generic", "Conn_01x03"), "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical", "HDR", "-", "-")
    signals = [Signal("A", "J1.3", "out", "X"), Signal("B", "3", "in", "X", ref="J2"), Signal("CLK", "", "out", "X", to="Y")]
    module = ModuleSheet(parts=(("J1", hdr3, (101.6, 101.6), (101.6, 93.98), (101.6, 109.22)), ("J2", hdr3, (152.4, 101.6), (152.4, 93.98), (152.4, 109.22))),
                         signals=signals, no_connect=["J1.1", "J2.1"], power=(), gnd_pins=("J1.2", "J2.2"))
    x = Circuit("X", by_sheet(signals, "X"))
    r1, r2 = x.part("R1", R), x.part("R2", R)
    x.signal("A", r1["1"])
    x.signal("B", r1["2"])
    x.signal("CLK", r2["1"])
    x.gnd(r2["2"])
    y = Circuit("Y", by_sheet(signals, "Y"))
    r3 = y.part("R3", R)
    y.signal("CLK", r3["1"])
    y.gnd(r3["2"])
    assert x.check() == [] and y.check() == []
    net, notes = offline.synth_netlist({"Main": DescribedModule(module), "X": Described(x, Layout()), "Y": Described(y, Layout())})
    assert notes == []
    assert _nets(net) == {"/A": [("J1", "3"), ("R1", "1")], "/B": [("J2", "3"), ("R1", "2")], "/CLK": [("R2", "1"), ("R3", "1")],
                          "GND": [("J1", "2"), ("J2", "2"), ("R2", "2"), ("R3", "2")]}


def test_offline_a_dual_opamp_footprint_takes_the_lowest_unit_symbol_path(tmp_path, monkeypatch):
    """U1's three units are drawn with unit 1 not last (the layout names C first); symbol_paths and the board footprint keep unit 1's path."""
    _need("Amplifier_Operational", "OPA1678")
    _need("Connector_Generic", "Conn_01x02")
    opamp = Part("OPAMP", ("Amplifier_Operational", "OPA1678"), "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "OPA1678", "Texas Instruments", "OPA1678IDR")
    _need_fp(opamp.footprint)
    _need_fp(HDR2.footprint)
    signals = [Signal("S", "1", "out", "Amp")]
    module = ModuleSheet(parts=(("J1", HDR2, (101.6, 101.6), (101.6, 96.52), (101.6, 106.68)),), signals=signals, no_connect=[], power=(), gnd_pins=("2",))
    c = Circuit("Amp", signals)
    u = c.part("U1", opamp)
    c.signal("S", u["3"])
    c.net(u["1"], u["2"], name="A_FB")
    c.gnd(u["5"])
    c.net(u["6"], u["7"], name="B_FB")
    c.rail("+12V", u["8"])
    c.rail("-12V", u["4"])
    layout = Layout(parts={"U1/C": At(165.1, 76.2), "U1/B": At(101.6, 101.6), "U1": At(101.6, 63.5)})
    board = Board(title="t", scope="t", outline=(0, 0, 30, 20), copper_layers=2, placements=(Place("J1", (6.0, 10.0)), Place("U1", (20.0, 10.0))))
    got: dict = {}

    def board_builder(out_path, sheetfile, netlist, symbol_paths, setup_template, with_routes=True):
        got["paths"] = symbol_paths
        return build_board(board, out_path, sheetfile, netlist, symbol_paths, setup_template, date="", rev="", company="", with_routes=with_routes)

    p = Project(name="t", dir=tmp_path / "t", title="t", date="2026-09-22", rev="1", company="", signals=signals,
                root=RootLayout(module_sheet="Main", left_sheets=["Amp"], right_sheets=[]), rules=Rules(template=None, rules={}, classes=[{"name": "Default"}],
                assignments=[], track_widths=[], via_dimensions=[], diff_pair_dimensions=[]),
                sheets=lambda: {"Main": DescribedModule(module), "Amp": Described(c, layout)}, board_builder=board_builder)
    p.dir.mkdir()
    monkeypatch.setattr(build, "find_kicad_cli", _no_cli)
    try:
        assert build.main(p, ["--offline", "--no-render"]) == 0
    finally:
        set_settings(None)
    sheet = parse((p.dir / "Amp.kicad_sch").read_text(encoding="utf-8"))
    units = {}
    for node in children(sheet, "symbol"):
        props = {str(q[1]): str(q[2]) for q in children(node, "property")}
        if props.get("Reference") == "U1":
            units[int(value(node, "unit"))] = str(value(node, "uuid"))
    assert sorted(units) == [1, 2, 3] and list(units)[-1] != 1, units  # unit 1 is not the last placed: a last-wins map would miss it
    path, sheetname, sheetfile = got["paths"]["U1"]
    assert path.endswith(f"/{units[1]}") and (sheetname, sheetfile) == ("/Amp/", "Amp.kicad_sch")
    pcb = parse((p.dir / "t.kicad_pcb").read_text(encoding="utf-8"))
    (fp,) = [f for f in children(pcb, "footprint") if any(str(q[1]) == "Reference" and str(q[2]) == "U1" for q in children(f, "property"))]
    assert str(value(fp, "path")) == path


def test_a_multi_unit_symbol_takes_its_lowest_unit_whatever_the_order():
    _need("Amplifier_Operational", "OPA1678")
    from kicad_layer.ids import IdFactory
    from kicad_layer.sch_writer import SchematicBuilder

    root = SchematicBuilder("t", ids=IdFactory("t"))
    sheet = root.sheet("Main", "Main.kicad_sch", (50.8, 50.8), (25.4, 25.4))
    cb = root.child(sheet)
    placed = {u: cb.place("Amplifier_Operational", "OPA1678", "U1", (50.8 * u, 101.6), unit=u) for u in (3, 2, 1)}
    paths = build.symbol_paths(root, {"Main": cb}, "t.kicad_sch")
    assert paths["U1"] == (f"{cb.path}/{placed[1].uuid}", "/Main/", "Main.kicad_sch")


def test_out_copies_the_project_libraries_it_has(project, tmp_path, monkeypatch):
    d = project.project.dir
    (d / "sym-lib-table").write_text("(sym_lib_table)\n", encoding="utf-8")
    (d / "lib").mkdir()
    (d / "lib" / "x.kicad_sym").write_text("(kicad_symbol_lib)\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    assert build.main(project.project, ["--out", str(scratch), "--sch-only", "--offline"]) == 0
    assert (scratch / "sym-lib-table").is_file() and (scratch / "lib" / "x.kicad_sym").is_file()
    assert not (scratch / "fp-lib-table").exists()
    assert value(parse((scratch / "t.kicad_sch").read_text(encoding="utf-8")), "version")
