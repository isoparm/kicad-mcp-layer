"""A turnkey order package for PCBWay from a finished build: Gerbers, drill files, the pick-and-place
file and a BOM in PCBWay's assembly template layout (Item, Designator, Qty, Manufacturer, Mfg Part #,
Description/Value, Package, Type (SMD/THT), Your notes).

Manufacturer and part number come from the schematic fields the catalogue's parts stamp on every
symbol during the build, so the BOM is only ever as current as the build it reads. The package
refuses a BOM with an empty part number (exit code 1).

A project's script calls ``main(PROJECT, argv)`` with ``[--build DIR] [--out DIR]``. ``--build`` is
the folder holding the built .kicad_pcb and .kicad_sch (default: the project itself, which needs
KiCad's files on disk to be saved); ``--out`` defaults to <project>/fab/pcbway, which git ignores
because it is generated.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import zipfile
from pathlib import Path

from kicad_layer.cli.exports import export_bom, export_fab
from kicad_layer.config import load_settings, set_settings

from .project import Project

PCBWAY_COLUMNS = ["Item", "Designator", "Qty", "Manufacturer", "Mfg Part #", "Description/Value", "Package", "Type (SMD/THT)", "Your notes"]
BOM_FIELDS = ["Reference", "Value", "Footprint", "Manufacturer", "MPN", "Package", "PCBWay Note", "${QUANTITY}", "${DNP}"]
# what goes into the Gerber archive for PCBWay: copper, mask, paste, silkscreen, outline, drill, drill map, job file
# (KiCad writes Protel extensions: .gtl .gbl .g1 .g2 .gts .gbs .gtp .gbp .gto .gbo .gm1)
ARCHIVE_PATTERN = re.compile(r"-(?:F|B|In\d+)_(?:Cu|Mask|Paste|Silkscreen)\.|-Edge_Cuts\.|\.drl$|-drl_map\.pdf$|\.gbrjob$")


def footprint_attrs(pcb: Path) -> dict[str, str]:
    """Reference -> the footprint's ``attr`` line (smd, through_hole, exclude_from_bom ...) read from the board file."""
    text = pcb.read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(r'\(footprint "', text)] + [len(text)]
    attrs: dict[str, str] = {}
    for a, b in zip(starts, starts[1:]):
        block = text[a:b]
        ref = re.search(r'\(property "Reference" "([^"]+)"', block)
        attr = re.search(r"\(attr ([^)]*)\)", block)
        if ref:
            attrs[ref.group(1)] = attr.group(1) if attr else ""
    return attrs


def expand_refs(designators: str) -> list[str]:
    """kicad-cli writes grouped references as ``C1,C5-C7,C10``; expand the ranges."""
    out: list[str] = []
    for part in designators.split(","):
        part = part.strip()
        m = re.fullmatch(r"([A-Za-z_]+)(\d+)-([A-Za-z_]+)(\d+)", part)
        if m and m.group(1) == m.group(3):
            out.extend(f"{m.group(1)}{i}" for i in range(int(m.group(2)), int(m.group(4)) + 1))
        elif part:
            out.append(part)
    return out


def package_name(footprint: str, explicit: str) -> str:
    """PCBWay's Package column: the explicit Package field when the table gives one, else a readable form of the footprint name."""
    if explicit:
        return explicit
    name = footprint.split(":", 1)[-1]
    m = re.match(r"^(?:C|R|L|LED|D)_(\d{4})_\d{4}Metric", name)
    if m:
        return m.group(1)
    return name


def mount_type(attr: str) -> str:
    """PCBWay's Type column from the footprint's attr line: THT or SMD."""
    if "through_hole" in attr:
        return "THT"
    if "smd" in attr:
        return "SMD"
    return "SMD" if attr == "" else attr


def pcbway_package(name: str, build: Path, out: Path) -> int:
    """Write the package for project ``name`` from the build in ``build`` into ``out``. 0 when complete, 1 when a BOM
    line has no part number, 2 when the build is missing."""
    board = build / f"{name}.kicad_pcb"
    root_sch = build / f"{name}.kicad_sch"
    for p in (board, root_sch):
        if not p.is_file():
            print(f"missing {p}")
            return 2
    # the framework refuses paths outside its workspace; span both folders
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": os.path.commonpath([str(build), str(out)])}))
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    fab = export_fab(board, output_dir=str(out), gerbers=True, drill=True, position=True)
    print(f"fabrication files: {len(fab.files)} in {fab.output_dir} ({fab.duration_s}s)")
    for w in fab.warnings:
        print(f"   warning: {w}")

    bom = export_bom(root_sch, fields=BOM_FIELDS, group_by=["Value", "Footprint", "MPN"], output_path=str(out / f"{name}-bom-kicad.csv"))
    attrs = footprint_attrs(board)
    rows: list[list[str]] = []
    missing: list[str] = []
    for i, row in enumerate(bom.rows, start=1):
        v = row.values
        refs = expand_refs(v.get("Reference", ""))
        kinds = {mount_type(attrs.get(r, "")) for r in refs}
        if len(kinds) != 1:
            print(f"   note: {v.get('Reference')} mixes mounting types {sorted(kinds)}")
        if not v.get("MPN"):
            missing.append(v.get("Reference", "?"))
        rows.append([
            str(i), v.get("Reference", ""), v.get("QUANTITY", str(len(refs))), v.get("Manufacturer", ""), v.get("MPN", ""),
            v.get("Value", ""), package_name(v.get("Footprint", ""), v.get("Package", "")), "/".join(sorted(kinds)), v.get("PCBWay Note", ""),
        ])
    pcbway_csv = out / f"{name}-bom-pcbway.csv"
    with pcbway_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(PCBWAY_COLUMNS)
        w.writerows(rows)
    print(f"BOM: {len(rows)} lines, {sum(int(r[2]) for r in rows)} parts -> {pcbway_csv}")

    gerbers = sorted(p for p in out.iterdir() if ARCHIVE_PATTERN.search(p.name))
    zip_path = out / f"{name}-gerbers.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in gerbers:
            z.write(p, p.name)
    print(f"Gerber archive: {len(gerbers)} files -> {zip_path}")
    pos = out / f"{board.stem}-pos.csv"
    print(f"position file: {pos if pos.is_file() else 'MISSING'}")
    if missing:
        print(f"BOM lines without a manufacturer part number: {', '.join(missing)}")
        return 1
    return 0


def main(project: Project, argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Everything PCBWay needs for a turnkey order, from a finished build.")
    ap.add_argument("--build", help="folder with the built project files (default: the project)")
    ap.add_argument("--out", help="output folder (default: <project>/fab/pcbway)")
    args = ap.parse_args(argv)
    build = Path(args.build).resolve() if args.build else project.dir
    out = Path(args.out).resolve() if args.out else project.dir / "fab" / "pcbway"
    return pcbway_package(project.name, build, out)
