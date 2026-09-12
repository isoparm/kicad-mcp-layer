"""Run kicad-cli with a timeout and capture everything."""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from kicad_layer.errors import KICAD_CLI_TIMEOUT, LayerError

log = logging.getLogger(__name__)

# Observed on kicad-cli 10.0.6.
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_LOAD_FAILED = 3
EXIT_OUTPUT_DIR_FAILED = 4
EXIT_VIOLATIONS = 5

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass(frozen=True)
class CliResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == EXIT_OK

    def tail(self, lines: int = 12) -> str:
        text = (self.stdout + "\n" + self.stderr).strip().splitlines()
        return "\n".join(text[-lines:])


def run(command: Sequence[str | Path], *, timeout_s: float, cwd: Path | None = None) -> CliResult:
    """Run ``command`` and return its result. Raises LayerError on timeout only."""
    argv = [str(c) for c in command]
    log.info("run: %s", " ".join(argv))
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            cwd=str(cwd) if cwd else None,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise LayerError(
            KICAD_CLI_TIMEOUT,
            f"kicad-cli did not finish within {timeout_s:.0f} s: {' '.join(argv[:4])} ...",
            hint="Large boards and 3D exports can take minutes; retry with a longer timeout.",
            retryable=True,
        ) from exc
    result = CliResult(
        command=argv,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        duration_s=round(time.monotonic() - started, 3),
    )
    log.info("exit %s in %.2fs", result.returncode, result.duration_s)
    return result
