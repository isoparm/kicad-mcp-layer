"""Parsing of real kicad-cli 10.0.6 ERC and DRC JSON, saved under tests/data."""

import json
from pathlib import Path

from kicad_layer.cli.reports import parse_drc, parse_erc, summarize

DATA = Path(__file__).parent / "data"


def load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def test_clean_erc_is_pass():
    findings, notes = parse_erc(load("erc-complex_hierarchy.json"))
    verdict, counts = summarize(findings)
    assert verdict == "PASS"
    assert counts["total"] == 0


def test_erc_with_errors_is_fail_and_positions_are_corrected():
    findings, notes = parse_erc(load("erc-multichannel.json"))
    verdict, counts = summarize(findings)
    assert verdict == "FAIL"
    assert counts["errors"] >= 1 and counts["warnings"] >= 1
    assert any("multiplied by 100" in n for n in notes)
    pwr = next(f for f in findings if f.type == "power_pin_not_driven")
    assert pwr.sheet is not None
    assert pwr.items[0].uuid
    # KiCad wrote 0.8636 mm for a symbol that sits at 86.36 mm.
    assert pwr.items[0].x_mm and pwr.items[0].x_mm > 10


def test_finding_ids_are_stable():
    a, _ = parse_erc(load("erc-multichannel.json"))
    b, _ = parse_erc(load("erc-multichannel.json"))
    assert [f.id for f in a] == [f.id for f in b]
    assert len({f.id for f in a}) == len(a), "ids must be unique within one report"


def test_clean_drc_is_pass():
    findings, _ = parse_drc(load("drc-complex_hierarchy.json"))
    assert summarize(findings)[0] == "PASS"


def test_drc_with_clearance_errors_is_fail():
    findings, _ = parse_drc(load("drc-multichannel.json"))
    verdict, counts = summarize(findings)
    assert verdict == "FAIL"
    # kicad-cli 10.0.6 on the multichannel demo: 12 clearance-class errors, 91 warnings.
    assert counts["errors"] == 12
    assert counts["warnings"] == 91
    assert counts["total"] == 103
    assert all(f.category in ("violation", "unconnected", "parity") for f in findings)
    clearance = next(f for f in findings if f.type == "clearance")
    assert clearance.items and clearance.items[0].x_mm and clearance.items[0].x_mm > 10


def test_unconnected_items_count_as_errors():
    data = load("drc-complex_hierarchy.json")
    data["unconnected_items"] = [
        {
            "type": "unconnected_items",
            "severity": "error",
            "description": "Missing connection between items",
            "items": [{"uuid": "abc", "description": "Pad 1 [GND] of R1", "pos": {"x": 1.0, "y": 2.0}}],
        }
    ]
    findings, _ = parse_drc(data)
    verdict, counts = summarize(findings)
    assert verdict == "FAIL"
    assert counts["unconnected"] == 1
