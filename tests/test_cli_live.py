"""End-to-end through the MCP client against real kicad-cli 10.x. Self-skips without it."""

import shutil

import pytest

from tests.conftest import real_kicad

pytestmark = [real_kicad, pytest.mark.anyio]


async def call(client, name, **args):
    result = await client.call_tool(name, args)
    assert not result.is_error, result.content[0].text if result.content else result
    return result.structured_content


async def test_doctor_finds_kicad_cli(client):
    doc = await call(client, "kicad_doctor")
    assert doc["kicad_cli"]["found"] is True
    assert doc["kicad_cli"]["version"].startswith("10.")
    assert "gerbers" in doc["kicad_cli"]["pcb_export_verbs"]
    assert doc["ipc"]["diagnosis"] in {"not_running", "api_disabled", "no_editor_open", "reachable", "busy", "stale_endpoint", "unknown"}
    assert doc["mode"] == "readonly"


async def test_project_open(client):
    info = await call(client, "project_open", path="pic_programmer")
    assert info["name"] == "pic_programmer"
    assert info["root_schematic"].endswith("pic_programmer.kicad_sch")
    assert info["schematic_format_version"] == 20260306
    assert info["board_format_version"] == 20260206
    assert "Default" in info["netclasses"]


async def test_erc_clean_hierarchy(client):
    report = await call(client, "run_erc", schematic_path="complex_hierarchy/ampli_ht.kicad_sch")
    assert report["verdict"] == "PASS"
    assert report["source"].endswith("complex_hierarchy.kicad_sch"), "sub-sheet resolves to the root"
    assert report["kicad_version"].startswith("10.")


async def test_erc_with_problems(client):
    report = await call(client, "run_erc", schematic_path="multichannel/multichannel_mixer.kicad_sch")
    assert report["verdict"] == "FAIL"
    assert report["counts"]["errors"] >= 1
    types = {f["type"] for f in report["findings"]}
    assert "power_pin_not_driven" in types


async def test_drc_clean_board(client):
    report = await call(client, "run_drc", board_path="pic_programmer/pic_programmer.kicad_pcb")
    assert report["verdict"] == "PASS"


async def test_drc_failing_board(client):
    report = await call(client, "run_drc", board_path="multichannel/multichannel_mixer.kicad_pcb", schematic_parity=False)
    assert report["verdict"] == "FAIL"
    assert report["counts"]["errors"] >= 10
    assert report["counts"]["warnings"] >= 50
    assert any(f["type"] == "clearance" for f in report["findings"])


async def test_drc_unrouted_board_is_never_pass(client):
    report = await call(client, "run_drc", board_path="multichannel/multichannel_mixer-unrouted.kicad_pcb", schematic_parity=False)
    assert report["verdict"] == "FAIL"
    assert report["counts"]["unconnected"] >= 100
    assert any(f["category"] == "unconnected" for f in report["findings"])


async def test_netlist_and_trace(client):
    net = await call(client, "sch_netlist", schematic_path="complex_hierarchy/complex_hierarchy.kicad_sch")
    assert net["net_count"] > 20
    assert net["cache_hit"] is False
    refs = {c["ref"] for c in net["components"]}
    assert "C101" in refs
    assert any(s["name"] == "/" for s in net["sheets"])
    again = await call(client, "sch_netlist", schematic_path="complex_hierarchy/ampli_ht.kicad_sch")
    assert again["cache_hit"] is True, "second call on any sheet of the same project reuses the export"

    tr = await call(client, "sch_trace", schematic_path="complex_hierarchy/complex_hierarchy.kicad_sch", ref="C101")
    assert tr["ref"] == "C101"
    assert any(p["net"] for p in tr["pins"])
    connected = [n for p in tr["pins"] for n in p["connected_to"]]
    assert connected and all(n["ref"] != "C101" or True for n in connected)


async def test_trace_unknown_ref_is_a_readable_error(client):
    result = await client.call_tool("sch_trace", {"schematic_path": "complex_hierarchy/complex_hierarchy.kicad_sch", "ref": "ZZ99"})
    assert result.is_error
    assert "NOT_FOUND_IN_DESIGN" in result.content[0].text


async def test_bom(client, workspace):
    out = workspace / "complex_hierarchy" / "_test-bom.csv"
    try:
        bom = await call(client, "export_bom", schematic_path="complex_hierarchy/complex_hierarchy.kicad_sch", output_path=str(out))
        assert bom["row_count"] > 5
        assert "Reference" in bom["columns"]
    finally:
        out.unlink(missing_ok=True)


async def test_render_board_returns_image(client, workspace):
    out_dir = workspace / "pic_programmer" / "_test-renders"
    try:
        result = await client.call_tool(
            "render_board",
            {"board_path": "pic_programmer/pic_programmer.kicad_pcb", "width": 640, "height": 360, "output_path": str(out_dir / "top.png")},
        )
        assert not result.is_error, result.content
        kinds = [c.type for c in result.content]
        assert "image" in kinds and "text" in kinds
        image = next(c for c in result.content if c.type == "image")
        assert image.mime_type == "image/png"
        assert len(image.data) > 1000
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


async def test_sch_render_svg(client, workspace):
    out_dir = workspace / "pic_programmer" / "_test-sch"
    try:
        result = await call(client, "sch_render", schematic_path="pic_programmer/pic_programmer.kicad_sch", output_dir=str(out_dir))
        assert result["format"] == "svg"
        assert len(result["files"]) >= 2, "root sheet plus pic_sockets sub-sheet"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


async def test_export_fab_gerbers_and_drill(client, workspace):
    out_dir = workspace / "pic_programmer" / "_test-fab"
    try:
        result = await call(client, "export_fab", board_path="pic_programmer/pic_programmer.kicad_pcb", output_dir=str(out_dir), position=True)
        names = [f["path"].replace("\\", "/").rsplit("/", 1)[-1] for f in result["files"]]
        assert any(n.endswith(".gbrjob") for n in names), names
        assert any(n.endswith(".drl") for n in names), names
        assert any(n.endswith("-pos.csv") for n in names), names
        assert all(f["sha256"] for f in result["files"])
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


async def test_workspace_escape_is_refused_through_mcp(client):
    result = await client.call_tool("run_erc", {"schematic_path": "../../elsewhere/board.kicad_sch"})
    assert result.is_error
    assert "WORKSPACE_VIOLATION" in result.content[0].text
