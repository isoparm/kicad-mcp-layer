"""Seed: the first import of a hand-made board, done by the build instead of KiCad's F8.

KiCad's *Update PCB from Schematic* drops new footprints in a pile and knows nothing about how parts belong
together. The seed writes the board once, when it holds no footprints yet: every footprint from the netlist in
tidy rows inside a placeholder outline, linked to its symbol the way KiCad links them (so a later F8 recognises
each one), the board's own setup and stack-up kept, and the project's blocks applied, so the Compute Module's two
connectors already form the module. From then on the board is the user's: the build never writes it again.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..kicad_libs import load_footprint
from ..sexpr import child, value
from . import blocks as blocks_mod
from .board import Board, Place, Text, _build

Point = tuple[float, float]


@dataclass(frozen=True)
class Pile:
    """Where the seed puts the parts: rows from (x0, y0), at most ``width`` wide, ``gap`` between courtyards."""

    origin: Point = (20.0, 20.0)
    width: float = 160.0
    gap: float = 2.0


Rect = tuple[float, float, float, float]


def pack(boxes: dict[str, Rect], pile: Pile) -> dict[str, Point]:
    """Row packing of each part's box (x0, y0, x1, y1 around its own origin, unrotated): where the origin goes so the
    boxes sit in rows without overlapping, wrapping at the width. A box need not be centred on the origin: the
    Compute Module's hangs 51 mm to one side of its hole."""
    out: dict[str, Point] = {}
    x, y = pile.origin
    row_h = 0.0
    for ref, (x0, y0, x1, y1) in boxes.items():
        w, h = x1 - x0, y1 - y0
        if x > pile.origin[0] and x + w > pile.origin[0] + pile.width:
            x = pile.origin[0]
            y += row_h + pile.gap
            row_h = 0.0
        out[ref] = (round(x - x0, 3), round(y - y0, 3))  # the box's corner lands on the cursor
        x += w + pile.gap
        row_h = max(row_h, h)
    return out


def bottom(boxes: dict[str, Rect], at: dict[str, Point]) -> float:
    """The lowest edge of the packed boxes."""
    return max(at[r][1] + b[3] for r, b in boxes.items()) if boxes else 0.0


def extent(fp, margin: float = 0.5) -> Rect:
    """Everything a part occupies, around its origin: courtyard, silkscreen and fab drawings (a module's outline is
    often only drawn, not a courtyard) and the pads, plus ``margin``. Wider than the courtyard on purpose: the seed
    packs parts to be picked up, not to be soldered."""
    xs: list[float] = []
    ys: list[float] = []
    for g in fp.tree:
        if not isinstance(g, list) or str(g[0]) not in ("fp_line", "fp_rect", "fp_arc", "fp_poly", "fp_circle"):
            continue
        if value(g, "layer") not in ("F.CrtYd", "F.SilkS", "F.Fab", "Edge.Cuts"):
            continue
        pts = [child(g, k) for k in ("start", "end", "mid", "center")]
        pts = [(float(c[1]), float(c[2])) for c in pts if c is not None]
        if str(g[0]) == "fp_circle" and len(pts) == 2:
            (cx, cy), (ex, ey) = pts
            r = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
            pts = [(cx - r, cy - r), (cx + r, cy + r)]
        poly = child(g, "pts")
        if poly is not None:
            pts += [(float(p[1]), float(p[2])) for p in poly[1:] if isinstance(p, list) and str(p[0]) == "xy"]
        for x, y in pts:
            xs.append(x)
            ys.append(y)
    for p in fp.pads:
        w, h = p.size
        xs += [p.x - w / 2, p.x + w / 2]
        ys += [p.y - h / 2, p.y + h / 2]
    if not xs:
        return (-1.0, -1.0, 1.0, 1.0)
    return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)


def sort_key(ref: str) -> tuple[str, int, str]:
    """Letters, then the number, so C2 sorts before C10 and every prefix stays together."""
    head = ref.rstrip("0123456789")
    tail = ref[len(head):]
    return head, int(tail) if tail.isdigit() else 0, ref


def seed_board(project, pcb_path: Path, sheetfile: str, netlist, symbol_paths: dict[str, tuple[str, str, str]], *,
               pile: Pile = Pile(), margin: float = 10.0) -> list[str]:
    """Write the seed into ``pcb_path`` (which must hold no footprints) and apply the project's blocks; returns the log lines.

    The placeholder outline is drawn ``margin`` around the rows, so every part starts inside it."""
    text = pcb_path.read_text(encoding="utf-8") if pcb_path.is_file() else ""
    if "(footprint " in text:
        raise ValueError(f"{pcb_path.name} already holds footprints: it is the hand-made board, the seed does not touch it")
    boxes: dict[str, Rect] = {}
    for comp in sorted(netlist.components, key=lambda c: sort_key(c.ref)):
        lib, name = comp.footprint.split(":", 1)
        boxes[comp.ref] = extent(load_footprint(lib, name))
    at = pack(boxes, pile)
    x0, y0 = pile.origin[0] - margin, pile.origin[1] - margin
    outline = (x0, y0, pile.width + 2 * margin, round(bottom(boxes, at) + margin - y0, 3))
    board = Board(
        title=project.title, scope=f"{project.name}-seed", outline=outline, copper_layers=4,
        placements=tuple(Place(ref, at[ref]) for ref in at),
        texts=(Text("Placeholder outline from the seed: draw the real one and delete this.", (x0 + 2, y0 + outline[3] - 4), size=1.5),),
    )
    setup_template = pcb_path if text else project.setup_template
    stats = _build(board, pcb_path, sheetfile, netlist, symbol_paths, setup_template, date=project.date, rev=project.rev, company=project.company,
                   with_routes=False, lenient=frozenset(at))
    lines = [f"seed: {len(at)} footprints in rows from {pile.origin}, placeholder outline {outline[2]} x {outline[3]} mm ({stats})"]
    if project.blocks:
        lines += blocks_mod.apply(pcb_path, project.blocks)
    return lines
