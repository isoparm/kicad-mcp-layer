"""examples/two_layer_basic builds its sheets clean without kicad-cli: circuits check, lint is clean, the drawing reads back
as its circuit, and every part has a place on the board."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from kicad_layer.design.lint import lint
from kicad_layer.design.root import build_design
from kicad_layer.design.verify import compare
from tests.sheet_readback import pin_groups

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "two_layer_basic" / "design.py"


@pytest.fixture(scope="module")
def example():
    from kicad_layer.kicad_libs import load_symbol

    for lib, name in (("Amplifier_Operational", "OPA1678"), ("Connector_Generic", "Conn_01x04"), ("Mechanical", "MountingHole"), ("Device", "R")):
        try:
            load_symbol(lib, name)
        except Exception as exc:  # the library is not installed here
            pytest.skip(f"symbol {lib}:{name} not available: {exc}")
    spec = importlib.util.spec_from_file_location("two_layer_basic_design", EXAMPLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_example_draws_clean_and_reads_back_as_its_circuit(example, tmp_path):
    project = example.make_project(tmp_path)
    project.check_signals()
    root, children = build_design(project)
    assert set(children) == {"Header", "Buffer"} and children["Buffer"].paper == "A3"
    for name, cb in children.items():
        assert lint(cb)[0] == [], name
    c = example.buffer()
    assert c.check() == []
    got, _ = pin_groups(children["Buffer"])
    assert compare({g for n in c.nets if len(g := frozenset((p.ref, p.number) for p in n.pins)) > 1}, got) == []
    assert sorted(pl.unit for pl in children["Buffer"].placed if pl.ref == "U1") == [1, 2, 3]
    placed = {p.ref for p in example.board(tmp_path).placements}
    assert placed == set(c.parts) | {"J1"}


def test_the_example_builds_offline_on_the_package_rules(example, tmp_path, capsys):
    """No fixture behind it: JLCPCB's two-layer set on the package's template, and the board from the offline netlist (C3 DNP)."""
    import json

    from kicad_layer.config import set_settings
    from kicad_layer.design import build
    from kicad_layer.kicad_libs import load_footprint
    from kicad_layer.sexpr import children, parse

    for ref in ("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "Capacitor_SMD:C_1206_3216Metric", "MountingHole:MountingHole_3.2mm_M3",
                "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"):
        try:
            load_footprint(*ref.split(":", 1))
        except Exception as exc:
            pytest.skip(f"footprint {ref} not available: {exc}")
    project = example.make_project(tmp_path / "tlb")
    assert project.rules.template is None and project.setup_template is None
    project.dir.mkdir()
    try:
        assert build.main(project, ["--offline", "--no-render"]) == 0
    finally:
        set_settings(None)
    pro = json.loads((project.dir / "two_layer_basic.kicad_pro").read_text(encoding="utf-8"))
    assert pro["board"]["design_settings"]["rule_severities"]["silk_overlap"] == "warning"
    assert pro["board"]["design_settings"]["rules"]["min_clearance"] == 0.15
    assert [c["name"] for c in pro["net_settings"]["classes"]] == ["Default", "Power"]
    pcb = parse((project.dir / "two_layer_basic.kicad_pcb").read_text(encoding="utf-8"))
    dnp = {next(str(p[2]) for p in children(fp, "property") if str(p[1]) == "Reference") for fp in children(pcb, "footprint")
           if any(str(a) == "dnp" for n in children(fp, "attr") for a in n[1:])}
    assert dnp == {"C3"}
