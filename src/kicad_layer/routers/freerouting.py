"""Run FreeRouting headless on a DSN and bring the session back as routes.

FreeRouting (github.com/freerouting/freerouting, GPL-3.0) is a Java autorouter that reads Specctra
DSN and writes Specctra SES. It is not bundled: ``jar_path()`` looks for ``freerouting*.jar`` in
``KICAD_LAYER_FREEROUTING`` (a file or a directory), then in ``<workspace>/tools``. Java comes from
``KICAD_LAYER_JAVA``, a portable JRE in ``<workspace>/tools/jre`` (FreeRouting 2.4 needs Java 25), or the PATH.

The router does not know differential pairs; it routes both halves as unrelated nets. Route pairs
first with the pair router and export them as protected wiring, then let FreeRouting finish the
rest. ``route_check`` measures the result either way.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .. import jobs
from ..config import settings
from ..errors import LayerError
from .ses import Routes, parse_ses

ROUTER_NOT_FOUND = "ROUTER_NOT_FOUND"
ROUTER_FAILED = "ROUTER_FAILED"


def jar_path() -> Path:
    env = os.environ.get("KICAD_LAYER_FREEROUTING")
    candidates: list[str] = []
    if env:
        p = Path(env)
        candidates += [str(p)] if p.is_file() else glob.glob(str(p / "freerouting*.jar"))
    candidates += glob.glob(str(Path(settings().workspace_root) / "tools" / "freerouting*.jar"))
    for c in sorted(candidates, reverse=True):
        if Path(c).is_file():
            return Path(c)
    raise LayerError(ROUTER_NOT_FOUND, "no FreeRouting jar found",
                     hint="Download freerouting-<version>.jar from github.com/freerouting/freerouting/releases into <workspace>/tools, or set KICAD_LAYER_FREEROUTING.")


def java_path() -> str:
    """A Java runtime: KICAD_LAYER_JAVA, then a portable JRE in <workspace>/tools/jre, then the PATH.

    FreeRouting 2.4 needs Java 25; a Temurin JRE unpacked into tools/jre keeps the system Java untouched."""
    env = os.environ.get("KICAD_LAYER_JAVA")
    if env and Path(env).is_file():
        return env
    bundled = Path(settings().workspace_root) / "tools" / "jre" / "bin" / ("java.exe" if os.name == "nt" else "java")
    if bundled.is_file():
        return str(bundled)
    j = shutil.which("java")
    if not j:
        raise LayerError(ROUTER_NOT_FOUND, "no Java runtime found", hint="Unpack a Temurin JRE 25 into <workspace>/tools/jre, set KICAD_LAYER_JAVA, or add java to PATH.")
    return j


@dataclass
class RouterRun:
    dsn: Path
    ses: Path
    seconds: float
    returncode: int
    log_tail: str
    routes: Routes


def run(dsn: Path, ses: Path, *, max_passes: int = 30, improvement_threshold: float = 0.5, threads: int | None = None,
        ignore_classes: tuple[str, ...] = (), timeout_s: float = 3600.0) -> RouterRun:
    """Route ``dsn`` into ``ses`` headless; raises ROUTER_FAILED when no session comes out."""
    jar = jar_path()
    cmd = [java_path(), "-jar", str(jar), "-de", str(dsn), "-do", str(ses), "--gui.enabled=false", "-mp", str(max_passes),
           "-oit", str(improvement_threshold), "-dct", "0", "-da", "--logging.file.enabled=false"]
    if threads is not None:
        cmd += ["-mt", str(threads)]
    if ignore_classes:
        cmd += ["-inc", ",".join(ignore_classes)]
    if ses.exists():
        ses.unlink()
    t0 = time.time()
    # streamed, so a background job (kicad_layer.jobs) shows the passes; on timeout FreeRouting is asked to stop
    # (it has no save-and-stop command headless) and a session it wrote before that is still used
    proc = jobs.run_process(cmd, timeout_s=timeout_s, cwd=dsn.parent)
    if proc.timed_out and not ses.exists():
        raise LayerError(ROUTER_FAILED, f"FreeRouting did not finish within {timeout_s:.0f} s", hint="Lower max_passes or route fewer nets at a time.")
    tail = "\n".join((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-25:])
    if proc.timed_out:
        tail += f"\nstopped at the {timeout_s:.0f} s timeout; the session is the one FreeRouting had written"
    if not ses.exists():
        raise LayerError(ROUTER_FAILED, f"FreeRouting exited with {proc.returncode} and wrote no session file", hint=tail[-800:] or "no output")
    routes = parse_ses(ses)
    return RouterRun(dsn=dsn, ses=ses, seconds=round(time.time() - t0, 1), returncode=proc.returncode, log_tail=tail, routes=routes)
