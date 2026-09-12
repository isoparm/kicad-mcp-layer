"""kicad_layer.design: parts with identity, circuits as descriptions, sheets drawn by rule."""
from __future__ import annotations

from collections import Counter

import pytest

from kicad_layer.design import catalog
from kicad_layer.design.circuit import Circuit
from kicad_layer.design.parts import Quantity
from kicad_layer.design.render import At, Layout, render
from kicad_layer.design.signals import Signal, by_sheet, check_table
from kicad_layer.ids import IdFactory
from kicad_layer.sch_writer import SchematicBuilder
from kicad_layer.sexpr import dumps

SIGNALS = [Signal("CLK", "1", "out", "Test"), Signal("DATA", "2", "in", "Test"), Signal("OTHER", "3", "out", "Elsewhere")]


@pytest.mark.parametrize("text,unit_hint,value,unit,tol", [
    ("100nF", "", 100e-9, "F", None), ("2k2", "Ohm", 2200.0, "Ohm", None), ("0R", "Ohm", 0.0, "Ohm", None),
    ("22uF 10V", "", 22e-6, "F", None), ("1kohm +/- 5%", "", 1000.0, "Ohm", 0.05), ("MAX98357A", "", None, "", None),
])
def test_quantity_parses_and_keeps_the_text(text, unit_hint, value, unit, tol):
    q = Quantity.parse(text, unit_hint=unit_hint)
    assert str(q) == text
    assert (q.value is None) == (value is None)
    if value is not None:
        assert q.value == pytest.approx(value) and q.unit == unit and q.tolerance == tol


def test_catalog_is_consistent_without_any_library():
    assert all(name == p.id for name, p in catalog.ALL.items())
    dup = [k for k, n in Counter((p.mpn, p.footprint) for p in catalog.ALL.values()).items() if n > 1]
    assert not dup
    assert all(p.manufacturer and p.mpn for p in catalog.ALL.values())
    for p in catalog.ALL.values():
        if p.symbol[1] in ("R", "C", "C_Polarized", "L"):
            assert p.quantity.value is not None, p.id


def test_signal_table_checks():
    check_table(SIGNALS, {"4": "spare"}, ["5"])
    assert [s.name for s in by_sheet(SIGNALS, "Test")] == ["CLK", "DATA"]
    with pytest.raises(AssertionError):
        check_table(SIGNALS, {"1": "clash with CLK"}, [])


def _circuit() -> Circuit:
    """A connector as the anchor; a series resistor and a capacitor hang from its second pin's node."""
    c = Circuit("Test", by_sheet(SIGNALS, "Test"))
    j, r, cap = c.part("J1", catalog.FAN), c.part("R2", catalog.R_1K), c.part("C1", catalog.C_100N)
    c.signal("CLK", j["1"])
    c.signal("DATA", r["1"])
    c.net(j["2"], r["2"], cap["1"])
    c.gnd(j["3"], cap["2"])
    c.nc(j, "4")
    return c


def test_circuit_checks_cover_every_pin_and_the_sheet_signals():
    assert _circuit().check() == []
    c = Circuit("Test", by_sheet(SIGNALS, "Test"))
    r = c.part("R1", catalog.R_10K)
    c.signal("BOGUS", r["1"])
    problems = c.check()
    assert any("R1.2: not connected" in p for p in problems)
    assert any("BOGUS: not a signal of the Test sheet" in p for p in problems)
    assert any("CLK: the Test sheet must carry it" in p for p in problems)


def test_render_refuses_a_circuit_that_does_not_check():
    c = Circuit("Test", by_sheet(SIGNALS, "Test"))
    c.part("J1", catalog.FAN)
    with pytest.raises(ValueError):
        render(c, Layout(parts={"J1": At(50.8, 50.8)}), SchematicBuilder("t", ids=IdFactory("t")))


def test_render_is_deterministic_and_draws_every_part():
    def draw() -> str:
        sch = SchematicBuilder("t", ids=IdFactory("t"))
        render(_circuit(), Layout(parts={"J1": At(76.2, 63.5)}), sch)
        return dumps(sch.build())
    a = draw()
    assert a == draw()
    for ref in ("J1", "R2", "C1"):
        assert f'"{ref}"' in a
    assert a.count("hierarchical_label") == 2 and '"CLK"' in a and '"DATA"' in a and "no_connect" in a
