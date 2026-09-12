"""The legacy SWIG pcbnew module must never be imported. It dies with KiCad 11."""

import re
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"
PATTERN = re.compile(r"^\s*(import\s+pcbnew|from\s+pcbnew\b)", re.MULTILINE)


def test_no_pcbnew_import_anywhere():
    offenders = []
    for py in SRC.rglob("*.py"):
        if PATTERN.search(py.read_text(encoding="utf-8")):
            offenders.append(str(py))
    assert not offenders, f"SWIG pcbnew imported in: {offenders}"
