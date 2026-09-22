"""Typed, model-readable errors.

Every failure a tool anticipates is raised as :class:`LayerError`. It subclasses the MCP
SDK's ``ToolError``, so the message reaches the model as an ``is_error`` result instead of
an opaque protocol error. The message carries a stable code, the reason, a hint that says
what to do next, and whether retrying could help.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

# Stable error codes. Tools and tests refer to these, never to message text.
WORKSPACE_VIOLATION = "WORKSPACE_VIOLATION"
FILE_NOT_FOUND = "FILE_NOT_FOUND"
WRONG_FILE_TYPE = "WRONG_FILE_TYPE"
READ_ONLY_MODE = "READ_ONLY_MODE"
KICAD_CLI_NOT_FOUND = "KICAD_CLI_NOT_FOUND"
KICAD_CLI_FAILED = "KICAD_CLI_FAILED"
KICAD_CLI_TIMEOUT = "KICAD_CLI_TIMEOUT"
KICAD_NOT_RUNNING = "KICAD_NOT_RUNNING"
KICAD_API_DISABLED = "KICAD_API_DISABLED"
BOARD_NOT_OPEN = "BOARD_NOT_OPEN"
PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
NOT_FOUND_IN_DESIGN = "NOT_FOUND_IN_DESIGN"
DOC_FETCH_FAILED = "DOC_FETCH_FAILED"
LIB_FETCH_FAILED = "LIB_FETCH_FAILED"
DOC_NOT_PDF = "DOC_NOT_PDF"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
IPC_REJECTED = "IPC_REJECTED"
IPC_BUSY = "IPC_BUSY"
SCHEMATIC_LOCKED = "SCHEMATIC_LOCKED"
EDIT_CONFLICT = "EDIT_CONFLICT"
JOB_FAILED = "JOB_FAILED"


class LayerError(ToolError):
    """A failure the tool saw coming, with a code, a hint and a retry flag."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        retryable: bool = False,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.hint = hint
        self.retryable = retryable
        self.data = data or {}
        text = f"[{code}] {message}"
        if hint:
            text += f" Hint: {hint}"
        if retryable:
            text += " (retryable)"
        super().__init__(text)


def require_write_mode(action: str) -> None:
    """Raise unless the server was started in write mode."""
    from kicad_layer.config import settings

    if not settings().writes_enabled:
        raise LayerError(
            READ_ONLY_MODE,
            f"{action} modifies a design file, and the server is in read-only mode.",
            hint="Restart the server with KICAD_LAYER_MODE=write to allow design edits.",
        )
