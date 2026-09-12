"""Build and run the MCP server.

Logging goes to stderr only. On a stdio transport, anything printed to stdout corrupts the
protocol stream, so nothing in this package ever prints.
"""

from __future__ import annotations

import logging
import os
import sys

from mcp.server import MCPServer

from kicad_layer import __version__
from kicad_layer.config import settings
from kicad_layer.tools import register_tools

INSTRUCTIONS = """\
kicad-mcp-layer drives KiCad 10 through three channels: kicad-cli (headless checks, netlists,
exports, renders), KiCad's IPC API (the board open in the PCB editor) and direct file edits
(schematics). Paths are absolute or relative to the workspace root and must stay inside it.

Rules of thumb:
- If a tool fails unexpectedly, call kicad_doctor before retrying; it names the cause.
- run_erc, run_drc, sch_netlist and sch_trace work whether KiCad is open or closed.
- A verdict of UNVERIFIED or BLOCKED means nothing was checked; never report it as a pass.
- In read-only mode (the default) design files are never modified; exports and reports are.
- KiCad does not reload files changed on disk; after a file edit the user reverts in the GUI.
- The core tool tier is registered by default; KICAD_LAYER_TOOLS=full adds design edits and routing.
"""


def build_server(tier: str | None = None) -> MCPServer:
    """The server with the tools of ``tier`` (default: the configured one)."""
    mcp = MCPServer(
        "kicad-mcp-layer",
        title="KiCad Layer",
        description="An AI layer for KiCad 10.",
        instructions=INSTRUCTIONS,
        version=__version__,
    )
    register_tools(mcp, tier or settings().tools_tier)
    return mcp


def main(argv: list[str] | None = None) -> None:
    level = os.environ.get("KICAD_LAYER_LOG", "INFO").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger(__name__).info("kicad-mcp-layer %s starting on stdio (pid %s)", __version__, os.getpid())
    build_server().run("stdio")


if __name__ == "__main__":
    main()
