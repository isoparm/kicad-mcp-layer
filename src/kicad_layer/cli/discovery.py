"""Find kicad-cli and learn what it can do.

Resolution order (first hit wins):

1. ``KICAD_CLI`` environment variable. If set and wrong, that is an error, not a fallback.
2. ``kicad-cli`` on ``PATH``.
3. Versioned install directories, newest version first:
   ``C:\\Program Files\\KiCad\\<ver>\\bin``, ``C:\\KiCad\\<ver>\\bin``,
   ``%LOCALAPPDATA%\\Programs\\KiCad\\<ver>\\bin``, the macOS app bundle, ``/usr/bin``.

The un-versioned ``C:\\Program Files\\KiCad\\bin`` is never used; it does not exist on
KiCad 8 or later.

Once found, ``kicad-cli version`` and the export ``--help`` texts are read once and cached
by (path, mtime), so verb spellings come from the installed binary, not from assumptions.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from kicad_layer.cli import runner
from kicad_layer.config import settings
from kicad_layer.errors import KICAD_CLI_FAILED, KICAD_CLI_NOT_FOUND, LayerError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class KicadCli:
    path: Path
    version: str
    source: str
    sch_export_verbs: tuple[str, ...]
    pcb_export_verbs: tuple[str, ...]

    @property
    def major(self) -> int | None:
        m = re.match(r"(\d+)", self.version)
        return int(m.group(1)) if m else None

    def pcb_export_verb(self, *candidates: str) -> str:
        """The first candidate this binary understands (for verb drift like gerbers/gerber)."""
        for c in candidates:
            if c in self.pcb_export_verbs:
                return c
        return candidates[0]


_cache: dict[str, KicadCli] = {}


def _version_key(directory: Path) -> tuple[int, ...]:
    nums = re.findall(r"\d+", directory.name)
    return tuple(int(n) for n in nums) if nums else (0,)


def candidate_paths() -> list[tuple[str, Path]]:
    """Every location worth checking, in resolution order, as (source, path)."""
    found: list[tuple[str, Path]] = []
    s = settings()
    if s.kicad_cli_override:
        found.append(("KICAD_CLI environment variable", Path(s.kicad_cli_override)))
        return found  # an explicit override is authoritative

    on_path = shutil.which("kicad-cli")
    if on_path:
        found.append(("PATH", Path(on_path)))

    if sys.platform == "win32":
        roots = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "KiCad",
            Path(r"C:\KiCad"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "KiCad",
        ]
        for root in roots:
            if not root.is_dir():
                continue
            versions = sorted(
                (d for d in root.iterdir() if d.is_dir() and re.match(r"^\d", d.name)),
                key=_version_key,
                reverse=True,
            )
            for v in versions:
                exe = v / "bin" / "kicad-cli.exe"
                if exe.is_file():
                    found.append((f"versioned install {root}", exe))
    elif sys.platform == "darwin":
        for p in (
            Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
            Path.home() / "Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        ):
            if p.is_file():
                found.append(("macOS app bundle", p))
    else:
        for p in (Path("/usr/bin/kicad-cli"), Path("/usr/local/bin/kicad-cli")):
            if p.is_file():
                found.append(("system install", p))
    return found


def _parse_verbs(help_text: str) -> tuple[str, ...]:
    """Pull ``{bom,dxf,...}`` out of the first Usage line of a ``--help`` text."""
    m = re.search(r"\{([^}]*)\}", help_text)
    if not m:
        return ()
    return tuple(v.strip() for v in m.group(1).split(",") if v.strip())


def _probe(path: Path, source: str) -> KicadCli:
    version = runner.run([path, "version"], timeout_s=30)
    if not version.ok:
        raise LayerError(
            KICAD_CLI_FAILED,
            f"{path} exists but 'kicad-cli version' failed with exit {version.returncode}.",
            data={"output": version.tail()},
        )
    sch = runner.run([path, "sch", "export", "--help"], timeout_s=30)
    pcb = runner.run([path, "pcb", "export", "--help"], timeout_s=30)
    return KicadCli(
        path=path,
        version=version.stdout.strip().splitlines()[0] if version.stdout.strip() else "unknown",
        source=source,
        sch_export_verbs=_parse_verbs(sch.stdout + sch.stderr),
        pcb_export_verbs=_parse_verbs(pcb.stdout + pcb.stderr),
    )


def find_kicad_cli(*, refresh: bool = False) -> KicadCli:
    """Locate kicad-cli, probe it once, and cache the result."""
    candidates = candidate_paths()
    if not candidates:
        raise LayerError(
            KICAD_CLI_NOT_FOUND,
            "kicad-cli was not found on PATH or in any known KiCad install directory.",
            hint="Install KiCad 10, or set KICAD_CLI to the full path of kicad-cli.",
        )
    source, path = candidates[0]
    if not path.is_file():
        raise LayerError(
            KICAD_CLI_NOT_FOUND,
            f"{path} (from {source}) does not exist.",
            hint="Fix KICAD_CLI or unset it to let the server search the usual locations.",
        )
    key = f"{path}|{path.stat().st_mtime_ns}"
    if not refresh and key in _cache:
        return _cache[key]
    info = _probe(path, source)
    _cache.clear()
    _cache[key] = info
    log.info("kicad-cli %s at %s (%s)", info.version, info.path, info.source)
    return info
