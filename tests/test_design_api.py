"""docs/design-api.md is generated from the code and must be current: a stale sheet teaches the wrong API."""

from __future__ import annotations

from kicad_layer.design import api


def test_api_sheet_is_current():
    generated = api.generate()
    on_disk = api.DOC_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert on_disk == generated, "docs/design-api.md is stale: run  python -m kicad_layer.design.api"


def test_api_sheet_covers_what_a_sheet_author_uses():
    text = api.generate()
    for name in ("### Circuit", "### Layout", "### Board", "### Project", "### ModuleSheet", "| `C_100N` |", "`net(*pins: Pin, name: str | None = None) -> Net`"):
        assert name in text, name
    assert len(text) // 4 < 12000, "the sheet should stay a few thousand tokens; trim SECTIONS before it grows"
