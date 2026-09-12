"""Find out whether KiCad is reachable, and why not when it is not.

On Windows the API endpoint is a named pipe: nothing appears on disk while the server is
off, and the pipe namespace can be listed. That lets the doctor tell three states apart:

* no KiCad process at all,
* KiCad running but the API switched off (Preferences > Plugins > Enable KiCad API),
* API on and answering.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from kicad_layer.config import settings
from kicad_layer.models import IpcInfo, ProcessInfo

log = logging.getLogger(__name__)

KICAD_PROCESS_NAMES = {"kicad", "kicad.exe", "pcbnew", "pcbnew.exe", "eeschema", "eeschema.exe"}
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def default_address() -> str:
    """The address kicad-python uses when KICAD_API_SOCKET is unset."""
    if sys.platform == "win32":
        return f"ipc://{tempfile.gettempdir()}\\kicad\\api.sock"
    return "ipc:///tmp/kicad/api.sock"


def resolved_address() -> tuple[str, str]:
    s = settings()
    if s.ipc_address_override:
        return s.ipc_address_override, "KICAD_API_SOCKET environment variable"
    return default_address(), "kicad-python default"


def kicad_pipe_names() -> list[str]:
    if sys.platform != "win32":
        return []
    try:
        names = os.listdir("\\\\.\\pipe\\")
    except OSError as exc:
        log.debug("pipe listing failed: %s", exc)
        return []
    return sorted(n for n in names if "kicad" in n.lower())


def kicad_processes() -> list[ProcessInfo]:
    out: list[ProcessInfo] = []
    try:
        if sys.platform == "win32":
            proc = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, errors="replace", timeout=15,
                creationflags=_CREATE_NO_WINDOW,
            )
            for row in csv.reader(io.StringIO(proc.stdout)):
                if len(row) >= 2 and row[0].lower() in KICAD_PROCESS_NAMES:
                    out.append(ProcessInfo(pid=int(row[1]), name=row[0]))
        else:
            proc = subprocess.run(
                ["ps", "-A", "-o", "pid=,comm="],
                capture_output=True, text=True, errors="replace", timeout=15,
            )
            for line in proc.stdout.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2 and Path(parts[1]).name.lower() in KICAD_PROCESS_NAMES:
                    out.append(ProcessInfo(pid=int(parts[0]), name=Path(parts[1]).name))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.debug("process listing failed: %s", exc)
    return out


def _document_path(doc) -> str:
    """Best-effort path for a DocumentSpecifier, whatever the proto exposes."""
    project = getattr(doc, "project", None)
    project_path = getattr(project, "path", "") if project is not None else ""
    board_file = getattr(doc, "board_filename", "") or ""
    if board_file:
        return str(Path(project_path) / board_file) if project_path else board_file
    project_name = getattr(project, "name", "") if project is not None else ""
    if project_path or project_name:
        return str(Path(project_path) / project_name) if project_path else project_name
    return str(doc).strip().replace("\n", " ")[:200]


def probe_ipc(timeout_ms: int | None = None) -> IpcInfo:
    """Try to reach KiCad and report exactly what was observed."""
    address, source = resolved_address()
    info = IpcInfo(address=address, address_source=source, reachable=False, pipe_names=kicad_pipe_names())
    processes = kicad_processes()
    timeout = timeout_ms or settings().ipc_timeout_ms

    try:
        from kipy import KiCad
        from kipy.errors import ApiError
        from kipy.errors import ConnectionError as KipyConnectionError
        from kipy.proto.common.types import base_types_pb2 as bt
    except Exception as exc:
        info.error = f"kicad-python is not importable, so the IPC channel is off: {exc}"
        info.diagnosis = "not_installed"
        return info

    try:
        kicad = KiCad(
            socket_path=address if settings().ipc_address_override else None,
            client_name=f"kicad-mcp-layer-{os.getpid()}",
            timeout_ms=timeout,
        )
        version = kicad.get_version()
        info.reachable = True
        info.kicad_version = getattr(version, "full_version", None) or str(version)
        info.diagnosis = "reachable"
        no_handler = False
        try:
            info.open_boards = [_document_path(d) for d in kicad.get_open_documents(bt.DOCTYPE_PCB)]
        except ApiError as exc:
            if "no handler available" in str(exc):
                no_handler = True
            else:
                info.error = f"open boards unavailable: {exc}"
        try:
            info.open_schematics = [_document_path(d) for d in kicad.get_open_documents(bt.DOCTYPE_SCHEMATIC)]
        except ApiError as exc:
            if "no handler available" in str(exc):
                no_handler = True
            else:
                info.error = (info.error + "; " if info.error else "") + f"open schematics unavailable: {exc}"
        if no_handler and not info.open_boards and not info.open_schematics:
            # Only the project manager answers: it has no document handlers at all.
            info.diagnosis = "no_editor_open"
    except KipyConnectionError as exc:
        text = str(exc)
        info.error = text
        if "Timed out" in text or "timed out" in text:
            info.diagnosis = "busy"
        elif processes and not info.pipe_names and sys.platform == "win32":
            info.diagnosis = "api_disabled"
        elif processes:
            info.diagnosis = "stale_endpoint" if info.pipe_names else "api_disabled"
        else:
            info.diagnosis = "not_running"
    except ApiError as exc:
        info.error = str(exc)
        info.diagnosis = "unknown"
    except Exception as exc:  # anything else from the transport layer
        info.error = f"{type(exc).__name__}: {exc}"
        info.diagnosis = "unknown"
    return info
