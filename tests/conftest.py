from __future__ import annotations

import os
from pathlib import Path

import pytest

from kicad_layer.config import load_settings, set_settings

FIXTURES = Path(__file__).parent / "fixtures"
DATA = Path(__file__).parent / "data"


def _kicad_cli_available() -> bool:
    try:
        from kicad_layer.cli.discovery import find_kicad_cli

        set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(FIXTURES)}))
        return find_kicad_cli().major == 10
    except Exception:
        return False
    finally:
        set_settings(None)


HAVE_KICAD_CLI = _kicad_cli_available()
real_kicad = pytest.mark.skipif(not HAVE_KICAD_CLI, reason="kicad-cli 10.x not found on this machine")


def copy_project(name: str, dst: Path) -> Path:
    """A working copy of a fixture project without KiCad's transient files. kicad-cli in another worker
    may be creating or removing a lock or a .kicad_prl in the source while we copy, so one retry."""
    import shutil
    import time

    ignore = shutil.ignore_patterns("*-backups", ".history", "~*.lck", "*.kicad_prl", "_autosave-*")
    for attempt in range(2):
        try:
            shutil.copytree(FIXTURES / name, dst, ignore=ignore, dirs_exist_ok=attempt > 0)
            return dst
        except shutil.Error:
            if attempt:
                raise
            time.sleep(0.5)
    return dst


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def workspace(tmp_path_factory) -> Path:
    """Settings pointed at the fixture corpus, with a throwaway cache directory."""
    cache = tmp_path_factory.mktemp("cache")
    set_settings(
        load_settings(
            {
                "KICAD_LAYER_WORKSPACE": str(FIXTURES),
                "KICAD_LAYER_CACHE_DIR": str(cache),
                "KICAD_CLI": os.environ.get("KICAD_CLI", ""),
            }
        )
    )
    yield FIXTURES
    set_settings(None)


@pytest.fixture
async def client(workspace):
    from mcp import Client

    from kicad_layer.server import build_server

    async with Client(build_server(tier="full"), raise_exceptions=True) as c:
        yield c
