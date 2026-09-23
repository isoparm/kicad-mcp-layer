"""The checks that run before KiCad does: the geometry lint, the flow layout, and the drawing read back against its circuit."""
from __future__ import annotations

import pytest

from kicad_layer.cli.discovery import find_kicad_cli
from kicad_layer.design import catalog, verify
from kicad_layer.design.circuit import Circuit
from kicad_layer.design.lint import lint
from kicad_layer.design.parts import place_part
from kicad_layer.design.render import Layout, render
from kicad_layer.ids import IdFactory
from kicad_layer.sch_writer import SchematicBuilder


def _has_cli() -> bool:
    try:
        find_kicad_cli()
        return True
    except Exception:
        return False


requires_cli = pytest.mark.skipif(not _has_cli(), reason="kicad-cli is not installed")


def sheet(name: str = "Test") -> SchematicBuilder:
    return SchematicBuilder(name, ids=IdFactory(scope=name), paper="A4", title=name, date="", rev="", company="")


def fan_circuit() -> Circuit:
    c = Circuit("Test", [])
    j, r, cap = c.part("J1", catalog.FAN), c.part("R1", catalog.R_1K), c.part("C1", catalog.C_100N)
    c.rail("+5V", j["1"], cap["1"], r["1"])
    c.gnd(j["2"], cap["2"])
    c.net(j["3"], r["2"], name="FAN_PWM")
    c.nc(j, "4")
    return c


def test_flow_layout_places_what_the_layout_does_not():
    sch = sheet()
    render(fan_circuit(), Layout(), sch)
    assert {p.ref for p in sch.placed if not p.ref.startswith("#")} == {"J1", "R1", "C1"}
    assert not Layout().placed  # a plain sheet: the build skips its geometry lint, and it is clean anyway
    assert lint(sch) == ([], [])


def test_layout_without_flow_demands_a_place():
    with pytest.raises(ValueError, match="J1"):
        render(fan_circuit(), Layout(flow=None), sheet())


def test_lint_rejects_two_net_names_at_one_point():
    sch = sheet()
    sch.label("A", (10.16, 10.16))
    sch.power("GND", (10.16, 10.16))
    errors, _ = lint(sch)
    assert errors == ["A and GND meet at (10.16, 10.16)"]


def test_lint_rejects_two_net_names_joined_by_wires():
    sch = sheet()
    sch.power("+12V", (25.4, 25.4))
    sch.wire((25.4, 25.4), (25.4, 30.48))
    sch.label("SIG", (25.4, 27.94))  # a label on the wire's run joins it too
    sch.power("GND", (25.4, 30.48))
    errors, _ = lint(sch)
    assert errors == ["+12V and GND and SIG are joined by wires (at (25.4, 25.4)): a short between two named nets"]
    sch = sheet()
    sch.power("+12V", (25.4, 25.4))
    sch.wire((25.4, 25.4), (25.4, 30.48))
    sch.wire((22.86, 27.94), (27.94, 27.94))  # crossing without a junction: not joined
    sch.power("GND", (22.86, 27.94))
    assert lint(sch)[0] == []


def test_lint_rejects_a_pin_on_a_wire_without_a_junction():
    sch = sheet()
    r = place_part(sch, "R1", catalog.R_1K, (25.4, 25.4))
    x, y = r.pin("1")
    sch.wire((x - 5.08, y), (x + 5.08, y))
    errors, _ = lint(sch)
    assert len(errors) == 1 and errors[0].startswith("R1 pin 1 meets a wire")
    sch.junction((x, y))
    assert lint(sch)[0] == []


def test_lint_warns_about_label_text_over_a_body():
    sch = sheet()
    r = place_part(sch, "R1", catalog.R_1K, (25.4, 25.4))
    sch.label("A_LONG_LABEL_NAME", (r.at[0] - 12.7, r.at[1]))
    errors, warnings = lint(sch)
    assert errors == []
    assert warnings and "runs over R1" in warnings[0]


@requires_cli
def test_a_drawing_reads_back_as_its_circuit(tmp_path):
    assert verify.check_sheet(fan_circuit(), Layout(), tmp_path) == []
