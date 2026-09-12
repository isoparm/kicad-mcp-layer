"""The lossless schematic editor, on copies of the fixture corpus."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_layer.config import load_settings, set_settings
from kicad_layer.cst import mark_dirty, parse_cst, render_file
from kicad_layer.errors import LayerError
from kicad_layer.sch_edit import Schematic
from tests.conftest import FIXTURES, copy_project, real_kicad

SHEETS = sorted(FIXTURES.rglob("*.kicad_sch"))


@pytest.mark.parametrize("sheet", SHEETS, ids=[s.name for s in SHEETS])
def test_untouched_file_is_byte_identical(sheet):
    text = sheet.read_text(encoding="utf-8")
    root = parse_cst(text)
    assert render_file(root, text) == text


@pytest.mark.parametrize("sheet", SHEETS, ids=[s.name for s in SHEETS])
def test_dirty_root_with_clean_children_is_byte_identical(sheet):
    """Inserting or deleting a top-level item re-renders only the root: that must not disturb the rest."""
    text = sheet.read_text(encoding="utf-8")
    root = parse_cst(text)
    mark_dirty(root)
    assert render_file(root, text) == text


@pytest.fixture
def project(tmp_path):
    """A writable copy of the complex_hierarchy project in a workspace of its own, write mode on."""
    dst = copy_project("complex_hierarchy", tmp_path / "complex_hierarchy")
    cache = tmp_path / "cache"
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_MODE": "write", "KICAD_LAYER_CACHE_DIR": str(cache)}))
    yield dst
    set_settings(None)


def test_set_property_changes_only_that_line(project):
    path = project / "complex_hierarchy.kicad_sch"
    before = path.read_text(encoding="utf-8")
    sch = Schematic(path)
    old, changed = sch.set_property("C101", "Value", "47uF/100V")
    assert changed and old == "47uF/63V"
    saved = sch.save()
    assert saved.changed and saved.snapshot and saved.snapshot.is_file()
    after = path.read_text(encoding="utf-8")
    diff = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert len(before.splitlines()) == len(after.splitlines())
    assert len(diff) == 1 and "47uF/100V" in diff[0][1]


def test_save_refuses_lock_and_conflict(project):
    path = project / "complex_hierarchy.kicad_sch"
    sch = Schematic(path)
    sch.set_property("C101", "Value", "1uF")
    lock = project / "~complex_hierarchy.kicad_sch.lck"
    lock.write_text('{"hostname":"x","username":"y"}')
    with pytest.raises(LayerError) as exc:
        sch.save()
    assert exc.value.code == "SCHEMATIC_LOCKED"
    lock.unlink()
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(LayerError) as exc:
        sch.save()
    assert exc.value.code == "EDIT_CONFLICT"


def test_add_symbol_embeds_cache_and_reports_pins(project):
    path = project / "complex_hierarchy.kicad_sch"
    sch = Schematic(path)
    assert sch.lib_symbol("Device:R") is None
    u, pins = sch.add_symbol("Device:R", "R900", (200.66, 100.33))
    assert sch.lib_symbol("Device:R") is not None
    assert set(pins) == {"1", "2"}
    assert pins["1"][:2] == (200.66, 96.52) and pins["2"][:2] == (200.66, 104.14)
    sch.save()
    again = Schematic(path)
    v = again.find("R900")
    assert v.lib_id == "Device:R" and v.at == (200.66, 100.33)
    assert again._instance_reference(v.node) == "R900"


def test_delete_and_annotate(project):
    path = project / "complex_hierarchy.kicad_sch"
    sch = Schematic(path)
    u, _ = sch.add_symbol("Device:C", "C?", (210.82, 100.33))
    changes = sch.annotate()
    assert changes == {"C?": "C106"} or list(changes.values())[0].startswith("C")
    new_ref = list(changes.values())[0]
    sch.save()
    sch = Schematic(path)
    assert sch.find(new_ref).uuid == u
    assert sch.delete(u) == "symbol"
    sch.save()
    with pytest.raises(LayerError):
        Schematic(path).find(new_ref)


@real_kicad
@pytest.mark.anyio
@pytest.mark.slow
async def test_edit_through_mcp_with_erc_and_netlist_delta(project):
    from mcp import Client

    from kicad_layer.server import build_server

    sheet = "complex_hierarchy/complex_hierarchy.kicad_sch"
    async with Client(build_server(tier="full"), raise_exceptions=True) as c:
        listed = (await c.call_tool("sch_list_components", {"schematic_path": sheet})).structured_content
        refs = {x["ref"] for x in listed["components"]}
        assert "C101" in refs and not any(r.startswith("#") for r in refs)

        placed = (await c.call_tool("sch_add_component", {"schematic_path": sheet, "lib_id": "Device:R", "ref": "R901", "x_mm": 200.66, "y_mm": 100.33})).structured_content
        assert placed["changed"] and placed["erc"]["verdict"] in ("PASS", "WARN", "FAIL")
        pins = {p["number"]: (p["x_mm"], p["y_mm"]) for p in placed["pins"]}
        assert placed["netlist_delta"]["components_added"] == ["R901"]

        top = pins["1"]
        bottom = pins["2"]
        w1 = (await c.call_tool("sch_wire", {"schematic_path": sheet, "points": [[top[0], top[1]], [top[0], top[1] - 2.54]]})).structured_content
        assert w1["changed"]
        l1 = (await c.call_tool("sch_label", {"schematic_path": sheet, "text": "TEST_A", "x_mm": top[0], "y_mm": top[1] - 2.54})).structured_content
        assert any(n.endswith("TEST_A") for n in l1["netlist_delta"]["nets_added"])
        w2 = (await c.call_tool("sch_wire", {"schematic_path": sheet, "points": [[bottom[0], bottom[1]], [bottom[0], bottom[1] + 2.54]]})).structured_content
        l2 = (await c.call_tool("sch_label", {"schematic_path": sheet, "text": "TEST_B", "x_mm": bottom[0], "y_mm": bottom[1] + 2.54})).structured_content
        assert any(n.endswith("TEST_B") for n in l2["netlist_delta"]["nets_added"])
        # A resistor hanging off two labels is electrically odd, and KiCad says so: both nets have a
        # single pin. No errors may remain, and the only warnings must be exactly that one.
        assert l2["erc"]["counts"]["errors"] == 0, l2["erc"]["findings"][:3]
        assert {f["type"] for f in l2["erc"]["findings"]} <= {"isolated_pin_label"}
        assert l2["erc"]["verdict"] in ("PASS", "WARN")

        dry = (await c.call_tool("sch_set_property", {"schematic_path": sheet, "ref": "R901", "name": "Value", "value": "4k7", "dry_run": True})).structured_content
        assert dry["dry_run"] and dry["changed"]
        got = (await c.call_tool("sch_get_symbol", {"schematic_path": sheet, "ref": "R901"})).structured_content
        assert got["value"] == "R" and len(got["pins"]) == 2

        gone = (await c.call_tool("sch_delete", {"schematic_path": sheet, "uuid": placed["uuids"][0]})).structured_content
        assert gone["netlist_delta"]["components_removed"] == ["R901"]


@pytest.mark.anyio
async def test_writes_refuse_in_readonly_mode(tmp_path):
    from mcp import Client

    from kicad_layer.server import build_server

    dst = copy_project("complex_hierarchy", tmp_path / "complex_hierarchy")
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    try:
        async with Client(build_server(tier="full"), raise_exceptions=True) as c:
            r = await c.call_tool("sch_set_property", {"schematic_path": "complex_hierarchy/complex_hierarchy.kicad_sch", "ref": "C101", "name": "Value", "value": "x"})
            assert r.is_error and "READ_ONLY_MODE" in r.content[0].text
    finally:
        set_settings(None)
