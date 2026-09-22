"""Project-level reads from ``.kicad_pro`` (JSON) and the design file headers."""

from __future__ import annotations

import json
import re
from pathlib import Path

from kicad_layer.models import ProjectInfo
from kicad_layer.paths import display, locate_project

_VERSION_RE = re.compile(rb"\(version\s+(\d+)\)")


def format_version(path: Path | None) -> int | None:
    """The ``(version N)`` header of a KiCad S-expression file, without parsing it all."""
    if path is None or not path.is_file():
        return None
    with path.open("rb") as fh:
        head = fh.read(512)
    m = _VERSION_RE.search(head)
    return int(m.group(1)) if m else None


def open_project(path: str) -> ProjectInfo:
    files = locate_project(path)
    text_variables: dict[str, str] = {}
    netclasses: list[str] = []
    project_version: int | None = None
    warnings: list[str] = []

    if files.project_file is not None:
        try:
            raw = json.loads(files.project_file.read_text(encoding="utf-8", errors="replace") or "{}")
        except json.JSONDecodeError as exc:
            raw = {}
            warnings.append(f"{files.project_file.name} is not valid JSON: {exc}")
        if not raw:
            warnings.append(f"{files.project_file.name} is empty; KiCad will fill it on the next save.")
        text_variables = {str(k): str(v) for k, v in (raw.get("text_variables") or {}).items()}
        classes = (raw.get("net_settings") or {}).get("classes") or []
        netclasses = [c.get("name", "") for c in classes if isinstance(c, dict)]
        meta = raw.get("meta") or {}
        project_version = meta.get("version") if isinstance(meta.get("version"), int) else None
    else:
        warnings.append("No .kicad_pro file; ERC settings, netclasses and variables fall back to KiCad defaults.")

    locks = [display(p) for p in files.lock_files]
    if locks:
        warnings.append(
            "KiCad holds a lock on a file in this project. Schematic edits will be refused until "
            "that editor window is closed; reads and kicad-cli commands still work."
        )
    return ProjectInfo(
        name=files.name,
        directory=display(files.directory),
        project_file=display(files.project_file) if files.project_file else None,
        root_schematic=display(files.root_schematic) if files.root_schematic else None,
        board=display(files.board) if files.board else None,
        schematics=[display(p) for p in files.schematics],
        boards=[display(p) for p in files.boards],
        text_variables=text_variables,
        netclasses=netclasses,
        project_file_version=project_version,
        schematic_format_version=format_version(files.root_schematic),
        board_format_version=format_version(files.board),
        lock_files=locks,
        warnings=warnings,
    )


NO_PROJECT_RULES = "no project file next to the board: KiCad used default design rules"


def board_rule_warnings(board: Path) -> list[str]:
    """What kicad-cli will miss of the board's design rules, as warnings.

    kicad-cli (DRC, zone refill) takes the net classes and constraints from ``<board>.kicad_pro`` and the
    custom rules from ``<board>.kicad_dru``, both found by the board's own name and nothing else. A board
    copied or renamed without them is checked and filled against KiCad's defaults, silently. A
    ``.kicad_dru`` under another name in the same folder is the copy-with-rename mistake: its rules
    exist and are not applied."""
    warnings: list[str] = []
    folder = board.parent
    pro = board.with_suffix(".kicad_pro")
    if not pro.is_file():
        others = sorted(p.name for p in folder.glob("*.kicad_pro"))
        msg = f"{NO_PROJECT_RULES} (expected {pro.name})"
        if others:
            msg += f"; {', '.join(others)} is ignored because KiCad reads only the project named after the board"
        warnings.append(msg)
    dru = board.with_suffix(".kicad_dru")
    if not dru.is_file():
        strays = sorted(p.name for p in folder.glob("*.kicad_dru"))
        if strays:
            warnings.append(f"custom rules {', '.join(strays)} not applied: KiCad reads only {dru.name}, the rules file named after the board")
    return warnings
