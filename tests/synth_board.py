"""Small hand-made boards for the layout checks: only what ``review.load_board`` and ``design.copper`` read.

These are not KiCad fixtures (those stay KiCad-authored, see CLAUDE.md); they exercise geometry rules on a few
pads and tracks whose distances the test states, which no demo project has.
"""

from __future__ import annotations

from pathlib import Path

HEADER = """(kicad_pcb
\t(version 20241229)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general (thickness 1.6))
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(13 "F.Paste" user)
\t\t(1 "F.Mask" user)
\t\t(5 "F.SilkS" user "F.Silkscreen")
\t\t(25 "Edge.Cuts" user)
\t\t(31 "F.CrtYd" user "F.Courtyard")
\t\t(35 "F.Fab" user)
\t)
\t(setup
\t\t(stackup
\t\t\t(layer "F.Cu" (type "copper") (thickness 0.035))
\t\t\t(layer "dielectric 1" (type "core") (thickness 1.51) (material "FR4") (epsilon_r 4.5))
\t\t\t(layer "B.Cu" (type "copper") (thickness 0.035))
\t\t)
\t)
"""


class Board:
    def __init__(self, w: float = 50.0, h: float = 40.0) -> None:
        self.w, self.h = w, h
        self.items: list[str] = []
        self.n = 0

    def _u(self) -> str:
        self.n += 1
        return f"00000000-0000-0000-0000-{self.n:012d}"

    def footprint(self, ref: str, x: float, y: float, pads: list[tuple], *, lib: str = "Test:FP", court: tuple[float, float, float, float] | None = None,
                  rot: float = 0.0, extra: str = "") -> "Board":
        """pads: (number, net, dx, dy, w, h[, kind[, shape[, drill]]])."""
        lines = [f'\t(footprint "{lib}"', '\t\t(layer "F.Cu")', f'\t\t(uuid "{self._u()}")', f"\t\t(at {x} {y}{' ' + str(rot) if rot else ''})",
                 f'\t\t(property "Reference" "{ref}" (at 0 -2 0) (layer "F.SilkS") (uuid "{self._u()}") (effects (font (size 1 1) (thickness 0.15))))',
                 f'\t\t(property "Value" "v" (at 0 2 0) (layer "F.Fab") (uuid "{self._u()}") (effects (font (size 1 1) (thickness 0.15))))']
        xs, ys = [], []
        for p in pads:
            num, net, dx, dy, w, h = p[:6]
            kind = p[6] if len(p) > 6 else "smd"
            shape = p[7] if len(p) > 7 else "rect"
            drill = p[8] if len(p) > 8 else None
            layers = '"F.Cu" "F.Paste" "F.Mask"' if kind == "smd" else '"*.Cu" "*.Mask"'
            lines.append(f'\t\t(pad "{num}" {kind} {shape} (at {dx} {dy}) (size {w} {h})' + (f" (drill {drill})" if drill else "") +
                         f" (layers {layers})" + (f' (net "{net}")' if net else "") + f' (uuid "{self._u()}"))')
            xs += [dx - w / 2, dx + w / 2]
            ys += [dy - h / 2, dy + h / 2]
        cx0, cy0, cx1, cy1 = court or (min(xs) - 0.25, min(ys) - 0.25, max(xs) + 0.25, max(ys) + 0.25)
        lines.append(f'\t\t(fp_rect (start {cx0} {cy0}) (end {cx1} {cy1}) (stroke (width 0.05) (type solid)) (fill no) (layer "F.CrtYd") (uuid "{self._u()}"))')
        if extra:
            lines.append(extra)
        lines.append("\t)")
        self.items.append("\n".join(lines))
        return self

    def track(self, net: str, layer: str, w: float, *pts: tuple[float, float]) -> "Board":
        for a, b in zip(pts, pts[1:]):
            self.items.append(f'\t(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) (layer "{layer}") (net "{net}") (uuid "{self._u()}"))')
        return self

    def via(self, net: str, x: float, y: float, size: float = 0.6, drill: float = 0.3) -> "Board":
        self.items.append(f'\t(via (at {x} {y}) (size {size}) (drill {drill}) (layers "F.Cu" "B.Cu") (net "{net}") (uuid "{self._u()}"))')
        return self

    def zone(self, net: str, layer: str, outline: list[tuple[float, float]], fills: list[list[tuple[float, float]]] | None = None) -> "Board":
        pts = " ".join(f"(xy {x} {y})" for x, y in outline)
        body = [f'\t(zone (net "{net}") (layer "{layer}") (uuid "{self._u()}") (name "{net}") (hatch edge 0.5) (connect_pads (clearance 0.2)) (min_thickness 0.25)',
                "\t\t(fill yes (thermal_gap 0.3) (thermal_bridge_width 0.4) (island_removal_mode 0))", f"\t\t(polygon (pts {pts}))"]
        for f in fills if fills is not None else [outline]:
            body.append(f'\t\t(filled_polygon (layer "{layer}") (pts {" ".join(f"(xy {x} {y})" for x, y in f)}))')
        body.append("\t)")
        self.items.append("\n".join(body))
        return self

    def text(self) -> str:
        edge = f'\t(gr_rect (start 0 0) (end {self.w} {self.h}) (stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts") (uuid "{self._u()}"))'
        return HEADER + "\n".join(self.items + [edge]) + "\n)\n"

    def write(self, path: Path) -> Path:
        path.write_text(self.text(), encoding="utf-8")
        return path


PRO = """{
  "board": {"design_settings": {"rules": {"min_clearance": 0.2, "min_track_width": 0.2, "min_copper_edge_clearance": 0.3, "min_hole_clearance": 0.25}}},
  "net_settings": {"classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3},
                               {"name": "Power", "clearance": 0.2, "track_width": 0.5, "via_diameter": 0.8, "via_drill": 0.4}],
                   "netclass_patterns": [{"netclass": "Power", "pattern": "VIN"}]}
}
"""


def project(dir_: Path, board: Board, name: str = "t") -> Path:
    """Write ``board`` and a minimal .kicad_pro beside it; the board path."""
    (dir_ / f"{name}.kicad_pro").write_text(PRO, encoding="utf-8")
    return board.write(dir_ / f"{name}.kicad_pcb")
