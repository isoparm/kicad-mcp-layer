"""Compare the connectivity of two KiCad XML netlists, net names aside.

A design described again from scratch is right when every part's pins group the same way as in
the reference: the same sets of (reference, pin) nodes, whatever the nets are called.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path

Nodes = dict[str, set[tuple[str, str]]]


def nodes(xml_path: Path) -> Nodes:
    """Net name -> its (reference, pin) nodes, from ``kicad-cli sch export netlist --format kicadxml``."""
    out: Nodes = {}
    for net in ET.parse(xml_path).getroot().iter("net"):
        out[net.get("name", "")] = {(n.get("ref", ""), n.get("pin", "")) for n in net.findall("node")}
    return out


def sheet_refs(xml_path: Path) -> dict[str, set[str]]:
    """Sheet path (``/Audio/``) -> the references placed on it."""
    out: dict[str, set[str]] = {}
    for comp in ET.parse(xml_path).getroot().iter("comp"):
        sp = comp.find("sheetpath")
        out.setdefault(sp.get("names", "/") if sp is not None else "/", set()).add(comp.get("ref", ""))
    return out


def groups(nets: Nodes, refs: Iterable[str] | None = None) -> set[frozenset[tuple[str, str]]]:
    """The connectivity as a set of node groups, restricted to ``refs`` when given."""
    keep = set(refs) if refs is not None else None
    out: set[frozenset[tuple[str, str]]] = set()
    for members in nets.values():
        grp = frozenset(m for m in members if keep is None or m[0] in keep)
        if grp:
            out.add(grp)
    return out


def _fmt(grp: frozenset[tuple[str, str]]) -> str:
    return " ".join(f"{r}.{p}" for r, p in sorted(grp))


def diff(reference: Nodes, candidate: Nodes, refs: Iterable[str] | None = None) -> tuple[list[str], list[str]]:
    """Groups only in the reference and groups only in the candidate, each as a ``ref.pin`` list."""
    a, b = groups(reference, refs), groups(candidate, refs)
    return sorted(_fmt(grp) for grp in a - b), sorted(_fmt(grp) for grp in b - a)
