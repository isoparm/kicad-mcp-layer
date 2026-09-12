"""kicad-mcp-layer: an AI layer for KiCad 10, exposed as an MCP server.

Three channels, chosen per operation:

* ``cli``  - ``kicad-cli`` subprocess for ERC, DRC, netlists, BOM, renders and fabrication exports.
* ``ipc``  - KiCad's IPC API (via ``kicad-python``) for the board open in the PCB editor.
* ``file`` - direct, lossless edits of ``.kicad_sch`` / ``.kicad_pro`` files.

The SWIG ``pcbnew`` module is never imported; a test enforces that.
"""

__version__ = "0.1.0"
