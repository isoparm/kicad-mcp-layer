"""A drawn sheet means what its description says: KiCad's netlist of the drawing, pin group for pin group.

``check_sheet`` renders one circuit alone, exports its netlist with kicad-cli and compares the pin
groups with the circuit's nets; ``check_module`` does the same for a module sheet. The build runs
the same comparison for free against the project's own netlist. Net names play no part: a group is
the set of (reference, pin) that end up connected. Groups of one pin carry no information and are
ignored on both sides.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from kicad_layer.cli import runner
from kicad_layer.cli.discovery import find_kicad_cli
from kicad_layer.ids import IdFactory
from kicad_layer.kicad_libs import load_symbol
from kicad_layer.sch_writer import SchematicBuilder

from . import netcompare
from .circuit import Circuit
from .module_sheet import ModuleSheet, build_module_sheet
from .render import Layout, render

Groups = set[frozenset[tuple[str, str]]]


def circuit_groups(c: Circuit) -> Groups:
    return {g for n in c.nets if len(g := frozenset((p.ref, p.number) for p in n.pins)) > 1}


def module_groups(m: ModuleSheet) -> Groups:
    """What a module sheet connects: each power group with its capacitors' first pins, every ground pin with the second pins."""
    pins_of = {ref: load_symbol(*part.symbol).pins for ref, part, *_ in m.parts}
    by_net: dict[str, set[tuple[str, str]]] = {}
    gnd = {(ref, p.number) for ref, pins in pins_of.items() for p in pins if p.name.startswith(m.gnd_prefix)}
    for grp in m.power:
        owner = next(ref for ref, pins in pins_of.items() if all(any(p.number == n for p in pins) for n in grp.pins))
        by_net.setdefault(grp.net, set()).update({(owner, n) for n in grp.pins} | {(cap, "1") for cap, _ in grp.caps})
        gnd |= {(cap, "2") for cap, _ in grp.caps}
    groups = {frozenset(s) for s in by_net.values()} | {frozenset(gnd)}
    return {g for g in groups if len(g) > 1}


def module_refs(m: ModuleSheet) -> set[str]:
    return {ref for ref, *_ in m.parts} | {cap for grp in m.power for cap, _ in grp.caps}


def netlist_groups(xml: Path, refs) -> Groups:
    return {g for g in netcompare.groups(netcompare.nodes(xml), refs) if len(g) > 1}


def compare(want: Groups, got: Groups, *, want_label: str = "description only", got_label: str = "drawing only") -> list[str]:
    """The pin groups that differ, one line each: what only ``want`` has, then what only ``got`` has."""
    def fmt(g: frozenset[tuple[str, str]]) -> str:
        return " ".join(f"{r}.{p}" for r, p in sorted(g))

    return [f"{want_label}: {fmt(g)}" for g in sorted(want - got, key=fmt)] + [f"{got_label}: {fmt(g)}" for g in sorted(got - want, key=fmt)]


def export_netlist(sch: SchematicBuilder, workdir: Path, name: str) -> Path:
    """Write the sheet as a schematic of its own and export its kicadxml netlist."""
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / f"{name}.kicad_sch"
    sch.write(str(path))
    xml = workdir / f"{name}.xml"
    cli = find_kicad_cli()
    r = runner.run([cli.path, "sch", "export", "netlist", "--format", "kicadxml", "-o", str(xml), str(path)], timeout_s=120, cwd=workdir)
    if r.returncode != 0 or not xml.is_file():
        raise RuntimeError(f"kicad-cli could not export the netlist of {name}: {r.tail()}")
    return xml


def _workdir(workdir: Path | str | None) -> Path:
    return Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="kicad-mcp-layer-check-"))


def check_sheet(circuit: Circuit, layout: Layout, workdir: Path | str | None = None) -> list[str]:
    """Differences between the circuit and KiCad's reading of its drawing; empty when they agree.

    Lint errors and circuit problems raise from :func:`render`."""
    sch = SchematicBuilder(circuit.sheet, ids=IdFactory(scope=f"check/{circuit.sheet}"), paper="A3", title=circuit.sheet, date="", rev="", company="")
    render(circuit, layout, sch)
    xml = export_netlist(sch, _workdir(workdir), circuit.sheet)
    return compare(circuit_groups(circuit), netlist_groups(xml, set(circuit.parts)))


def check_module(module: ModuleSheet, name: str = "module", workdir: Path | str | None = None) -> list[str]:
    """A module sheet drawn to a scratch file, exported through kicad-cli and compared with its description; the differences."""
    sch = SchematicBuilder(name, ids=IdFactory(scope=f"check/{name}"), paper="A3", title=name, date="", rev="", company="")
    build_module_sheet(module, sch)
    xml = export_netlist(sch, _workdir(workdir), name)
    return compare(module_groups(module), netlist_groups(xml, module_refs(module)))
