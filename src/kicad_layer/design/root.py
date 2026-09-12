"""The root sheet and the placeholder consumer sheets, from a project's signal table.

Root layout: the module sheet in the middle, consumer sheets on its left and right. Every
consumer sheet's pins sit on the side facing the module sheet at the same rows as the matching
module pins, so every root wire is a straight horizontal line. A sheet the project has no
builder for gets a placeholder: a pin header carrying its signals, so ERC is clean and the
board shows where the block's connections land.
"""
from __future__ import annotations

from kicad_layer.ids import IdFactory
from kicad_layer.sch_writer import PlacedSheet, SchematicBuilder

from .draw import attach_label, g
from .project import Project
from .signals import Signal, by_sheet

GAP_ROWS = 2


def _rows(project: Project, names: list[str]) -> tuple[dict[str, tuple[int, list[Signal]]], int]:
    """Sheet name -> (first row, its signals); rows count from 1 with a gap between sheets."""
    rows: dict[str, tuple[int, list[Signal]]] = {}
    row = 1
    for n in names:
        sigs = by_sheet(project.signals, n)
        rows[n] = (row, sigs)
        row += len(sigs) + GAP_ROWS
    return rows, row - GAP_ROWS


def build_root(project: Project) -> tuple[SchematicBuilder, dict[str, SchematicBuilder]]:
    L = project.root
    root = SchematicBuilder(project.name, ids=IdFactory(scope=project.name), paper=L.paper, title=project.title, date=project.date, rev=project.rev,
                            company=project.company, comments=list(L.comments))
    left, left_rows = _rows(project, L.left_sheets)
    right, right_rows = _rows(project, L.right_sheets)
    total_rows = max(left_rows, right_rows)

    def module_side(rows: dict[str, tuple[int, list[Signal]]], n_rows: int) -> list[tuple[str, str]]:
        pins: list[tuple[str, str]] = [("", "passive")] * n_rows
        for start, sigs in rows.values():
            for i, s in enumerate(sigs):
                pins[start - 1 + i] = (s.name, s.cm5_shape)
        return pins

    module = root.sheet(L.module_sheet, f"{L.module_sheet}.kicad_sch", L.module_at, (L.sheet_w, g((total_rows + 1) * L.pitch)),
                        pins_left=module_side(left, total_rows), pins_right=module_side(right, total_rows))
    children: dict[str, SchematicBuilder] = {}
    placed: dict[str, PlacedSheet] = {L.module_sheet: module}
    for names, rows, x, facing_right in ((L.left_sheets, left, L.left_x, True), (L.right_sheets, right, L.right_x, False)):
        for n in names:
            start, sigs = rows[n]
            y0 = g(L.module_at[1] + (start - 1) * L.pitch)
            pins = [(s.name, s.consumer_shape) for s in sigs]
            sh = root.sheet(n, f"{n}.kicad_sch", (x, y0), (L.sheet_w, g((len(sigs) + 1) * L.pitch)),
                            pins_right=pins if facing_right else None, pins_left=None if facing_right else pins)
            placed[n] = sh
            for s in sigs:
                a, b = sh.pin(s.name), module.pin(s.name)
                assert abs(a[1] - b[1]) < 1e-6, (s.name, a, b)
                root.wire(a, b)
    if L.extras is not None:
        L.extras(root)
    builders = project.sheets()
    module_sch = root.child(module, paper=L.paper, title=f"{L.module_sheet} module", date=project.date, rev=project.rev, company=project.company)
    children[L.module_sheet] = module_sch
    for n in L.left_sheets + L.right_sheets:
        if n in builders:
            children[n] = root.child(placed[n], paper=L.paper, title=n, date=project.date, rev=project.rev, company=project.company)
            continue
        cb = root.child(placed[n], paper="A4", title=f"{n} (placeholder)", date=project.date, rev=project.rev, company=project.company)
        _placeholder(cb, n, by_sheet(project.signals, n))
        children[n] = cb
    return root, children


def _placeholder(sch: SchematicBuilder, name: str, sigs: list[Signal]) -> None:
    """A pin header standing in for the block, one pin per signal; Power also sources +5V and GND."""
    extra = 2 if name == "Power" else 0
    n = len(sigs) + extra
    hdr = sch.place("Connector_Generic", f"Conn_01x{n:02d}", f"J_{name.upper()}", (127.0, g(50.8 + n * 1.27)), value_text=f"{name} placeholder",
                    footprint=f"Connector_PinHeader_2.54mm:PinHeader_1x{n:02d}_P2.54mm_Vertical", in_bom=False)
    sch.text(f"{name}: placeholder header until the block is designed. Pins carry the sheet's signals.", (25.4, 38.1), size=1.5)
    for i, s in enumerate(sigs):
        attach_label(sch, hdr, str(i + 1), s.name, s.consumer_shape, 10.16)
    if extra:
        # the converter's 5 V output and its return, flagged as the source of the rails
        for pin, net in ((str(n - 1), "+5V"), (str(n), "GND")):
            end = hdr.pin(pin)
            far = (g(end[0] - 10.16), end[1])
            sch.wire(end, far)
            sch.power(net, far)
            sch.pwr_flag(far)


def build_design(project: Project) -> tuple[SchematicBuilder, dict[str, SchematicBuilder]]:
    """The root and every sheet, fully populated: what the build writes and what the tests inspect."""
    root, children = build_root(project)
    for name, builder in project.sheets().items():
        builder(children[name])
    return root, children
