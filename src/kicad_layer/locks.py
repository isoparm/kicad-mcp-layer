"""KiCad's lock files, and whether the process that wrote one is still there.

KiCad writes ``~<file>.lck`` next to a sheet or board it opens and removes it on close; a crash
leaves it behind. The file is JSON with the ``hostname`` and ``username`` that hold it (and a
``pid`` in some builds). A lock is **orphaned** only when its owner is provably gone: its pid is
not running, or it names this host and no KiCad process runs here at all. A lock from another host
(a shared drive) is never called orphaned: this machine cannot see that process.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".kicad-layer"}


@dataclass
class LockInfo:
    path: Path
    hostname: str | None = None
    username: str | None = None
    pid: int | None = None


def lock_path(design: Path) -> Path:
    return design.parent / f"~{design.name}.lck"


def read_lock(lock: Path) -> LockInfo:
    info = LockInfo(path=lock)
    try:
        raw = json.loads(lock.read_text(encoding="utf-8", errors="replace") or "{}")
    except (OSError, ValueError):
        return info
    if isinstance(raw, dict):
        info.hostname = raw.get("hostname") or None
        info.username = raw.get("username") or None
        pid = raw.get("pid") or raw.get("process_id")
        try:
            info.pid = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            info.pid = None
    return info


def pid_running(pid: int) -> bool:
    """Whether a process with this id exists (psutil when installed, else the OS)."""
    try:
        import psutil  # type: ignore[import-not-found]

        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    if sys.platform == "win32":
        import subprocess

        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=15,
                                 creationflags=0x08000000).stdout
        except (OSError, subprocess.SubprocessError):
            return True  # cannot tell: assume it runs
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def kicad_pids() -> list[int]:
    """Process ids of every KiCad process on this machine."""
    try:
        import psutil  # type: ignore[import-not-found]

        from kicad_layer.ipc.probe import KICAD_PROCESS_NAMES

        return sorted(p.pid for p in psutil.process_iter(["name"]) if (p.info.get("name") or "").lower() in KICAD_PROCESS_NAMES)
    except ImportError:
        from kicad_layer.ipc.probe import kicad_processes

        return sorted(p.pid for p in kicad_processes())


def owner_alive(lock: Path) -> bool:
    """False only when the lock's owner is provably gone (see the module docstring)."""
    info = read_lock(lock)
    if info.pid is not None and (info.hostname in (None, socket.gethostname())):
        return pid_running(info.pid)
    if info.hostname and info.hostname.lower() != socket.gethostname().lower():
        return True
    return bool(kicad_pids())


def clean_orphan_locks(root: Path, *, max_depth: int = 4) -> list[Path]:
    """Remove KiCad lock files under ``root`` whose owner is gone; each removal is logged."""
    removed: list[Path] = []
    root = Path(root)
    if not root.is_dir():
        return removed
    base_depth = len(root.parts)
    kicad_running: bool | None = None
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - base_depth
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.endswith("-backups") and depth < max_depth]
        for name in filenames:
            if not (name.startswith("~") and name.endswith(".lck") and ".kicad_" in name):
                continue
            lock = Path(dirpath) / name
            info = read_lock(lock)
            if info.pid is not None and info.hostname in (None, socket.gethostname()):
                gone = not pid_running(info.pid)
            elif info.hostname and info.hostname.lower() != socket.gethostname().lower():
                gone = False
            else:
                if kicad_running is None:
                    kicad_running = bool(kicad_pids())
                gone = not kicad_running
            if not gone:
                continue
            try:
                lock.unlink()
            except OSError as exc:
                log.warning("could not remove orphan lock %s: %s", lock, exc)
                continue
            log.warning("removed orphan KiCad lock %s (owner %s@%s, pid %s, no longer running)", lock, info.username, info.hostname, info.pid)
            removed.append(lock)
    return removed
