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
import time
from pathlib import Path
from typing import Any, Literal

from kicad_layer.cli import runner
from kicad_layer.cli.discovery import find_kicad_cli
from kicad_layer.config import settings
from kicad_layer.models import Finding, FindingItem, VerdictReport
from kicad_layer.paths import display

Severity = Literal["default", "all", "error", "warning"]

_SEVERITY_FLAGS: dict[str, list[str]] = {
    "default": [],
    "all": ["--severity-all"],
    "error": ["--severity-error"],
    "warning": ["--severity-warning"],
}

MAX_FINDINGS = 400


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
    truncated = len(findings) > MAX_FINDINGS
    if truncated:
        notes.append(f"Only the first {MAX_FINDINGS} of {len(findings)} findings are listed; the full report is at {report_path}.")
    return VerdictReport(
        verdict=verdict,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        source=display(source),
        report_path=str(report_path),
        kicad_version=data.get("kicad_version"),
        date=data.get("date"),
        counts=counts,
        findings=findings[:MAX_FINDINGS],
        truncated=truncated,
        command=result.command,
        exit_code=result.returncode,
        duration_s=result.duration_s,
        notes=notes,
    )


def run_erc(root_schematic: Path, *, severity: Severity = "all") -> VerdictReport:
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
    return _build("erc", root_schematic, result, report_path, parse_erc)


def run_drc(
    board: Path,
    *,
    severity: Severity = "all",
    schematic_parity: bool = True,
    all_track_errors: bool = False,
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
    report = _build("drc", board, result, report_path, parse_drc)
    if report.duration_s is None:
        report.duration_s = round(time.monotonic() - started, 3)
    return report
