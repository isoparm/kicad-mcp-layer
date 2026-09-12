"""Stable identifiers: same input, same files."""
from __future__ import annotations

from kicad_layer.ids import IdFactory
from kicad_layer.pcb_writer import BoardBuilder
from kicad_layer.sch_writer import SchematicBuilder
from kicad_layer.sexpr import dumps as render


def test_same_key_same_id_and_duplicates_are_numbered():
    a, b = IdFactory("x"), IdFactory("x")
    assert a.make("wire", 1.0, 2.0) == b.make("wire", 1.0, 2.0)
    assert a.make("wire", 1.0, 2.0) != a.make("wire", 1.0, 2.0)  # a second identical wire in one document
    assert IdFactory("y").make("wire", 1.0, 2.0) != b.make("wire", 1.0, 2.0)  # another scope
    assert a.child("sheet", "A.kicad_sch").scope == "x/sheet/A.kicad_sch"


def test_no_scope_means_random():
    f = IdFactory()
    assert f.make("footprint", "R1") != f.make("footprint", "R1")


def _schematic(scope: str | None) -> str:
    root = SchematicBuilder("p", title="t", ids=IdFactory(scope))
    sh = root.sheet("Load", "Load.kicad_sch", (50.8, 50.8), (25.4, 12.7), pins_left=[("IN", "input")])
    root.power("+5V", (40.0, 53.34))
    root.wire((40.0, 53.34), sh.pin("IN"))
    cb = root.child(sh, title="load")
    r = cb.place("Device", "R", "R1", (76.2, 63.5), value_text="10k", footprint="Resistor_SMD:R_0603_1608Metric")
    cb.hier_label("IN", r.pin("1"), shape="input", rot=90)
    cb.power("GND", r.pin("2"))
    return render(root.build()) + render(cb.build())


def _board(scope: str | None) -> str:
    b = BoardBuilder(sheetfile="p.kicad_sch", ids=IdFactory(scope))
    b.rounded_rect_outline(0, 0, 20, 10, 1)
    b.segment((1, 1), (5, 1), width=0.2, layer="F.Cu", net="N")
    b.segment((1, 1), (5, 1), width=0.2, layer="F.Cu", net="N")  # an identical twin must still get its own id
    b.via((5, 1), net="N")
    b.zone(net="GND", layer="B.Cu", polygon=[(0, 0), (20, 0), (20, 10), (0, 10)])
    b.text("hello", (2, 2))
    return render(b.build())


def test_writers_are_deterministic_with_a_scope():
    assert _schematic("s") == _schematic("s")
    assert _board("b") == _board("b")


def test_writers_still_randomise_without_a_scope():
    assert _schematic(None) != _schematic(None)
    assert _board(None) != _board(None)


def test_twin_items_get_distinct_ids():
    text = _board("b")
    ids = [line.strip() for line in text.splitlines() if line.strip().startswith("(uuid")]
    assert len(ids) == len(set(ids)), "duplicate identifiers in one board"
