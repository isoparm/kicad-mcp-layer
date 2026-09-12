"""Geometry checks on a drawn sheet, before KiCad sees it.

Errors are what KiCad would turn into a wrong netlist or an ERC error: two net names at one
point, a pin meeting a wire away from the wire's ends with no junction, two symbols on one spot.
Warnings are what only a person would notice: label text running over another symbol's body or
over another label. Text extents are estimates from the character count.
"""
from __future__ import annotations

from kicad_layer.sch_writer import Placed, SchematicBuilder
from kicad_layer.sexpr import child

from .draw import outward

Point = tuple[float, float]
Box = tuple[float, float, float, float]
EPS = 1e-3
CHAR_W = 0.9  # of the text size, per character
LINE_H = 1.5  # of the text size


def _key(p: Point) -> Point:
    return (round(p[0], 3), round(p[1], 3))


def _at(node: list) -> tuple[Point, int]:
    c = child(node, "at")
    return (float(c[1]), float(c[2])), (int(float(c[3])) if len(c) > 3 else 0)


def text_box(text: str, at: Point, rot: int, size: float = 1.27, extra: float = 0.0) -> Box:
    """The rectangle a label's text covers, for a label anchored at ``at`` reading in direction ``rot``."""
    w = len(text) * size * CHAR_W + extra
    h = size * LINE_H
    x, y = at
    r = rot % 360
    if r == 0:
        return (x, y - h, x + w, y)
    if r == 180:
        return (x - w, y - h, x, y)
    if r == 90:
        return (x - h, y - w, x, y)
    return (x - h, y, x, y + w)


def _overlap(a: Box, b: Box) -> bool:
    return a[0] < b[2] - EPS and b[0] < a[2] - EPS and a[1] < b[3] - EPS and b[1] < a[3] - EPS


def _interior(p: Point, a: Point, b: Point) -> bool:
    """``p`` lies on segment ``ab`` and is neither of its ends."""
    if _key(p) in (_key(a), _key(b)):
        return False
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    if abs(cross) > EPS:
        return False
    return min(a[0], b[0]) - EPS <= p[0] <= max(a[0], b[0]) + EPS and min(a[1], b[1]) - EPS <= p[1] <= max(a[1], b[1]) + EPS


def body_box(pl: Placed) -> Box | None:
    """The symbol body, estimated as the rectangle spanned by the inner ends of its pins."""
    pts = []
    for p in pl.symbol.pins:
        if pl.rot or pl.mirror:
            pts.append(pl.pin(p.number))  # turned parts are two-pin parts drawn by a rule: their body lies between the pins
        else:
            (x, y), (dx, dy) = outward(pl, p.number)
            pts.append((x - dx * p.length, y - dy * p.length))
    if not pts:
        return None
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    # a two-pin part's body is a line between its pins: give it width across, none along (its ends carry wires and labels)
    px = 1.0 if x1 - x0 < EPS else 0.0
    py = 1.0 if y1 - y0 < EPS else 0.0
    return (x0 - px, y0 - py, x1 + px, y1 + py)


def lint(sch: SchematicBuilder) -> tuple[list[str], list[str]]:
    """(errors, warnings) for the items drawn so far."""
    errors: list[str] = []
    warnings: list[str] = []
    names_at: dict[Point, set[str]] = {}
    labels: list[tuple[str, Point, int, str]] = []
    wires: list[tuple[Point, Point]] = []
    junctions: set[Point] = set()
    for node in sch.items:
        tag = str(node[0])
        if tag in ("label", "hierarchical_label"):
            at, rot = _at(node)
            labels.append((str(node[1]), at, rot, tag))
            names_at.setdefault(_key(at), set()).add(str(node[1]))
        elif tag == "wire":
            pts = child(node, "pts")
            wires.append(((float(pts[1][1]), float(pts[1][2])), (float(pts[2][1]), float(pts[2][2]))))
        elif tag == "junction":
            junctions.add(_key(_at(node)[0]))
    parts: list[Placed] = []
    terminals: dict[Point, int] = {}  # how many things end at a point: wire ends, junctions, labels, power pins, part pins
    for a, b in wires:
        for e in (a, b):
            terminals[_key(e)] = terminals.get(_key(e), 0) + 1
    for pt in junctions:
        terminals[pt] = terminals.get(pt, 0) + 1
    for _, at, _, _ in labels:
        terminals[_key(at)] = terminals.get(_key(at), 0) + 1
    for pl in sch.placed:
        if pl.symbol.lib_id.startswith("power:"):
            if pl.symbol.name != "PWR_FLAG":
                names_at.setdefault(_key(pl.at), set()).add(pl.symbol.name)
            terminals[_key(pl.at)] = terminals.get(_key(pl.at), 0) + 1
        else:
            parts.append(pl)
            for p in pl.symbol.pins:
                terminals[_key(pl.pin(p.number))] = terminals.get(_key(pl.pin(p.number)), 0) + 1

    for pt, names in sorted(names_at.items()):
        if len(names) > 1:
            errors.append(f"{' and '.join(sorted(names))} meet at {pt}")
    seen: dict[tuple[Point, int], str] = {}
    for pl in sch.placed:
        key = (_key(pl.at), pl.rot)
        if key in seen:
            errors.append(f"{pl.ref} sits on {seen[key]} at {pl.at}")
        else:
            seen[key] = pl.ref
    for pl in parts:
        for p in pl.symbol.pins:
            end = pl.pin(p.number)
            if terminals.get(_key(end), 0) > 1:
                continue  # something ends here: the pin is connected to that, whatever else passes through
            if any(_interior(end, a, b) for a, b in wires):
                errors.append(f"{pl.ref} pin {p.number} meets a wire away from its ends at {end} with no junction: KiCad does not connect it")

    bodies = [(pl.ref, box) for pl in parts if (box := body_box(pl)) is not None]
    boxes = [(text, text_box(text, at, rot, extra=2.54 if kind == "hierarchical_label" else 0.0), at) for text, at, rot, kind in labels]
    for i, (text, box, at) in enumerate(boxes):
        for ref, body in bodies:
            if _overlap(box, body):
                warnings.append(f"label {text!r} at {at} runs over {ref}")
        for text2, box2, at2 in boxes[i + 1:]:
            if text2 != text and _overlap(box, box2):
                warnings.append(f"labels {text!r} at {at} and {text2!r} at {at2} overprint")
    return errors, warnings
