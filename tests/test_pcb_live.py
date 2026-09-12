"""Live board edits against KiCad's PCB Editor. Self-skips unless a board is open.

The test leaves the board exactly as it found it: every item it creates is deleted again
and nothing is saved.
"""

from __future__ import annotations

import pytest

from kicad_layer.config import load_settings, set_settings
from tests.conftest import FIXTURES
from tests.test_ipc_live import _board_open

pytestmark = [pytest.mark.gui, pytest.mark.skipif(not _board_open(), reason="KiCad is not running with the API enabled and a board open"), pytest.mark.anyio]


@pytest.fixture
def live_write_workspace(tmp_path_factory):
    cache = tmp_path_factory.mktemp("cache")
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(FIXTURES.parents[1].parent), "KICAD_LAYER_MODE": "write", "KICAD_LAYER_CACHE_DIR": str(cache)}))
    yield
    set_settings(None)


@pytest.fixture
async def client(live_write_workspace):
    from mcp import Client

    from kicad_layer.server import build_server

    async with Client(build_server(tier="full"), raise_exceptions=True) as c:
        yield c


async def call(client, name, **args):
    result = await client.call_tool(name, args)
    assert not result.is_error, result.content[0].text if result.content else result
    return result.structured_content


async def test_place_move_track_via_delete_live(client):
    summary = await call(client, "pcb_summary")
    x0 = (summary["size"]["x_mm"] + summary["size"]["width_mm"] + 20.0) if summary.get("size") else 300.0
    y0 = summary["size"]["y_mm"] if summary.get("size") else 300.0
    created: list[str] = []
    try:
        placed = await call(client, "pcb_place_footprint", lib_id="Resistor_SMD:R_0603_1608Metric", ref="RLIVE1", x_mm=x0, y_mm=y0, value="test")
        assert placed["channel"] == "ipc" and placed["items"] and placed["items"][0]["ref"] == "RLIVE1"
        created += placed["item_ids"]
        moved = await call(client, "pcb_move_footprint", ref="RLIVE1", x_mm=x0 + 3.0, y_mm=y0 + 1.0, rotation=90)
        assert abs(moved["items"][0]["x_mm"] - (x0 + 3.0)) < 0.01 and moved["items"][0]["rotation_deg"] == 90
        track = await call(client, "pcb_add_track", points=[[x0, y0 + 6], [x0 + 4, y0 + 6]], net="GND", width=0.3, layer="F.Cu")
        assert track["items"] and track["items"][0]["net"] == "GND" and track["items"][0]["width_mm"] == 0.3
        created += track["item_ids"]
        via = await call(client, "pcb_add_via", x_mm=x0 + 4, y_mm=y0 + 6, net="GND")
        assert via["items"][0]["drill_mm"] == 0.3
        created += via["item_ids"]
        listed = await call(client, "pcb_list_items", kind="footprint", ref="RLIVE")
        assert listed["total"] == 1
    finally:
        if created:
            gone = await call(client, "pcb_delete_items", ids=created)
            assert {d["kind"] for d in gone["deleted"]} >= {"FootprintInstance"} or gone["deleted"]
    listed = await call(client, "pcb_list_items", kind="footprint", ref="RLIVE")
    assert listed["total"] == 0
