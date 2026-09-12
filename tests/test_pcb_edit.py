"""Board editing through the file channel, on copies of fixture boards. No KiCad needed."""

from __future__ import annotations

import shutil

import pytest

from kicad_layer.config import load_settings, set_settings
from kicad_layer.cst import mark_dirty, parse_cst, render_file
from kicad_layer.errors import LayerError
from kicad_layer.ipc.session import get_session, reset_session
from kicad_layer.pcb_edit import BoardFile
from kicad_layer.sexpr import child, children
from tests.conftest import FIXTURES, real_kicad

BOARDS = sorted(FIXTURES.rglob("*.kicad_pcb"))


@pytest.mark.parametrize("board", BOARDS, ids=[b.name for b in BOARDS])
def test_board_round_trip_is_byte_identical(board):
    text = board.read_text(encoding="utf-8")
    root = parse_cst(text)
    assert render_file(root, text) == text
    mark_dirty(root)
    assert render_file(root, text) == text


@pytest.fixture
def project(tmp_path):
    src = FIXTURES / "pic_programmer"
    dst = tmp_path / "pic_programmer"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*-backups", ".history", "~*.lck", "*.kicad_prl", "fab", "renders", "_test-*"))
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_MODE": "write", "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    reset_session()
    yield dst
    set_settings(None)
    reset_session()


def test_move_and_rotate_updates_header_and_pad_angles(project):
    path = project / "pic_programmer.kicad_pcb"
    bf = BoardFile(path)
    ref = bf.footprints()[0].reference
    before = bf.find(ref)
    moved = bf.move_footprint(ref, (before.at[0] + 5.0, before.at[1] + 2.5), before.rotation + 90)
    assert moved.at == (round(before.at[0] + 5.0, 4), round(before.at[1] + 2.5, 4))
    assert moved.rotation == (before.rotation + 90) % 360
    for pad in children(moved.node, "pad"):
        at = child(pad, "at")
        assert len(at) > 3, "every pad must now carry the footprint's absolute angle"
    saved = bf.save()
    assert saved.changed and saved.snapshot
    again = BoardFile(path).find(ref)
    assert again.rotation == moved.rotation


def test_file_channel_places_on_the_back(project):
    """The file channel flips a footprint like KiCad does instead of leaving it on F.Cu with a warning."""
    path = project / "pic_programmer.kicad_pcb"
    bf = BoardFile(path)
    fp = bf.place_footprint("Resistor_SMD:R_0603_1608Metric", "R901", (100.0, 60.0), 90, value_text="10k", layer="B.Cu")
    assert fp.layer == "B.Cu" and fp.rotation == 90 and fp.at == (100.0, 60.0)
    bf.save()
    again = BoardFile(path).find("R901")
    assert again.layer == "B.Cu"
    pads = children(again.node, "pad")
    assert len(pads) == 2
    for pad in pads:
        assert {str(a) for a in child(pad, "layers")[1:]} == {"B.Cu", "B.Paste", "B.Mask"}
        assert str(child(pad, "at")[3]) == "90", "a pad with no angle of its own takes the footprint's"


def test_place_track_via_delete(project):
    path = project / "pic_programmer.kicad_pcb"
    bf = BoardFile(path)
    fp = bf.place_footprint("Resistor_SMD:R_0603_1608Metric", "R900", (100.0, 60.0), 0, value_text="10k")
    assert fp.lib_id == "Resistor_SMD:R_0603_1608Metric" and fp.at == (100.0, 60.0)
    assert child(fp.node, "path") is None, "a footprint without a symbol must not claim a schematic path"
    seg = bf.add_segment((100.0, 62.0), (104.0, 62.0), width=0.25, layer="F.Cu", net="GND")
    via = bf.add_via((104.0, 62.0), net="GND")
    bf.save()
    bf = BoardFile(path)
    assert bf.find("R900")
    assert bf.delete(seg) == "segment" and bf.delete(via) == "via"
    bf.save()
    with pytest.raises(LayerError):
        BoardFile(path).find_uuid(seg)


def test_lock_and_seen_live_refusals(project):
    from kicad_layer import pcb_tools

    path = project / "pic_programmer.kicad_pcb"
    rel = "pic_programmer/pic_programmer.kicad_pcb"
    get_session().seen_live.add(pcb_tools._canon(path) if hasattr(pcb_tools, "_canon") else __import__("os").path.normcase(__import__("os").path.realpath(str(path))))
    with pytest.raises(LayerError) as exc:
        pcb_tools.choose_channel(path, "file")
    assert exc.value.code == "EDIT_CONFLICT"
    get_session().seen_live.clear()
    lock = project / "~pic_programmer.kicad_pcb.lck"
    lock.write_text("{}")
    with pytest.raises(LayerError) as exc:
        pcb_tools.choose_channel(path, "file")
    assert exc.value.code == "EDIT_CONFLICT"
    lock.unlink()
    assert pcb_tools.choose_channel(path, "file") == ("file", path)


@real_kicad
@pytest.mark.anyio
async def test_file_channel_through_mcp_passes_drc(project):
    from mcp import Client

    from kicad_layer.server import build_server

    board = "pic_programmer/pic_programmer.kicad_pcb"
    # an empty spot: the grid point inside the outline farthest from every existing footprint
    bf = BoardFile(project / "pic_programmer.kicad_pcb")
    placed_at = [f.at for f in bf.footprints()]
    candidates = [(x, y) for x in range(80, 226, 5) for y in range(46, 135, 5)]
    fx, fy = max(candidates, key=lambda c: min(((c[0] - a) ** 2 + (c[1] - b) ** 2) ** 0.5 for a, b in placed_at))
    assert min(((fx - a) ** 2 + (fy - b) ** 2) ** 0.5 for a, b in placed_at) > 5
    async with Client(build_server(tier="full"), raise_exceptions=True) as c:
        placed = (await c.call_tool("pcb_place_footprint", {"board_path": board, "lib_id": "Resistor_SMD:R_0603_1608Metric", "ref": "R900", "x_mm": fx, "y_mm": fy, "channel": "file"})).structured_content
        assert placed["changed"] and placed["channel"] == "file" and placed["snapshot"]
        moved = (await c.call_tool("pcb_move_footprint", {"board_path": board, "ref": "R900", "x_mm": fx + 1.0, "y_mm": fy + 1.0, "rotation": 90, "channel": "file"})).structured_content
        assert moved["items"][0]["rotation_deg"] == 90
        drc = (await c.call_tool("run_drc", {"board_path": board, "schematic_parity": False})).structured_content
        assert drc["verdict"] in ("PASS", "WARN"), [f["description"] for f in drc["findings"] if f["severity"] == "error"][:5]
        gone = (await c.call_tool("pcb_delete_items", {"board_path": board, "ids": [placed["item_ids"][0]], "channel": "file"})).structured_content
        assert gone["deleted"][0]["kind"] == "footprint"
