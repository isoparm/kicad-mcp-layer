"""Exports and renders through kicad-cli.

Every result is verified by what actually appeared on disk after the command ran, not by
the exit code alone. Files are reported with size and sha256 so the model can tell a
fresh Gerber from a stale one.
"""

from __future__ import annotations

import csv
import hashlib
import time
from pathlib import Path

from kicad_layer.cli import runner
from kicad_layer.cli.discovery import find_kicad_cli
from kicad_layer.config import settings
from kicad_layer.errors import INVALID_ARGUMENT, KICAD_CLI_FAILED, LayerError
from kicad_layer.models import BomResult, BomRow, ExportResult, FileArtifact, RenderResult
from kicad_layer.paths import display, resolve_in_workspace


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def artifact(path: Path) -> FileArtifact:
    return FileArtifact(path=display(path), size=path.stat().st_size, sha256=_sha256(path))


def _new_files(directory: Path, since: float) -> list[Path]:
    out: list[Path] = []
    for p in sorted(directory.rglob("*")):
        if p.is_file() and p.stat().st_mtime >= since - 1.0:
            out.append(p)
    return out


def _output_dir(base: Path, output_dir: str | None, default_name: str) -> Path:
    if output_dir:
        candidate = Path(output_dir)
        target = candidate if candidate.is_absolute() else base / candidate
    else:
        target = base / default_name
    resolved = resolve_in_workspace(target, must_exist=False)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _warnings(result: runner.CliResult) -> list[str]:
    lines = (result.stdout + "\n" + result.stderr).splitlines()
    return [ln.strip() for ln in lines if "warn" in ln.lower() or "error" in ln.lower()][:20]


def export_fab(
    board: Path,
    *,
    output_dir: str | None = None,
    gerbers: bool = True,
    drill: bool = True,
    position: bool = True,
    step: bool = False,
    pdf: bool = False,
    layers: list[str] | None = None,
    exclude_dnp: bool = False,
) -> ExportResult:
    cli = find_kicad_cli()
    out = _output_dir(board.parent, output_dir, "fab")
    started = time.time()
    commands: list[list[str]] = []
    warnings: list[str] = []
    skipped: list[str] = []
    long_timeout = settings().cli_long_timeout_s

    def run(cmd: list, label: str) -> None:
        result = runner.run(cmd, timeout_s=long_timeout, cwd=board.parent)
        commands.append(result.command)
        warnings.extend(_warnings(result))
        if not result.ok:
            raise LayerError(
                KICAD_CLI_FAILED,
                f"{label} export failed with exit {result.returncode}.",
                data={"output": result.tail(), "command": result.command},
            )

    if gerbers:
        cmd = [cli.path, "pcb", "export", cli.pcb_export_verb("gerbers", "gerber"), "-o", out]
        if layers:
            cmd += ["--layers", ",".join(layers)]
        run(cmd + [board], "Gerber")
    else:
        skipped.append("gerbers")
    if drill:
        run([cli.path, "pcb", "export", "drill", "-o", out, "--format", "excellon", "--excellon-units", "mm", "--generate-map", "--map-format", "pdf", board], "Drill")
    else:
        skipped.append("drill")
    if position:
        dnp = ["--exclude-dnp"] if exclude_dnp else []  # footprints carrying KiCad's do-not-populate attribute
        run([cli.path, "pcb", "export", cli.pcb_export_verb("pos", "positions"), "-o", out / f"{board.stem}-pos.csv", "--format", "csv", "--units", "mm", "--side", "both", *dnp, board], "Position")
    else:
        skipped.append("position")
    if step:
        run([cli.path, "pcb", "export", "step", "-o", out / f"{board.stem}.step", "--force", "--subst-models", board], "STEP")
    else:
        skipped.append("step")
    if pdf:
        run([cli.path, "pcb", "export", "pdf", "-o", out, "--layers", "F.Cu,B.Cu,F.SilkS,B.SilkS,Edge.Cuts", "--mode-multi", board], "PDF")
    else:
        skipped.append("pdf")

    files = [artifact(p) for p in _new_files(out, started)]
    return ExportResult(
        output_dir=display(out),
        files=files,
        commands=commands,
        warnings=warnings,
        skipped=skipped,
        duration_s=round(time.time() - started, 3),
    )


DEFAULT_BOM_FIELDS = ["Reference", "Value", "Footprint", "${QUANTITY}", "${DNP}"]


def export_bom(
    root_schematic: Path,
    *,
    fields: list[str] | None = None,
    group_by: list[str] | None = None,
    output_path: str | None = None,
    max_rows: int = 500,
    exclude_dnp: bool = False,
) -> BomResult:
    cli = find_kicad_cli()
    if output_path:
        target = Path(output_path)
        csv_path = resolve_in_workspace(target if target.is_absolute() else root_schematic.parent / target, must_exist=False)
    else:
        csv_path = root_schematic.parent / f"{root_schematic.stem}-bom.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    use_fields = fields or DEFAULT_BOM_FIELDS
    use_group = group_by if group_by is not None else ["Value", "Footprint"]
    cmd: list = [cli.path, "sch", "export", "bom", "--fields", ",".join(use_fields)]
    if use_group:
        cmd += ["--group-by", ",".join(use_group)]
    if exclude_dnp:
        cmd.append("--exclude-dnp")  # symbols marked do-not-populate
    cmd += ["--sort-field", "Reference", "-o", csv_path, root_schematic]
    result = runner.run(cmd, timeout_s=settings().cli_long_timeout_s, cwd=root_schematic.parent)
    if not result.ok or not csv_path.is_file():
        raise LayerError(
            KICAD_CLI_FAILED,
            f"BOM export failed with exit {result.returncode}.",
            data={"output": result.tail()},
        )
    with csv_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    columns = rows[0] if rows else []
    body = rows[1:]
    parsed = [BomRow(values=dict(zip(columns, r))) for r in body[:max_rows]]
    return BomResult(
        csv_path=display(csv_path),
        columns=columns,
        rows=parsed,
        row_count=len(body),
        truncated=len(body) > max_rows,
        command=result.command,
    )


def render_board(
    board: Path,
    *,
    side: str = "top",
    width: int = 1600,
    height: int = 900,
    quality: str = "basic",
    background: str = "default",
    output_path: str | None = None,
) -> tuple[Path, list[str]]:
    if side not in ("top", "bottom", "left", "right", "front", "back"):
        raise LayerError(INVALID_ARGUMENT, f"side must be top, bottom, left, right, front or back, not {side!r}.")
    if quality not in ("basic", "high", "user", "job_settings"):
        raise LayerError(INVALID_ARGUMENT, f"quality must be basic, high, user or job_settings, not {quality!r}.")
    cli = find_kicad_cli()
    if output_path:
        target = Path(output_path)
        png = resolve_in_workspace(target if target.is_absolute() else board.parent / target, must_exist=False)
    else:
        png = board.parent / "renders" / f"{board.stem}-{side}.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        cli.path, "pcb", "render", "-o", png,
        "-w", str(int(width)), "--height", str(int(height)),
        "--side", side, "--quality", quality, "--background", background,
        board,
    ]
    result = runner.run(cmd, timeout_s=settings().cli_long_timeout_s, cwd=board.parent)
    if not result.ok or not png.is_file():
        raise LayerError(
            KICAD_CLI_FAILED,
            f"Render failed with exit {result.returncode}.",
            data={"output": result.tail()},
        )
    return png, result.command


def render_schematic(
    root_schematic: Path,
    *,
    fmt: str = "svg",
    output_dir: str | None = None,
) -> RenderResult:
    if fmt not in ("svg", "pdf"):
        raise LayerError(INVALID_ARGUMENT, f"format must be svg or pdf, not {fmt!r}.")
    cli = find_kicad_cli()
    out = _output_dir(root_schematic.parent, output_dir, "renders")
    started = time.time()
    if fmt == "pdf":
        target: Path = out / f"{root_schematic.stem}.pdf"
        cmd: list = [cli.path, "sch", "export", "pdf", "-o", target, root_schematic]
    else:
        cmd = [cli.path, "sch", "export", "svg", "-o", out, root_schematic]
    result = runner.run(cmd, timeout_s=settings().cli_long_timeout_s, cwd=root_schematic.parent)
    if not result.ok:
        raise LayerError(
            KICAD_CLI_FAILED,
            f"Schematic {fmt} export failed with exit {result.returncode}.",
            data={"output": result.tail()},
        )
    files = [artifact(p) for p in _new_files(out, started) if p.suffix.lower() == f".{fmt}"]
    if not files:
        raise LayerError(KICAD_CLI_FAILED, f"kicad-cli exited 0 but wrote no .{fmt} file into {display(out)}.")
    note = (
        "One file per sheet. Open them with an image or PDF viewer, or ask the client to read the file, "
        "to see the drawing."
    )
    return RenderResult(files=files, format=fmt, command=result.command, note=note)
