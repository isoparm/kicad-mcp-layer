"""The document index finds headings, captions and contents lines; fact sheets answer by section."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from kicad_layer import docs
from kicad_layer.config import load_settings, set_settings


def _pdf_lines(pages: list[list[str]]) -> bytes:
    """A PDF with one text line per Tj, several pages, made with pypdf."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    w = PdfWriter()
    for lines in pages:
        page = w.add_blank_page(width=300, height=400)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
        body = "BT /F1 12 Tf 20 380 Td " + " ".join(f"({l}) Tj 0 -18 Td" for l in lines) + " ET"
        content = DecodedStreamObject()
        content.set_data(body.encode("latin-1"))
        page[NameObject("/Contents")] = w._add_object(content)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@pytest.fixture
def library(tmp_path, monkeypatch):
    lib = tmp_path / "references"
    lib.mkdir()
    monkeypatch.setenv("KICAD_LAYER_PARTS_DIR", str(tmp_path / "parts"))
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_DOCS_DIR": str(lib), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    return lib


def test_sections_come_from_contents_headings_and_captions(library, tmp_path):
    pdf = tmp_path / "ds.pdf"
    pdf.write_bytes(_pdf_lines([
        ["Table of Contents", "Pin Description ............ 2", "Electrical Characteristics ...... 3"],
        ["Pin Description", "1 SD_MODE shutdown", "Table 1. Pin functions", "2 Detailed Description"],
        ["Electrical Characteristics", "Figure 3. Test circuit", "see Table 1 again"],
    ]))
    found = docs.sections(pdf)
    by = {(s["kind"], s["page"]): s["title"] for s in found}
    assert by[("contents", 2)] == "Pin Description" and by[("contents", 3)] == "Electrical Characteristics"
    assert by[("heading", 2)] in ("Pin Description", "2 Detailed Description")
    assert any(s["kind"] == "table" and s["page"] == 2 and s["title"].startswith("Table 1.") for s in found)
    assert any(s["kind"] == "figure" and s["page"] == 3 for s in found)
    assert not any(s["title"].startswith("Table 1") and s["page"] == 3 for s in found)  # a mention is not a caption
    assert docs.sections(pdf) == found  # cached


def test_fact_sheet_by_section_and_the_advice_without_one(library, tmp_path):
    parts = tmp_path / "parts"
    parts.mkdir()
    (parts / "MAX98357A-MAX98357B.md").write_text("# MAX98357A\n\n## Pins\n| 4 | SD_MODE | shutdown | p.15 |\n\n## Values\nB0 0.16 V typ (p.7)\n", encoding="utf-8")
    path, text, headings = docs.fact_sheet("MAX98357A", "values")
    assert path is not None and text == "## Values\nB0 0.16 V typ (p.7)" and headings == ["Pins", "Values"]
    assert docs.fact_sheet("max98357a")[1].startswith("# MAX98357A")
    assert docs.fact_sheet("MAX98357A", "package")[1] == ""
    assert docs.fact_sheet("MAX98357A", None, "B0|sd_mode")[1] == "[Pins] | 4 | SD_MODE | shutdown | p.15 |\n[Values] B0 0.16 V typ (p.7)"
    assert docs.fact_sheet("MAX98357A", "values", "B0")[1] == "[Values] B0 0.16 V typ (p.7)"
    none, _, _ = docs.fact_sheet("NOPE")
    assert none is None and "Write one at" in docs.fact_sheet_brief("NOPE", None)


def test_cli_facts_and_sections(library, tmp_path, capsys):
    (tmp_path / "parts").mkdir()
    (tmp_path / "parts" / "ASEK.md").write_text("# ASEK\n\n## Pins\n| 1 | INH | open or high runs | p.3 |\n", encoding="utf-8")
    assert docs.main(["facts", "ASEK", "pins"]) == 0
    assert "INH" in capsys.readouterr().out
    assert docs.main(["facts", "NOPE"]) == 1
    pdf = tmp_path / "ds.pdf"
    pdf.write_bytes(_pdf_lines([["Pin Description", "Table 1. Pins"]]))
    assert docs.main(["sections", str(pdf), "--find", "pin"]) == 0
    assert "Table 1" in capsys.readouterr().out
