"""A netlist from the descriptions, for a build without kicad-cli (``build.py --offline``).

It stands in for KiCad's netlist of the drawing only so that the board can be written: parts,
footprints, values, fields and the pads' nets as each sheet's :class:`Circuit` or module data
says. Nothing here reads the drawing, so nothing here checks it: ERC, the netlist gates and DRC
stay UNVERIFIED until a build with kicad-cli runs. Net names follow KiCad's for a hierarchy:
signals ``/NAME``, a sheet's named private nets ``/Sheet/NAME``, rails and ground by their power
symbol's name, an unnamed net ``Net-(REF-PadN)``. A sheet with no description (a hand-drawn
builder, or the root's placeholder for a sheet with no builder) contributes its parts without nets,
and the build says so. A do-not-populate part carries the ``dnp`` property KiCad's netlist gives it.
"""
from __future__ import annotations

from collections.abc import Mapping

from kicad_layer.kicad_libs import load_symbol
from kicad_layer.models import Component, Net, NetNode, Netlist
from kicad_layer.sch_writer import SchematicBuilder
from kicad_layer.sexpr import child, children, value

from .module_sheet import pin_roles

SOURCE = "(synthesized from the descriptions; not KiCad's netlist)"
# KiCad's netlist lists a do-not-populate symbol's flag as a property named "dnp"; the board reads it from there
_DNP = {"dnp": ""}


def _net_name(sheet: str, kind: str, name: str | None, first: tuple[str, str]) -> str:
    if kind == "signal":
        return f"/{name}"
    if kind in ("rail", "gnd"):
        return name or "GND"
    if name:
        return f"/{sheet}/{name}"
    return f"Net-({first[0]}-Pad{first[1]})"


def _drawn_components(sch: SchematicBuilder, sheet_name: str) -> dict[str, Component]:
    """The parts a sheet builder placed, read from its symbols (power symbols and flags left out); no nets."""
    comps: dict[str, Component] = {}
    for node in sch.items:
        if not isinstance(node, list) or str(node[0]) != "symbol" or child(node, "lib_id") is None:
            continue
        props = {str(p[1]): str(p[2]) for p in children(node, "property") if len(p) > 2}
        ref = props.get("Reference", "")
        if not ref or ref.startswith("#") or value(node, "on_board") == "no":
            continue
        lib, _, part = str(value(node, "lib_id")).partition(":")
        pins = sorted({str(p[1]) for p in children(node, "pin")} | set(comps[ref].pins if ref in comps else []))
        fields = {k: v for k, v in props.items() if k not in ("Reference", "Value", "Footprint", "Datasheet", "Description")}
        dnp = value(node, "dnp") == "yes" or (ref in comps and "dnp" in comps[ref].properties)
        comps[ref] = Component(ref=ref, value=props.get("Value"), footprint=props.get("Footprint") or None, lib=lib, part=part,
                               description=props.get("Description"), sheet_name=sheet_name, fields=fields, properties=dict(_DNP) if dnp else {}, pins=pins)
    return comps


def synth_netlist(sheets: Mapping[str, object], drawn: Mapping[str, SchematicBuilder] | None = None, *, root: SchematicBuilder | None = None,
                  source: str = "") -> tuple[Netlist, list[str]]:
    """The netlist the descriptions give, and notes on what it could not cover.

    ``sheets`` maps a sheet name to its builder, as ``Project.sheets()`` does; a builder with a
    ``circuit`` or a ``module`` is read from that description. ``drawn`` (the built sheets) supplies
    the parts of any other sheet (a hand-drawn builder, a placeholder), and ``root`` those placed on
    the root, without nets. A signal between two consumer sheets (``Signal.to``) is ``/NAME`` on both,
    as the root's net labels join them; a module signal is ``/NAME`` on the one connector pin it names.
    """
    comps: dict[str, Component] = {}
    nets: dict[str, list[NetNode]] = {}
    notes: list[str] = []

    def add(net: str, ref: str, pin: str) -> None:
        nodes = nets.setdefault(net, [])
        if not any(n.ref == ref and n.pin == pin for n in nodes):
            nodes.append(NetNode(ref=ref, pin=pin))

    for sheet_name, builder in sheets.items():
        circuit = getattr(builder, "circuit", None)
        module = getattr(builder, "module", None)
        if circuit is not None:
            for ref, inst in circuit.parts.items():
                comps[ref] = Component(ref=ref, value=inst.value_text or inst.part.value, footprint=inst.part.footprint, lib=inst.part.symbol[0],
                                       part=inst.part.symbol[1], fields=inst.part.fields(), sheet_name=f"/{sheet_name}/",
                                       properties=dict(_DNP) if inst.dnp else {}, pins=list(dict.fromkeys(p.number for p in inst.pins)))
            for n in circuit.nets:
                if not n.pins:
                    continue
                name = _net_name(sheet_name, n.kind, n.name, (n.pins[0].ref, n.pins[0].number))
                for p in n.pins:
                    add(name, p.ref, p.number)
        elif module is not None:
            _module_nets(module, sheet_name, comps, add)
        elif drawn is not None and sheet_name in drawn:
            found = _drawn_components(drawn[sheet_name], f"/{sheet_name}/")
            comps.update(found)
            if found:
                notes.append(f"{sheet_name}: no description, so its {len(found)} parts go on the board without nets")
        else:
            notes.append(f"{sheet_name}: no description and no drawing; its parts are missing")
    for sheet_name in drawn or {}:
        if sheet_name in sheets:
            continue
        found = _drawn_components(drawn[sheet_name], f"/{sheet_name}/")  # a placeholder the root drew for a sheet with no builder
        comps.update(found)
        if found:
            notes.append(f"{sheet_name}: no builder (a placeholder), so its {len(found)} parts ({', '.join(sorted(found))}) go on the board without nets")
    if root is not None:
        found = {r: c for r, c in _drawn_components(root, "/").items() if r not in comps}
        comps.update(found)
        if found:
            notes.append(f"root: {len(found)} parts ({', '.join(sorted(found))}) without nets")
    net_list = [Net(code=i + 1, name=name, nodes=nodes) for i, (name, nodes) in enumerate(sorted(nets.items()))]
    netlist = Netlist(source=source, netlist_path=SOURCE, cache_hit=False, sheets=[], component_count=len(comps), net_count=len(net_list),
                      components=list(comps.values()), nets=net_list, command=[])
    return netlist, notes


def _module_nets(m, sheet_name: str, comps: dict[str, Component], add) -> None:
    """A module sheet as ``build_module_sheet`` draws it, pin keys resolved as it resolves them (``pin_roles``): each
    signal on the one connector pin it names, the ground pins (``gnd_pins`` and ground names), the power groups
    with their capacitors. Sheet-to-sheet signals in the table touch no module pin."""
    pins_of = {}
    for ref, part, *_ in m.parts:
        sym = load_symbol(*part.symbol)
        pins_of[ref] = sym.pins
        comps[ref] = Component(ref=ref, value=part.value, footprint=part.footprint, lib=part.symbol[0], part=part.symbol[1], fields=part.fields(),
                               sheet_name=f"/{sheet_name}/", pins=list(dict.fromkeys(p.number for p in sym.pins)))
    roles = pin_roles(m, pins_of)
    for (ref, number), sig in roles.signals.items():
        add(f"/{sig.name}", ref, number)
    for ref, number in sorted(roles.ground):
        add("GND", ref, number)
    for grp, owner, numbers in roles.groups:
        for n in numbers:
            add(grp.net, owner, n)
        for cap, part in grp.caps:
            comps[cap] = Component(ref=cap, value=part.value, footprint=part.footprint, lib=part.symbol[0], part=part.symbol[1], fields=part.fields(),
                                   sheet_name=f"/{sheet_name}/", pins=["1", "2"])
            add(grp.net, cap, "1")
            add("GND", cap, "2")
