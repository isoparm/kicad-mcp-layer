"""One PNG per sheet, on demand: kicad-cli's PDF of the project, split by sheet with PyMuPDF.

kicad-cli 10 exports schematics to PDF and SVG only; PyMuPDF (``pip install kicad-mcp-layer[preview]``)
rasterises the pages. Each page names its sheet in the title block, which is how pages are matched
to sheet names.
"""
from __future__ import annotations

import re
from pathlib import Path

from kicad_layer.cli import runner
from kicad_layer.cli.discovery import find_kicad_cli

from .project import Project


def preview(project: Project, sheets: list[str] | None = None, out_dir: Path | None = None, dpi: int = 100) -> list[Path]:
    """PNG files for the named sheets (``root`` for the root sheet), or every sheet when ``sheets`` is empty."""
    try:
        import pymupdf
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("previews need PyMuPDF: pip install kicad-mcp-layer[preview]") from e
    out = out_dir or project.dir / "_preview"
    out.mkdir(parents=True, exist_ok=True)
    root = project.dir / f"{project.name}.kicad_sch"
    pdf = out / f"{project.name}.pdf"
    cli = find_kicad_cli()
    r = runner.run([cli.path, "sch", "export", "pdf", "-o", str(pdf), str(root)], timeout_s=300, cwd=project.dir)
    if r.returncode != 0 or not pdf.is_file():
        raise RuntimeError(f"kicad-cli could not export the schematic PDF: {r.tail()}")
    wanted = set(sheets or [])
    written: list[Path] = []
    for page in pymupdf.open(pdf):
        m = re.search(r"Sheet:\s*(/\S*)", page.get_text())
        path = m.group(1) if m else "/"
        name = "root" if path == "/" else path.strip("/").replace("/", "-")
        if wanted and name not in wanted:
            continue
        png = out / f"{name}.png"
        page.get_pixmap(dpi=dpi).save(png)
        written.append(png)
    missing = wanted - {p.stem for p in written}
    if missing:
        raise ValueError(f"no such sheet: {', '.join(sorted(missing))}")
    return written
