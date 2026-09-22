"""ERC and DRC through kicad-cli, parsed into verdict reports.

Two KiCad facts shape this module:

* DRC JSON has three arrays, ``violations``, ``unconnected_items`` and ``schematic_parity``.
  All three count. A board with no clearance errors but forty unrouted nets is not PASS.
* ERC JSON on KiCad 10.0.x reports positions a hundred times too small: the writer uses the
  board's internal unit scale instead of the schematic's. Positions are corrected here and
  findings are keyed on item UUIDs, which are right.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Literal

from kicad_layer.cli import runner
from kicad_layer.cli.discovery import find_kicad_cli
from kicad_layer.config import settings
from kicad_layer.models import Finding, FindingItem, FindingsSummary, UnconnectedPair, VerdictReport, WorstFinding
from kicad_layer.paths import display
from kicad_layer.project import board_rule_warnings

Severity = Literal["default", "all", "error", "warning"]

_SEVERITY_FLAGS: dict[str, list[str]] = {
    "default": [],
    "all": ["--severity-all"],
    "error": ["--severity-error"],
    "warning": ["--severity-warning"],
}

MAX_FINDINGS = 200  # a big board's full list runs to hundreds of KB, more than an MCP client takes
SUMMARY_TOP = 20


def finding_id(rule: str, first_uuid: str | None, description: str) -> str:
    raw = f"{rule}|{first_uuid or ''}|{description}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:12]


def _items(raw_items: list[dict[str, Any]], scale: float) -> list[FindingItem]:
    out: list[FindingItem] = []
    for it in raw_items or []:
        pos = it.get("pos") or {}
        x = pos.get("x")
        y = pos.get("y")
        out.append(
            FindingItem(
                uuid=it.get("uuid"),
                description=it.get("description", ""),
                x_mm=round(x * scale, 4) if isinstance(x, (int, float)) else None,
                y_mm=round(y * scale, 4) if isinstance(y, (int, float)) else None,
            )
        )
    return out


def _finding(raw: dict[str, Any], category: str, sheet: str | None, scale: float) -> Finding:
    items = _items(raw.get("items") or [], scale)
    severity = raw.get("severity", "error")
    if severity not in ("error", "warning", "exclusion", "ignore", "info"):
        severity = "error"
    rule = raw.get("type", "unknown")
    description = raw.get("description", "")
    return Finding(
        id=finding_id(rule, items[0].uuid if items else None, description),
        category=category,  # type: ignore[arg-type]
        type=rule,
        severity=severity,  # type: ignore[arg-type]
        description=description,
        excluded=bool(raw.get("excluded", False)),
        comment=raw.get("comment") or None,
        sheet=sheet,
        items=items,
    )


def _erc_position_scale(data: dict[str, Any]) -> tuple[float, str | None]:
    """KiCad 10.0.x writes ERC positions in the wrong unit scale (100x too small)."""
    version = str(data.get("kicad_version", ""))
    if version.startswith("10."):
        return 100.0, (
            "ERC JSON positions were multiplied by 100: KiCad 10.0.x writes them with the "
            "board unit scale instead of the schematic one (eeschema/erc/erc_report.cpp). "
            "Item UUIDs are reliable."
        )
    return 1.0, None


def parse_erc(data: dict[str, Any]) -> tuple[list[Finding], list[str]]:
    scale, note = _erc_position_scale(data)
    findings: list[Finding] = []
    for sheet in data.get("sheets") or []:
        path = sheet.get("path")
        for raw in sheet.get("violations") or []:
            findings.append(_finding(raw, "violation", path, scale))
    has_positions = any(it.x_mm is not None for f in findings for it in f.items)
    return findings, [note] if (note and has_positions) else []


def parse_drc(data: dict[str, Any]) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    for raw in data.get("violations") or []:
        findings.append(_finding(raw, "violation", None, 1.0))
    for raw in data.get("unconnected_items") or []:
        findings.append(_finding(raw, "unconnected", None, 1.0))
    for raw in data.get("schematic_parity") or []:
        findings.append(_finding(raw, "parity", None, 1.0))
    return findings, []


def summarize(findings: list[Finding]) -> tuple[str, dict[str, int]]:
    active = [f for f in findings if not f.excluded]
    counts = {
        "errors": sum(1 for f in active if f.severity == "error"),
        "warnings": sum(1 for f in active if f.severity == "warning"),
        "excluded": sum(1 for f in findings if f.excluded),
        "unconnected": sum(1 for f in active if f.category == "unconnected"),
        "parity": sum(1 for f in active if f.category == "parity"),
        "total": len(findings),
    }
    if counts["errors"]:
        verdict = "FAIL"
    elif counts["warnings"]:
        verdict = "WARN"
    else:
        verdict = "PASS"
    return verdict, counts


_MEASURE_RE = re.compile(r"(?P<kw>\w+)?\s*(?P<req>-?\d+(?:\.\d+)?)\s*mm;\s*actual\s*(?P<act>-?\d+(?:\.\d+)?)\s*mm", re.IGNORECASE)


def rule_of(description: str) -> str | None:
    """The constraint a DRC description names: rule:<name>, netclass:<name>, or board for board setup."""
    m = re.search(r"\b(rule|netclass) '([^']+)'", description)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    if re.search(r"\(board (?:setup|minimum)", description):
        return "board"
    return None


def measure(description: str) -> tuple[float | None, float | None, float | None]:
    """(required, actual, deficit) from '... 0.3000 mm; actual 0.2000 mm'; deficit > 0 means the rule is missed.

    A maximum constraint ('max 1.0 mm; actual 1.2 mm') misses by actual - required, every other one by
    required - actual."""
    m = _MEASURE_RE.search(description)
    if not m:
        return None, None, None
    req, act = float(m.group("req")), float(m.group("act"))
    head = description[: m.start("req")].lower()
    is_max = bool(re.search(r"\bmax(?:imum)?\b[^;(]*$", head))
    return req, act, round((act - req) if is_max else (req - act), 4)


def summarize_findings(findings: list[Finding], top: int = SUMMARY_TOP) -> FindingsSummary:
    """Counts per type, rule and severity, the worst ``top`` violations and up to ``top`` unconnected pairs."""
    active = [f for f in findings if not f.excluded]
    by_type: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    ranked: list[tuple[int, float, int, int, WorstFinding]] = []
    for i, f in enumerate(active):
        by_type[f.type] = by_type.get(f.type, 0) + 1
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        rule = rule_of(f.description)
        if rule:
            by_rule[rule] = by_rule.get(rule, 0) + 1
        if f.category == "unconnected":
            continue
        req, act, deficit = measure(f.description)
        first = f.items[0] if f.items else None
        wf = WorstFinding(id=f.id, type=f.type, severity=f.severity, rule=rule, description=f.description, required_mm=req, actual_mm=act,
                          deficit_mm=deficit, items=[it.description for it in f.items],
                          x_mm=first.x_mm if first else None, y_mm=first.y_mm if first else None)
        ranked.append((0 if deficit is not None else 1, -(deficit or 0.0), 0 if f.severity == "error" else 1, i, wf))
    ranked.sort(key=lambda t: t[:4])
    unconnected = [f for f in active if f.category == "unconnected"]
    pairs = [UnconnectedPair(a=f.items[0].description if f.items else f.description, b=f.items[1].description if len(f.items) > 1 else None,
                             x_mm=f.items[0].x_mm if f.items else None, y_mm=f.items[0].y_mm if f.items else None) for f in unconnected[:top]]
    return FindingsSummary(by_type=dict(sorted(by_type.items(), key=lambda kv: -kv[1])), by_rule=dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
                           by_severity=by_severity, worst=[t[4] for t in ranked[:top]], unconnected=len(unconnected), unconnected_pairs=pairs)


def _report_path(kind: str, source: Path) -> Path:
    reports = settings().cache_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    stamp = hashlib.sha256(str(source).encode("utf-8", "replace")).hexdigest()[:10]
    return reports / f"{source.stem}-{stamp}-{kind}.json"


def _load_report(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return json.load(fh)


def _build(
    kind: str,
    source: Path,
    result: runner.CliResult,
    report_path: Path,
    parse,
    *,
    summary: bool = False,
    top: int = SUMMARY_TOP,
) -> VerdictReport:
    notes: list[str] = []
    if result.returncode not in (runner.EXIT_OK, runner.EXIT_VIOLATIONS):
        return VerdictReport(
            verdict="BLOCKED",
            kind=kind,  # type: ignore[arg-type]
            source=display(source),
            report_path=None,
            command=result.command,
            exit_code=result.returncode,
            duration_s=result.duration_s,
            notes=[f"kicad-cli exited with {result.returncode}.", result.tail()],
        )
    data = _load_report(report_path)
    if data is None:
        return VerdictReport(
            verdict="UNVERIFIED",
            kind=kind,  # type: ignore[arg-type]
            source=display(source),
            report_path=None,
            command=result.command,
            exit_code=result.returncode,
            duration_s=result.duration_s,
            notes=[
                "kicad-cli reported success but wrote no report file, so nothing can be claimed.",
                result.tail(),
            ],
        )
    findings, parse_notes = parse(data)
    notes.extend(parse_notes)
    verdict, counts = summarize(findings)
    digest = None
    if summary:
        digest = summarize_findings(findings, top)
        truncated = bool(findings)
        listed: list[Finding] = []
        if findings:
            notes.append(f"Summary of {len(findings)} findings; the full list is in the JSON report at {report_path}.")
    else:
        truncated = len(findings) > MAX_FINDINGS
        listed = findings[:MAX_FINDINGS]
        if truncated:
            notes.append(f"Only the first {MAX_FINDINGS} of {len(findings)} findings are listed; the full report is at {report_path}. "
                         "Call again with summary=true for counts per type and rule and the worst violations.")
    return VerdictReport(
        verdict=verdict,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        source=display(source),
        report_path=str(report_path),
        kicad_version=data.get("kicad_version"),
        date=data.get("date"),
        counts=counts,
        findings=listed,
        truncated=truncated,
        summary=digest,
        command=result.command,
        exit_code=result.returncode,
        duration_s=result.duration_s,
        notes=notes,
    )


def run_erc(root_schematic: Path, *, severity: Severity = "all", summary: bool = False, top: int = SUMMARY_TOP) -> VerdictReport:
    cli = find_kicad_cli()
    report_path = _report_path("erc", root_schematic)
    report_path.unlink(missing_ok=True)
    cmd = [
        cli.path, "sch", "erc",
        "--format", "json",
        "--units", "mm",
        *_SEVERITY_FLAGS[severity],
        "--exit-code-violations",
        "-o", report_path,
        root_schematic,
    ]
    result = runner.run(cmd, timeout_s=settings().cli_long_timeout_s, cwd=root_schematic.parent)
    return _build("erc", root_schematic, result, report_path, parse_erc, summary=summary, top=top)


def run_drc(
    board: Path,
    *,
    severity: Severity = "all",
    schematic_parity: bool = True,
    all_track_errors: bool = False,
    summary: bool = False,
    top: int = SUMMARY_TOP,
) -> VerdictReport:
    cli = find_kicad_cli()
    report_path = _report_path("drc", board)
    report_path.unlink(missing_ok=True)
    cmd: list[str | Path] = [
        cli.path, "pcb", "drc",
        "--format", "json",
        "--units", "mm",
        *_SEVERITY_FLAGS[severity],
        "--exit-code-violations",
    ]
    if schematic_parity:
        cmd.append("--schematic-parity")
    if all_track_errors:
        cmd.append("--all-track-errors")
    cmd += ["-o", report_path, board]
    started = time.monotonic()
    result = runner.run(cmd, timeout_s=settings().cli_long_timeout_s, cwd=board.parent)
    report = _build("drc", board, result, report_path, parse_drc, summary=summary, top=top)
    report.warnings = board_rule_warnings(board)
    if report.duration_s is None:
        report.duration_s = round(time.monotonic() - started, 3)
    return report
