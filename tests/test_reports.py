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


def test_summary_counts_types_rules_and_ranks_by_deficit():
    from kicad_layer.cli.reports import summarize_findings

    data = load("drc-multichannel.json")
    data["violations"].append({"type": "clearance", "severity": "error", "excluded": False,
                               "description": "Clearance violation (rule 'HV_creepage' clearance 2.5000 mm; actual 1.1000 mm)",
                               "items": [{"uuid": "u1", "description": "Pad 1 [HV] of J1 on F.Cu", "pos": {"x": 10.0, "y": 20.0}}]})
    data["unconnected_items"] = [{"type": "unconnected_items", "severity": "error", "description": "Missing connection between items",
                                  "items": [{"uuid": f"a{i}", "description": f"Pad {i} [GND] of U1", "pos": {"x": 1.0, "y": 2.0}},
                                            {"uuid": f"b{i}", "description": f"Pad {i} [GND] of U2", "pos": {"x": 3.0, "y": 4.0}}]} for i in range(30)]
    findings, _ = parse_drc(data)
    s = summarize_findings(findings, top=5)
    assert s.by_type["lib_footprint_mismatch"] == 81 and s.by_type["clearance"] == 13
    assert s.by_rule["rule:HV_creepage"] == 1 and s.by_rule["board"] == 16
    assert s.worst[0].rule == "rule:HV_creepage" and s.worst[0].deficit_mm == 1.4
    assert s.worst[0].required_mm == 2.5 and s.worst[0].actual_mm == 1.1 and s.worst[0].x_mm == 10.0
    assert len(s.worst) == 5 and all(w.deficit_mm is not None for w in s.worst)
    assert s.unconnected == 30 and len(s.unconnected_pairs) == 5
    assert s.unconnected_pairs[0].a.startswith("Pad 0") and s.unconnected_pairs[0].b.endswith("U2")


def test_measure_reads_minimum_and_maximum_constraints():
    from kicad_layer.cli.reports import measure, rule_of

    assert measure("Track width (netclass 'PWR' min width 0.5000 mm; actual 0.2500 mm)") == (0.5, 0.25, 0.25)
    assert measure("Track width (rule 'short' max width 1.0000 mm; actual 1.2000 mm)") == (1.0, 1.2, 0.2)
    assert measure("Silkscreen clipped by solder mask") == (None, None, None)
    assert rule_of("Clearance violation (netclass 'Default' clearance 0.2 mm; actual 0.1 mm)") == "netclass:Default"
    assert rule_of("Silkscreen clipped by solder mask") is None


def test_build_summary_mode_drops_the_list_and_keeps_the_report_path(tmp_path):
    from kicad_layer.cli import reports, runner

    report = tmp_path / "drc.json"
    report.write_text((DATA / "drc-multichannel.json").read_text(encoding="utf-8"), encoding="utf-8")
    res = runner.CliResult(command=["kicad-cli"], returncode=runner.EXIT_VIOLATIONS, stdout="", stderr="", duration_s=1.0)
    full = reports._build("drc", tmp_path / "b.kicad_pcb", res, report, parse_drc)
    assert len(full.findings) == 103 and not full.truncated and full.summary is None
    short = reports._build("drc", tmp_path / "b.kicad_pcb", res, report, parse_drc, summary=True, top=3)
    assert short.findings == [] and short.truncated and short.report_path == str(report)
    assert short.counts == full.counts and len(short.summary.worst) == 3
    assert len(short.model_dump_json()) < len(full.model_dump_json()) / 10


def test_full_list_is_capped(tmp_path, monkeypatch):
    from kicad_layer.cli import reports, runner

    monkeypatch.setattr(reports, "MAX_FINDINGS", 50)
    report = tmp_path / "drc.json"
    report.write_text((DATA / "drc-multichannel.json").read_text(encoding="utf-8"), encoding="utf-8")
    res = runner.CliResult(command=["kicad-cli"], returncode=runner.EXIT_VIOLATIONS, stdout="", stderr="", duration_s=1.0)
    out = reports._build("drc", tmp_path / "b.kicad_pcb", res, report, parse_drc)
    assert len(out.findings) == 50 and out.truncated and out.counts["total"] == 103
    assert any("summary=true" in n for n in out.notes)
