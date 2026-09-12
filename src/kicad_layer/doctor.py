"""The ``kicad_doctor`` tool: what is this server, and what can it reach right now.

It names the process actually answering, because a Claude Code session inside the
desktop app may be served by the app's own server process rather than the one the
project config spawns.
"""

from __future__ import annotations

import sys

from kicad_layer import __version__
from kicad_layer.formats import BOARD_FORMAT, KICAD_MAJOR, KICAD_RELEASE, SCHEMATIC_FORMAT, WRITER_FORMATS
from kicad_layer.cli.discovery import find_kicad_cli
from kicad_layer.config import settings
from kicad_layer.errors import LayerError
from kicad_layer.ipc.probe import kicad_processes, probe_ipc
from kicad_layer.models import DoctorReport, KicadCliInfo


def cli_info() -> KicadCliInfo:
    try:
        cli = find_kicad_cli()
    except LayerError as exc:
        return KicadCliInfo(found=False, error=str(exc))
    return KicadCliInfo(
        found=True,
        path=str(cli.path),
        version=cli.version,
        source=cli.source,
        sch_export_verbs=list(cli.sch_export_verbs),
        pcb_export_verbs=list(cli.pcb_export_verbs),
    )


def diagnose() -> DoctorReport:
    s = settings()
    cli = cli_info()
    ipc = probe_ipc()
    processes = kicad_processes()
    advice: list[str] = []

    if not cli.found:
        advice.append("kicad-cli is missing, so ERC, DRC, netlists, exports and renders cannot run.")
    elif cli.version and not cli.version.startswith(f"{KICAD_MAJOR}."):
        advice.append(
            f"kicad-cli {cli.version} found; this server targets KiCad {KICAD_RELEASE} and writes its formats "
            f"(schematic {SCHEMATIC_FORMAT}, board {BOARD_FORMAT}). Expect verb drift; files this layer writes "
            "still open, and KiCad upgrades them on save. Run the full test suite after a KiCad upgrade."
        )

    if ipc.diagnosis == "not_installed":
        advice.append(
            "kicad-python is not installed, so the IPC channel (the board open in KiCad) is off; kicad-cli and "
            "file tools still work. Install it with: pip install \"kicad-mcp-layer[ipc]\""
        )
    elif ipc.diagnosis == "not_running":
        advice.append("KiCad is not running. File and kicad-cli tools still work; board tools need KiCad open.")
    elif ipc.diagnosis == "api_disabled":
        advice.append(
            "KiCad is running but its API is off. In KiCad open Preferences > Plugins and tick "
            "'Enable KiCad API'. It takes effect immediately, no restart needed."
        )
    elif ipc.diagnosis == "busy":
        advice.append("KiCad answered slowly or not at all: a modal dialog or interactive tool may be open.")
    elif ipc.diagnosis == "no_editor_open":
        advice.append(
            "KiCad's API answers, but only the project manager is open. Open the PCB Editor "
            "(and the Schematic Editor if needed) from the project window; board tools need it."
        )
    elif ipc.diagnosis == "reachable" and not ipc.open_boards:
        advice.append("KiCad is reachable but no board is open in the PCB editor; board tools need one.")

    if s.mode != "write":
        advice.append("Read-only mode: design files will not be modified. Exports and reports are allowed.")

    return DoctorReport(
        server_version=__version__,
        python=sys.version.split()[0],
        executable=sys.executable,
        pid=__import__("os").getpid(),
        mode=s.mode,
        workspace_root=str(s.workspace_root),
        cache_dir=str(s.cache_dir),
        kicad_cli=cli,
        ipc=ipc,
        kicad_processes=processes,
        advice=advice,
        writer_formats=dict(WRITER_FORMATS),
    )
