"""A drawing read back into pin groups without kicad-cli: KiCad's joining rules on one sheet, for tests.

Points join where wire ends meet, where a junction or a label sits on a wire, and where pins, power
symbols and labels coincide; labels of one name and power symbols of one name are one net. A pin that
meets a wire away from its ends joins only through a junction, as in KiCad. This is a test aid, not the
build's check: the build reads KiCad's own netlist.
"""
from __future__ import annotations

from kicad_layer.sch_writer import SchematicBuilder
from kicad_layer.sexpr import child


def _pt(x, y) -> tuple[float, float]:
    return (round(float(x), 3), round(float(y), 3))


def _on(p, a, b) -> bool:
    if p in (a, b):
        return False
    if abs((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])) > 1e-6:
        return False
    return min(a[0], b[0]) - 1e-6 <= p[0] <= max(a[0], b[0]) + 1e-6 and min(a[1], b[1]) - 1e-6 <= p[1] <= max(a[1], b[1]) + 1e-6


def pin_groups(sch: SchematicBuilder) -> tuple[set[frozenset[tuple[str, str]]], dict[str, set[tuple[str, str]]]]:
    """(groups of two or more (ref, pin) that the drawing connects, name -> the pins on each named net)."""
    parent: dict = {}

    def find(p):
        parent.setdefault(p, p)
        while parent[p] != p:
            parent[p] = parent[parent[p]]
            p = parent[p]
        return p

    def union(a, b):
        parent[find(a)] = find(b)

    wires, taps, names = [], set(), []
    for node in sch.items:
        tag = str(node[0])
        if tag == "wire":
            pts = child(node, "pts")
            a, b = _pt(pts[1][1], pts[1][2]), _pt(pts[2][1], pts[2][2])
            wires.append((a, b))
            union(a, b)
        elif tag in ("junction", "label", "hierarchical_label", "global_label"):
            at = child(node, "at")
            p = _pt(at[1], at[2])
            taps.add(p)
            if tag != "junction":
                names.append((str(node[1]), p))
    for t in taps:
        for a, b in wires:
            if _on(t, a, b):
                union(t, a)
    pins: list[tuple[tuple[str, str], tuple[float, float]]] = []
    for pl in sch.placed:
        if pl.symbol.lib_id.startswith("power:"):
            if pl.symbol.name != "PWR_FLAG":
                names.append((pl.symbol.name, _pt(*pl.at)))
            else:
                find(_pt(*pl.at))
            continue
        for p in pl.symbol.pins:
            if p.unit in (0, pl.unit):
                pins.append(((pl.ref, p.number), _pt(*pl.pin(p.number))))
    for name, p in names:
        union(p, ("name", name))
    groups: dict = {}
    for key, p in pins:
        groups.setdefault(find(p), set()).add(key)
    named = {name: groups.get(find(("name", name)), set()) for name, _ in names}
    return {frozenset(g) for g in groups.values() if len(g) > 1}, named
