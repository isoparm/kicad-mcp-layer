"""What a board-as-code project is: its identity, files, libraries, sheets, root layout and rules.

A :class:`Project` is data plus a few callables the project supplies (its sheet builders, its
symbol-library generator, its board placement). Everything the build pipeline needs comes
from here, so a second board is a second ``Project`` and no copied pipeline.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from kicad_layer.kicad_libs import register_footprint_lib, register_symbol_lib
from kicad_layer.sch_writer import SchematicBuilder

from .signals import Signal

SheetBuilder = Callable[[SchematicBuilder], None]


@dataclass
class Rules:
    """What goes into the .kicad_pro and the .kicad_dru: built on a KiCad-authored project file so every section exists."""

    template: Path
    rules: dict
    classes: list[dict]
    assignments: list[dict]  # KiCad 10 netclass_patterns: [{"netclass": ..., "pattern": ...}]
    track_widths: list[float]
    via_dimensions: list[dict]
    diff_pair_dimensions: list[dict]
    design_rules: str = ""  # the .kicad_dru text, if any


@dataclass
class RootLayout:
    """The root sheet: the module sheet in the middle, consumer sheets facing it, one straight wire per signal."""

    module_sheet: str
    left_sheets: list[str]
    right_sheets: list[str]
    module_at: tuple[float, float] = (152.4, 25.4)
    sheet_w: float = 50.8
    left_x: float = 63.5
    right_x: float = 254.0
    pitch: float = 2.54
    paper: str = "A3"
    comments: list[str] = field(default_factory=list)
    extras: Callable[[SchematicBuilder], None] | None = None  # anything else on the root (mounting holes, notes)


@dataclass
class Review:
    """A reference design the build compares itself with, pad for pad (``design/compare.py``, ``build.py --review``)."""

    reference: Path  # a kicadxml netlist, or a .kicad_sch to export one from with kicad-cli
    module: tuple[str, ...]  # our references of the part both designs share (the module's connectors)
    reference_module: tuple[str, ...]  # that part's references in the reference design
    report: Path | None = None  # the markdown report; default review/<reference stem>-pins.md in the project
    ignore: tuple[str, ...] = ()  # our references left out of the comparison (fnmatch patterns)
    ignore_reference: tuple[str, ...] = ()  # the reference's, e.g. its expansion header and test points


@dataclass
class Project:
    name: str
    dir: Path
    title: str
    date: str
    rev: str
    company: str
    signals: list[Signal]
    root: RootLayout
    rules: Rules
    sheets: Callable[[], dict[str, SheetBuilder]]  # sheet name -> builder, resolved at build time (the sheet modules import the project)
    libraries: dict[str, tuple[Path, Path]] = field(default_factory=dict)  # name -> (symbols, footprints)
    symbol_writer: Callable[[Path], list[Path]] | None = None  # regenerates the project's own library into lib/
    check_signals: Callable[[], None] | None = None
    board_builder: Callable[..., dict] | None = None  # build_board(out_path, sheetfile, netlist, symbol_paths, setup_template, with_routes=...)
    setup_template: Path | None = None  # a KiCad board whose setup/stackup the generated board copies
    gui_task: str = ""  # the Windows scheduled task that opens KiCad on the project
    design_globs: tuple[str, ...] = ("*.kicad_sch", "*.kicad_pcb", "*.kicad_pro")
    reference_netlist: Path | None = None  # a kicadxml netlist every described sheet must agree with, net names aside
    review: Review | None = None  # a reference design to compare with on ``--review``
    blocks: list = field(default_factory=list)  # design.blocks.Block: members placed from an anchor and grouped, on ``build.py --blocks``

    @property
    def lib_dir(self) -> Path:
        return self.dir / "lib"

    @property
    def staging(self) -> Path:
        return self.dir.parent / "_staging" / self.dir.name

    def register_libraries(self) -> None:
        """The project's own libraries resolve ahead of the globals; the generated one is refreshed first."""
        if self.symbol_writer is not None:
            self.symbol_writer(self.lib_dir)
        for name, (sym, pretty) in self.libraries.items():
            register_symbol_lib(name, sym)
            register_footprint_lib(name, pretty)


def write_project_file(project: Project, path: Path, sheets: list[list[str]]) -> None:
    """The .kicad_pro from the template, with the project's rules, classes and sheet list, and its .kicad_dru."""
    r = project.rules
    pro = json.loads(r.template.read_text(encoding="utf-8"))
    pro["meta"] = {"filename": f"{project.name}.kicad_pro", "version": 3}
    pro["sheets"] = sheets
    pro["text_variables"] = {}
    pro["libraries"] = {"pinned_footprint_libs": [], "pinned_symbol_libs": []}
    pro["cvpcb"] = {"equivalence_files": []}
    pro["boards"] = []
    ds = pro["board"]["design_settings"]
    ds["rules"] = {**ds.get("rules", {}), **r.rules}
    ds["drc_exclusions"] = []
    ds["track_widths"] = list(r.track_widths)
    ds["via_dimensions"] = list(r.via_dimensions)
    ds["diff_pair_dimensions"] = list(r.diff_pair_dimensions)
    base = dict(pro["net_settings"]["classes"][0])
    classes = []
    for c in r.classes:
        d = dict(base)
        d.update(c)
        d.setdefault("microvia_diameter", 0.3)
        d.setdefault("microvia_drill", 0.1)
        classes.append(d)
    pro["net_settings"]["classes"] = classes
    # KiCad 10 reads pattern-based class assignment from netclass_patterns; netclass_assignments is the explicit map and stays null
    pro["net_settings"]["netclass_patterns"] = list(r.assignments)
    pro["net_settings"]["netclass_assignments"] = None
    pro["erc"]["erc_exclusions"] = []
    path.write_text(json.dumps(pro, indent=2), encoding="utf-8", newline='\n')
    if r.design_rules:
        path.with_suffix(".kicad_dru").write_text(r.design_rules, encoding="utf-8", newline='\n')
