"""Workspace-bounded path resolution and KiCad project discovery.

Every tool that takes a path calls :func:`resolve_in_workspace`. Nothing outside the
workspace root is ever read or written, whatever the model passes in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from kicad_layer.config import settings
from kicad_layer.errors import (
    FILE_NOT_FOUND,
    PROJECT_NOT_FOUND,
    WORKSPACE_VIOLATION,
    WRONG_FILE_TYPE,
    LayerError,
)

SCHEMATIC = ".kicad_sch"
BOARD = ".kicad_pcb"
PROJECT = ".kicad_pro"


def _norm(p: Path | str) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def workspace_root() -> Path:
    return Path(os.path.realpath(settings().workspace_root))


def is_inside_workspace(path: Path) -> bool:
    root = _norm(workspace_root())
    target = _norm(path)
    try:
        return os.path.commonpath([root, target]) == root
    except ValueError:  # different drives on Windows
        return False


def resolve_in_workspace(
    path: str | os.PathLike[str],
    *,
    must_exist: bool = True,
    suffixes: tuple[str, ...] = (),
) -> Path:
    """Resolve ``path`` (absolute, or relative to the workspace root) and check it is inside."""
    raw = Path(os.fspath(path)).expanduser()
    candidate = raw if raw.is_absolute() else workspace_root() / raw
    resolved = Path(os.path.realpath(candidate))
    if not is_inside_workspace(resolved):
        raise LayerError(
            WORKSPACE_VIOLATION,
            f"{raw} resolves outside the workspace root {workspace_root()}.",
            hint="Use a path inside the workspace, or start the server with "
            "KICAD_LAYER_WORKSPACE pointing at a parent directory.",
        )
    if must_exist and not resolved.exists():
        raise LayerError(FILE_NOT_FOUND, f"{resolved} does not exist.")
    if suffixes and resolved.is_file() and resolved.suffix.lower() not in suffixes:
        raise LayerError(
            WRONG_FILE_TYPE,
            f"{resolved.name} is not a {' or '.join(suffixes)} file.",
        )
    return resolved


def display(path: Path) -> str:
    """A path as shown to the model: relative to the workspace when possible."""
    try:
        return str(path.relative_to(workspace_root()))
    except ValueError:
        return str(path)


@dataclass(frozen=True)
class ProjectFiles:
    directory: Path
    name: str
    project_file: Path | None
    root_schematic: Path | None
    board: Path | None
    schematics: tuple[Path, ...]
    boards: tuple[Path, ...]

    @property
    def lock_files(self) -> tuple[Path, ...]:
        return tuple(sorted(self.directory.glob("~*.lck")))


def locate_project(path: str | os.PathLike[str]) -> ProjectFiles:
    """Find the KiCad project around ``path`` (a directory or any project file)."""
    p = resolve_in_workspace(path, must_exist=True)
    directory = p if p.is_dir() else p.parent
    project_files = sorted(directory.glob(f"*{PROJECT}"))
    stem = p.stem if p.is_file() else None
    project_file = next((x for x in project_files if stem and x.stem == stem), None)
    if project_file is None and project_files:
        project_file = project_files[0]
    name = project_file.stem if project_file else (stem or directory.name)

    schematics = tuple(sorted(directory.glob(f"*{SCHEMATIC}")))
    boards = tuple(sorted(directory.glob(f"*{BOARD}")))
    root_schematic = directory / f"{name}{SCHEMATIC}"
    if not root_schematic.exists():
        root_schematic = p if p.suffix.lower() == SCHEMATIC else (schematics[0] if schematics else None)
    board = directory / f"{name}{BOARD}"
    if not board.exists():
        board = p if p.suffix.lower() == BOARD else (boards[0] if boards else None)

    if project_file is None and root_schematic is None and board is None:
        raise LayerError(
            PROJECT_NOT_FOUND,
            f"No .kicad_pro, .kicad_sch or .kicad_pcb file found at {display(directory)}.",
        )
    return ProjectFiles(
        directory=directory,
        name=name,
        project_file=project_file,
        root_schematic=root_schematic,
        board=board,
        schematics=schematics,
        boards=boards,
    )


def root_schematic_for(schematic_path: str | os.PathLike[str]) -> Path:
    """The root sheet of the project that contains ``schematic_path``.

    ``kicad-cli sch`` commands must be given the root sheet, or hierarchical nets and
    sub-sheets are missing from the result.
    """
    p = resolve_in_workspace(schematic_path, must_exist=True, suffixes=(SCHEMATIC,))
    project = locate_project(p)
    return project.root_schematic or p
