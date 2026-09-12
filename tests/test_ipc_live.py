"""Board reads against a running KiCad with a board open. Self-skips otherwise."""

import pytest

from kicad_layer.config import load_settings, set_settings
from tests.conftest import FIXTURES


def _board_open() -> bool:
    try:
        from kicad_layer.ipc.probe import probe_ipc

        set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(FIXTURES)}))
        info = probe_ipc(timeout_ms=1500)
        return bool(info.reachable and info.open_boards)
    except Exception:
        return False
    finally:
        set_settings(None)


gui = pytest.mark.skipif(not _board_open(), reason="KiCad is not running with the API enabled and a board open")
pytestmark = [pytest.mark.gui, gui, pytest.mark.anyio]


@pytest.fixture
def open_workspace(tmp_path_factory):
    """The live board may live anywhere; widen the workspace to the whole KiCad folder."""
    cache = tmp_path_factory.mktemp("cache")
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(FIXTURES.parents[1].parent), "KICAD_LAYER_CACHE_DIR": str(cache)}))
    yield
    set_settings(None)


@pytest.fixture
async def live_client(open_workspace):
    from mcp import Client

    from kicad_layer.server import build_server

    async with Client(build_server(tier="full"), raise_exceptions=True) as c:
        yield c


async def call(client, name, **args):
    result = await client.call_tool(name, args)
    assert not result.is_error, result.content[0].text if result.content else result
    return result.structured_content


async def test_summary(live_client):
    s = await call(live_client, "pcb_summary")
    assert s["copper_layer_count"] >= 2
    assert "F.Cu" in s["enabled_layers"] and "B.Cu" in s["enabled_layers"]
    assert s["counts"]["footprints"] >= 0
    assert any(layer["type"] == "copper" for layer in s["stackup"])
    assert any("design rules" in n.lower() for n in s["notes"])


async def test_list_every_kind(live_client):
    for kind in ("footprint", "pad", "track", "via", "zone", "net", "text"):
        page = await call(live_client, "pcb_list_items", kind=kind, limit=50)
        assert page["kind"] == kind
        assert page["returned"] <= 50
        for item in page["items"]:
            assert item["kind"] == kind


async def test_footprints_have_geometry_when_present(live_client):
    page = await call(live_client, "pcb_list_items", kind="footprint", limit=5)
    for fp in page["items"]:
        assert fp["ref"]
        assert fp["layer"] in ("F.Cu", "B.Cu")
        assert isinstance(fp["x_mm"], float)


async def test_bad_layer_name_is_an_argument_error(live_client):
    result = await live_client.call_tool("pcb_list_items", {"kind": "track", "layer": "Front"})
    assert result.is_error
    assert "INVALID_ARGUMENT" in result.content[0].text


async def test_net_stats(live_client):
    stats = await call(live_client, "pcb_net_stats")
    assert stats["net_count"] >= 0
    for row in stats["nets"]:
        assert row["total_length_mm"] >= 0
