"""KiCad on the user's desktop: is it running, does it hold the project, close it, open it again.

A program started from an agent's shell has no window on the desktop, so opening goes through a
Windows scheduled task the user creates once (its command is in the project's workflow notes).
Closing asks KiCad to exit and waits for its lock files to disappear.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

KICAD_IMAGES = ("kicad.exe", "eeschema.exe", "pcbnew.exe")


def lock_files(project_dir: Path) -> list[Path]:
    """Lock files KiCad holds in the project, after discarding stale ones from a crashed KiCad."""
    locks = sorted(project_dir.glob("~*.lck"))
    if locks and not kicad_processes():
        for p in locks:
            p.unlink(missing_ok=True)
        print(f"removed {len(locks)} stale lock file(s): no KiCad process holds them")
        return []
    return locks


def kicad_processes() -> list[str]:
    """The running KiCad processes, by name."""
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    names = []
    for line in out.splitlines():
        name = line.split('","')[0].strip('"').lower()
        if name in KICAD_IMAGES:
            names.append(name)
    return names


def status(project_dir: Path) -> int:
    """Print the project's lock files and the KiCad processes; the exit code of build.py --status."""
    procs = kicad_processes()
    locks = lock_files(project_dir)
    print(f"KiCad processes: {', '.join(procs) if procs else 'none'}")
    print(f"lock files in {project_dir.name}: {', '.join(p.name for p in locks) if locks else 'none'}")
    print("build target: " + ("staging (KiCad holds the project)" if locks else "the project itself"))
    return 0


def close_kicad(project_dir: Path, timeout_s: float = 60.0) -> bool:
    """Ask KiCad to close and wait until it and its lock files are gone."""
    if not kicad_processes():
        lock_files(project_dir)  # clears stale locks
        print("KiCad is not running")
        return True
    subprocess.run(["taskkill", "/IM", "kicad.exe"], capture_output=True, text=True)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not kicad_processes():
            break
        time.sleep(1.0)
    if kicad_processes():
        print(f"KiCad did not close within {timeout_s:.0f} s. It is probably asking about unsaved changes; discard them "
              "(the generators are the source of truth) or save them elsewhere, then retry.")
        return False
    lock_files(project_dir)
    print("KiCad closed")
    return True


def open_kicad(task_name: str) -> bool:
    """Open KiCad on the user's desktop through the scheduled task they created once."""
    if not task_name:
        print("no scheduled task is configured for this project (Project.gui_task); open KiCad yourself")
        return False
    r = subprocess.run(["schtasks", "/Run", "/TN", task_name], capture_output=True, text=True)
    if r.returncode == 0:
        print(f"asked Windows to run the task '{task_name}': KiCad opens on your desktop")
        return True
    print(f"could not run the scheduled task '{task_name}': {(r.stderr or r.stdout).strip()}")
    print("create it once from your own terminal; the command is in the project's WORKFLOW.md")
    return False
