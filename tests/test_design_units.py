"""The renderer on multi-unit symbols, pinless parts, power flags, circuit notes and do-not-populate parts.

Drawings are read back into pin groups with KiCad's joining rules (``tests/sheet_readback.py``), so these run
without kicad-cli; ``test_design_checks.py`` has the kicad-cli read-back.
"""
from __future__ import annotations

import pytest

from kicad_layer.design import catalog
from kicad_layer.design.circuit import Circuit
from kicad_layer.design.lint import lint
from kicad_layer.design.parts import Part
from kicad_layer.design.render import At, Decouple, Layout, render, split_key
from kicad_layer.design.signals import Signal
from kicad_layer.design.verify import compare
from kicad_layer.ids import IdFactory
from kicad_layer.sch_writer import SchematicBuilder
from kicad_layer.sexpr import child
from tests.sheet_readback import pin_groups

OPAMP = Part("OPAMP", ("Amplifier_Operational", "OPA1678"), "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "OPA1678", "Texas Instruments", "OPA1678IDR")
HOLE = Part("HOLE", ("Mechanical", "MountingHole"), "MountingHole:MountingHole_3.2mm_M3", "M3", "-", "-")
HDR5 = Part("HDR5", ("Connector_Generic", "Conn_01x05"), "Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical", "HDR", "-", "-")
SIGNALS = [Signal(n, str(i), d, "Amp") for i, (n, d) in enumerate((("IN_A", "in"), ("OUT_A", "out"), ("IN_B", "in"), ("OUT_B", "out")), 1)]


def _need(lib: str, name: str) -> None:
    from kicad_layer.kicad_libs import load_symbol

    try:
        load_symbol(lib, name)
    except Exception as exc:  # the library is not installed here
        pytest.skip(f"symbol {lib}:{name} not available: {exc}")


def _sch() -> SchematicBuilder:
    return SchematicBuilder("t", ids=IdFactory("t"))


def amp(dnp: bool = False) -> Circuit:
    """A dual opamp: A a buffer, B an inverting stage, the power unit decoupled on both rails."""
    _need("Amplifier_Operational", "OPA1678")
    c = Circuit("Amp", SIGNALS)
    u = c.part("U1", OPAMP, dnp=dnp)
    r1, r2 = c.part("R1", catalog.R_10K, dnp=dnp), c.part("R2", catalog.R_10K)
    c1, c2 = c.part("C1", catalog.C_100N), c.part("C2", catalog.C_100N)
    c.signal("IN_A", u["3"])
    c.signal("OUT_A", u["1"], u["2"])
    c.gnd(u["5"])
    c.net(u["6"], r1["2"], r2["1"], name="B_SUM")
    c.signal("IN_B", r1["1"])
    c.signal("OUT_B", u["7"], r2["2"])
    c.rail("+12V", u["8"], c1["1"])
    c.rail("-12V", u["4"], c2["1"])
    c.gnd(c1["2"], c2["2"])
    return c


def _groups(c: Circuit) -> set[frozenset[tuple[str, str]]]:
    return {g for n in c.nets if len(g := frozenset((p.ref, p.number) for p in n.pins)) > 1}


def _check(c: Circuit, layout: Layout) -> SchematicBuilder:
    sch = _sch()
    render(c, layout, sch)
    got, _ = pin_groups(sch)
    assert compare(_groups(c), got) == []
    assert lint(sch)[0] == []
    return sch


PLACED = {"U1": At(76.2, 63.5), "U1/B": At(76.2, 101.6), "U1/C": At(152.4, 76.2)}


def test_units_of_a_part_come_from_its_symbol():
    c = amp()
    assert c.parts["U1"].units == [1, 2, 3] and c.parts["R1"].units == [1]
    assert {p.number for p in c.parts["U1"].unit_pins(2)} == {"5", "6", "7"}
    assert split_key("U1") == ("U1", 1) and split_key("U1/2") == ("U1", 2) and split_key("U1/C") == ("U1", 3)


def test_the_flow_places_every_unit_and_each_pin_on_its_own():
    sch = _check(amp(), Layout(attach_to={"R2": ("U1", "7")}))
    units = {pl.unit: pl for pl in sch.placed if pl.ref == "U1"}
    assert sorted(units) == [1, 2, 3]
    assert len({pl.at for pl in units.values()}) == 3  # three places, not three symbols on one spot


def test_a_layout_places_units_by_key_and_decouples_each_rail_its_own_way():
    layout = Layout(parts=dict(PLACED), flow=None, attach_to={"R2": ("U1", "7")},
                    decouple={("U1", "+12V"): Decouple(side="right"), ("U1", "-12V"): Decouple(side="left")})
    sch = _check(amp(), layout)
    at = {(pl.ref, pl.unit): pl.at for pl in sch.placed}
    assert at[("U1", 1)] == (76.2, 63.5) and at[("U1", 2)] == (76.2, 101.6) and at[("U1", 3)] == (152.4, 76.2)
    assert at[("C1", 1)][0] > 152.4 > at[("C2", 1)][0]  # +12V's capacitor to the right, -12V's to the left


def test_the_flow_places_the_units_the_layout_leaves_out():
    sch = _check(amp(), Layout(parts={"U1/B": At(76.2, 101.6)}, attach_to={"R2": ("U1", "7")}))
    assert sorted(pl.unit for pl in sch.placed if pl.ref == "U1") == [1, 2, 3]


def test_a_unit_without_a_place_or_a_missing_unit_is_refused():
    with pytest.raises(ValueError, match="U1/2"):
        render(amp(), Layout(parts={"U1": At(76.2, 63.5), "U1/3": At(152.4, 76.2)}, flow=None), _sch())
    with pytest.raises(ValueError, match="no unit 4"):
        render(amp(), Layout(parts={"U1/D": At(76.2, 63.5)}), _sch())


def test_a_flag_on_the_middle_pin_of_a_connector_column_touches_neither_neighbour():
    _need("Connector_Generic", "Conn_01x05")
    c = Circuit("T", [])
    j = c.part("J1", HDR5)
    c.gnd(j["1"], j["3"], j["5"])
    c.rail("+5V", j["2"])
    c.rail("+12V", j["4"])
    c.flag("+5V", "+12V")
    sch = _sch()
    render(c, Layout(parts={"J1": At(50.8, 50.8)}), sch)
    _, named = pin_groups(sch)
    assert named["+5V"] == {("J1", "2")} and named["+12V"] == {("J1", "4")}
    assert sum(pl.symbol.name == "PWR_FLAG" for pl in sch.placed) == 2
    assert lint(sch)[0] == []


def test_pinless_parts_share_the_flow_with_the_rest():
    _need("Mechanical", "MountingHole")
    c = amp()
    for i in (1, 2):
        c.part(f"H{i}", HOLE)
    sch = _check(c, Layout(attach_to={"R2": ("U1", "7")}))
    holes = [pl.at for pl in sch.placed if pl.ref.startswith("H")]
    assert len(set(holes)) == 2


def test_circuit_notes_stack_under_the_drawing_or_from_note_at():
    c = amp()
    c.note("FIRST", size=1.5)
    c.note("SECOND", size=1.5)
    sch = _sch()
    render(c, Layout(attach_to={"R2": ("U1", "7")}), sch)
    texts = {str(n[1]): (float(child(n, "at")[1]), float(child(n, "at")[2])) for n in sch.items if str(n[0]) == "text"}
    lowest = max(pl.at[1] for pl in sch.placed)
    assert texts["FIRST"][1] > lowest and texts["SECOND"][1] > texts["FIRST"][1]
    sch = _sch()
    render(amp(), Layout(attach_to={"R2": ("U1", "7")}, note_at=(25.4, 20.32)), sch)  # no notes: nothing drawn
    assert not [n for n in sch.items if str(n[0]) == "text"]
    c = amp()
    c.note("HERE")
    sch = _sch()
    render(c, Layout(attach_to={"R2": ("U1", "7")}, note_at=(25.4, 20.32)), sch)
    assert [(float(child(n, "at")[1]), float(child(n, "at")[2])) for n in sch.items if str(n[0]) == "text"] == [(25.4, 20.32)]


def test_a_do_not_populate_part_is_marked_on_every_unit():
    sch = _sch()
    render(amp(dnp=True), Layout(attach_to={"R2": ("U1", "7")}), sch)
    dnp = {}
    for n in sch.items:
        if str(n[0]) == "symbol":
            ref = next(str(p[2]) for p in n if isinstance(p, list) and str(p[0]) == "property" and str(p[1]) == "Reference")
            dnp.setdefault(ref, set()).add(str(child(n, "dnp")[1]))
    assert dnp["U1"] == {"yes"} and dnp["R1"] == {"yes"}  # R1 is drawn by a rule, not as an anchor
    assert dnp["R2"] == {"no"} and all(v == {"no"} for k, v in dnp.items() if k.startswith("#"))


def test_each_sheet_can_have_its_own_paper(tmp_path):
    from kicad_layer.design.project import Project, RootLayout, Rules
    from kicad_layer.design.root import build_root

    _need("Connector_Generic", "Conn_01x01")

    def project(papers: dict) -> Project:
        return Project(name="t", dir=tmp_path, title="t", date="", rev="", company="", signals=[Signal("S", "1", "out", "Io"), Signal("T", "2", "out", "Aux")],
                       root=RootLayout(module_sheet="Main", left_sheets=["Io"], right_sheets=["Aux"], papers=papers),
                       rules=Rules(template=tmp_path, rules={}, classes=[], assignments=[], track_widths=[], via_dimensions=[], diff_pair_dimensions=[]),
                       sheets=lambda: {"Io": lambda sch: None})

    root, children = build_root(project({"Io": "A2", "Aux": "A3"}))
    assert (root.paper, children["Main"].paper, children["Io"].paper, children["Aux"].paper) == ("A3", "A3", "A2", "A3")
    _, children = build_root(project({}))
    assert (children["Io"].paper, children["Aux"].paper) == ("A3", "A4")  # a placeholder keeps A4
    with pytest.raises(ValueError, match="Nope"):
        build_root(project({"Nope": "A4"}))
