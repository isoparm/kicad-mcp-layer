"""Specctra session (.ses) import: the routes an autorouter produced, as segments and vias in mm.

Coordinates in a session are integers in resolution units (``(resolution um 10)`` means tenths of a
micrometre) with Y counted upwards, which is how KiCad's own importer reads them. Via sizes come
from the padstack name (``Via[0-3]_600:300_um``) when the session does not spell them out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .dsn import parse_via_name
from ..routes import RouteSegment, Routes, RouteVia  # noqa: F401  re-exported: the session parser produces them
from ..sexpr import child, children, parse, tag

UNIT_MM = {"um": 0.001, "mm": 1.0, "cm": 10.0, "mil": 0.0254, "inch": 25.4}


def _resolution(node) -> float:
    """Millimetres per coordinate unit for a section carrying (resolution unit n)."""
    res = child(node, "resolution")
    if res is None:
        return 0.001 / 10  # KiCad's default: tenths of a micrometre
    unit = str(res[1]).lower()
    per = float(res[2]) if len(res) > 2 else 1.0
    return UNIT_MM.get(unit, 0.001) / per


def parse_ses(path: Path) -> Routes:
    root = parse(path.read_text(encoding="utf-8", errors="replace"))
    routes = child(root, "routes")
    if routes is None:
        return Routes()
    scale = _resolution(routes)
    via_sizes: dict[str, tuple[float, float]] = {}
    lib = child(routes, "library_out")
    if lib is not None:
        for ps in children(lib, "padstack"):
            name = str(ps[1])
            guess = parse_via_name(name)
            if guess:
                via_sizes[name] = guess
                continue
            # fall back to the first circle shape's diameter, drill unknown
            for sh in children(ps, "shape"):
                circ = sh[1] if len(sh) > 1 and isinstance(sh[1], list) and tag(sh[1]) == "circle" else None
                if circ is not None and len(circ) > 2:
                    via_sizes[name] = (float(circ[2]) * scale, 0.3)
                    break
    out = Routes()
    net_out = child(routes, "network_out")
    if net_out is None:
        return out
    for net in children(net_out, "net"):
        name = str(net[1])
        for wire in children(net, "wire"):
            path = child(wire, "path")
            if path is None or len(path) < 7:
                continue
            layer = str(path[1])
            width = float(path[2]) * scale
            nums = [float(a) for a in path[3:] if not isinstance(a, list)]
            pts = [(nums[i] * scale, -nums[i + 1] * scale) for i in range(0, len(nums) - 1, 2)]
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                if abs(x2 - x1) < 0.005 and abs(y2 - y1) < 0.005:
                    continue  # FreeRouting emits sub-micron slivers at some corners; KiCad flags them as dangling
                if abs(x1 - x2) < 1e-6 and abs(y1 - y2) < 1e-6:
                    continue
                out.segments.append(RouteSegment(name, layer, round(width, 4), round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)))
            out.nets.add(name)
        for via in children(net, "via"):
            ps_name = str(via[1])
            size, drill = via_sizes.get(ps_name) or parse_via_name(ps_name) or (0.6, 0.3)
            x, y = float(via[2]) * scale, -float(via[3]) * scale
            out.vias.append(RouteVia(name, round(x, 4), round(y, 4), size, drill))
            out.nets.add(name)
    return out
