"""The tool registry, docs/tools.md and the capability matrix must agree."""

import re
from pathlib import Path

import pytest

from kicad_layer import capabilities
from kicad_layer.tools import FULL_ONLY, GROUPS, TOOL_NAMES, tool_names

DOCS = Path(__file__).parent.parent / "docs" / "tools.md"


def documented_tools() -> dict[str, str]:
    """Tool name -> tier, from the table in docs/tools.md."""
    text = DOCS.read_text(encoding="utf-8")
    return dict(re.findall(r"^\|\s*`([a-z_]+)`\s*\|\s*(core|full)\s*\|", text, flags=re.MULTILINE))


@pytest.mark.anyio
async def test_registered_tools_match_registry(client):
    listed = await client.list_tools()
    names = {t.name for t in listed.tools}
    assert names == set(TOOL_NAMES)
    for tool in listed.tools:
        assert tool.description, f"{tool.name} has no description"
        assert tool.annotations is not None, f"{tool.name} has no annotations"
        assert tool.annotations.read_only_hint is not None


def test_docs_list_every_tool_with_its_tier():
    docs = documented_tools()
    assert set(docs) == set(TOOL_NAMES)
    assert {n for n, t in docs.items() if t == "core"} == set(tool_names("core"))


@pytest.mark.anyio
async def test_core_tier_reads_checks_and_exports_only(workspace):
    from mcp import Client

    from kicad_layer.server import build_server

    async with Client(build_server(tier="core"), raise_exceptions=True) as c:
        listed = await c.list_tools()
    names = {t.name for t in listed.tools}
    assert names == set(tool_names("core")) and names < set(TOOL_NAMES)
    assert all(not t.annotations.destructive_hint for t in listed.tools), "a design-edit tool leaked into the core tier"
    assert not names & {n for g in FULL_ONLY for n in GROUPS[g]}


def test_capability_matrix_names_only_real_or_planned_tools():
    covered = {r.tool for r in capabilities.ROWS if r.status == "covered"}
    assert covered == set(TOOL_NAMES), "every registered tool is a covered row and vice versa"
    planned = {r.tool for r in capabilities.ROWS if r.status == "planned"}
    assert not (planned & set(TOOL_NAMES)), "a planned row names a tool that already exists"
