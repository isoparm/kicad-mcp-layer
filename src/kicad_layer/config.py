"""Runtime settings, read once from the environment.

Environment variables:

``KICAD_LAYER_WORKSPACE``
    Directory every path argument must resolve under. Default: the current directory.
``KICAD_LAYER_MODE``
    ``readonly`` (default) or ``write``. Design files are modified only in write mode.
    Exports, renders and reports are always allowed; they create new files and never
    touch a design file.
``KICAD_CLI``
    Explicit path to ``kicad-cli``. If it is set and wrong, that is an error, not a fallback.
``KICAD_API_SOCKET``
    IPC address. ``kicad-python`` honours it directly; the default on Windows is
    ``ipc://%TEMP%\\kicad\\api.sock``.
``KICAD_LAYER_CACHE_DIR``
    Cache for netlist exports and reports. Default: ``%LOCALAPPDATA%\\kicad-mcp-layer\\cache``
    on Windows, ``~/.cache/kicad-mcp-layer`` elsewhere.
``KICAD_LAYER_IPC_TIMEOUT_MS``
    Per-request timeout for API calls to the running KiCad. Default 15000. Large boards
    and zone refills may need more.
``KICAD_LAYER_LOG``
    Log level for stderr logging. Default ``INFO``.
``KICAD_LAYER_TOOLS``
    Which tools the server registers: ``core`` (default: checks, exports, renders, reviews,
    libraries, documents, board reads) or ``full`` (plus the design-edit tools and the routers).
``KICAD_LAYER_JOBS``
    ``worker`` (default): ``job_start`` runs each job in a detached worker process that outlives a
    server restart; ``thread``: in a thread of the server (lost when the server goes away).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

MODE_READONLY = "readonly"
MODE_WRITE = "write"
MODES = (MODE_READONLY, MODE_WRITE)
TIERS = ("core", "full")


@dataclass(frozen=True)
class Settings:
    workspace_root: Path
    mode: str
    kicad_cli_override: str | None
    ipc_address_override: str | None
    cache_dir: Path
    cli_timeout_s: float = 60.0
    cli_long_timeout_s: float = 600.0
    ipc_timeout_ms: int = 2000
    ipc_call_timeout_ms: int = 15000
    docs_dir: Path | None = None  # documentation library; default <workspace>/research/references
    tools_tier: str = "core"  # which tools the server registers, see TIERS

    @property
    def writes_enabled(self) -> bool:
        return self.mode == MODE_WRITE


def _default_cache_dir(env: Mapping[str, str]) -> Path:
    if sys.platform == "win32":
        base = Path(env.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        base = Path(env.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "kicad-mcp-layer" / "cache"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build a Settings object from ``env`` (default: ``os.environ``)."""
    e: Mapping[str, str] = os.environ if env is None else env
    root = Path(e.get("KICAD_LAYER_WORKSPACE") or Path.cwd()).expanduser()
    root = Path(os.path.realpath(root))
    mode = (e.get("KICAD_LAYER_MODE") or MODE_READONLY).strip().lower()
    if mode not in MODES:
        raise ValueError(f"KICAD_LAYER_MODE must be one of {MODES}, got {mode!r}")
    cache_dir = Path(e.get("KICAD_LAYER_CACHE_DIR") or _default_cache_dir(e)).expanduser()
    call_timeout = int(e.get("KICAD_LAYER_IPC_TIMEOUT_MS") or 15000)
    tier = (e.get("KICAD_LAYER_TOOLS") or "core").strip().lower()
    if tier not in TIERS:
        raise ValueError(f"KICAD_LAYER_TOOLS must be one of {TIERS}, got {tier!r}")
    return Settings(
        workspace_root=root,
        mode=mode,
        kicad_cli_override=e.get("KICAD_CLI") or None,
        ipc_address_override=e.get("KICAD_API_SOCKET") or None,
        cache_dir=cache_dir,
        ipc_call_timeout_ms=call_timeout,
        docs_dir=Path(e["KICAD_LAYER_DOCS_DIR"]).expanduser() if e.get("KICAD_LAYER_DOCS_DIR") else None,
        tools_tier=tier,
    )


_current: Settings | None = None


def settings() -> Settings:
    """The process-wide settings, loaded lazily on first use."""
    global _current
    if _current is None:
        _current = load_settings()
    return _current


def set_settings(value: Settings | None) -> None:
    """Replace the process-wide settings (tests) or reset them with ``None``."""
    global _current
    _current = value
