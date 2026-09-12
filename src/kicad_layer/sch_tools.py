"""Schematic editing tools: the glue between :mod:`sch_edit` and the MCP surface.

Every write follows the same contract: refuse in read-only mode, edit the tree, then either
report what would change (dry run) or save atomically with a snapshot and validate the
result with ERC and a netlist comparison against the state before the edit.
"""

from __future__ import annotations

from pathlib import Path

from kicad_layer.cli import netlist as netlist_mod
from kicad_layer.cli import reports
from kicad_layer.errors import LayerError, require_write_mode
from kicad_layer.models import (
    AnnotateResult,
    ComponentPin,
    EditResult,
    NetlistDelta,
    SchematicComponent,
    SchematicComponents,
)
from kicad_layer.paths import SCHEMATIC, display, resolve_in_workspace, root_schematic_for
from kicad_layer.sch_edit import Point, Schematic, snap

Pins = dict[str, tuple[float, float, str, str]]


def _pins(p: Pins) -> list[ComponentPin]:
    return [ComponentPin(number=n, name=v[2], type=v[3], x_mm=v[0], y_mm=v[1]) for n, v in sorted(p.items(), key=lambda kv: (len(kv[0]), kv[0]))]


def load(schematic_path: str) -> Schematic:
    path = resolve_in_workspace(schematic_path, must_exist=True, suffixes=(SCHEMATIC,))
    return Schematic(path)


def list_components(schematic_path: str, *, ref_prefix: str | None = None, include_pins: bool = False, include_power: bool = False, limit: int = 500) -> SchematicComponents:
    sch = load(schematic_path)
    out: list[SchematicComponent] = []
    views = sch.placed()
    for v in views:
        ref = sch._instance_reference(v.node) or v.reference
        if not include_power and ref.startswith("#"):
            continue
        if ref_prefix and not ref.upper().startswith(ref_prefix.upper()):
            continue
        out.append(_component(sch, v, ref, include_pins))
    out.sort(key=lambda c: (c.ref.rstrip("0123456789?"), len(c.ref), c.ref))
    return SchematicComponents(
        schematic=display(sch.path), sheet_uuid=sch.uuid, instance_path=sch.instance_path(),
        total=len(out), components=out[:limit], truncated=len(out) > limit,
    )


def _component(sch: Schematic, v, ref: str, include_pins: bool) -> SchematicComponent:
    pins: list[ComponentPin] = []
    if include_pins:
        try:
            pins = _pins(sch.pin_positions(v))
        except LayerError:
            pins = []
    return SchematicComponent(
        ref=ref, lib_id=v.lib_id, value=v.properties.get("Value"), footprint=v.properties.get("Footprint") or None,
        uuid=v.uuid, x_mm=v.at[0], y_mm=v.at[1], rotation=v.rotation, mirror=v.mirror, unit=v.unit,
        dnp=(str(v.node and next((c for c in v.node if isinstance(c, list) and c and c[0] == "dnp"), ["", "no"])[1]) == "yes"),
        properties={k: val for k, val in v.properties.items() if k not in ("Reference", "Value", "Footprint")},
        pins=pins,
    )


def get_component(schematic_path: str, ref: str) -> SchematicComponent:
    sch = load(schematic_path)
    v = sch.find(ref)
    return _component(sch, v, sch._instance_reference(v.node) or v.reference, True)


# --------------------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------------------


def _netlist_before(sch: Schematic):
    try:
        root = sch.project.root_schematic or sch.path
        return netlist_mod.load_netlist(root, include_components=True, max_nets=5000)
    except LayerError:
        return None


def _delta(before, after) -> NetlistDelta | None:
    if before is None or after is None:
        return None
    b = {n.name: {(x.ref, x.pin) for x in n.nodes} for n in before.nets}
    a = {n.name: {(x.ref, x.pin) for x in n.nodes} for n in after.nets}
    bc = {c.ref for c in before.components}
    ac = {c.ref for c in after.components}
    return NetlistDelta(
        nets_added=sorted(set(a) - set(b)),
        nets_removed=sorted(set(b) - set(a)),
        nets_changed=sorted(n for n in set(a) & set(b) if a[n] != b[n]),
        components_added=sorted(ac - bc),
        components_removed=sorted(bc - ac),
    )


def _finish(sch: Schematic, summary: str, *, dry_run: bool, uuids: list[str], pins: Pins | None = None, force: bool = False, validate: bool = True, before=None) -> EditResult:
    warnings: list[str] = []
    if dry_run:
        text = sch.render()
        changed = text != sch.source
        return EditResult(changed=changed, dry_run=True, file=display(sch.path), summary=summary + (" (dry run, nothing written)" if changed else " (no change)"),
                          uuids=uuids, pins=_pins(pins) if pins else [], bytes_before=len(sch.source.encode()), bytes_after=len(text.encode()))
    saved = sch.save(force=force)
    if not saved.changed:
        return EditResult(changed=False, dry_run=False, file=display(sch.path), summary=summary + " (no change)", uuids=uuids)
    if sch.lock_file().exists():
        warnings.append("KiCad still has this sheet open: use File > Revert in the Schematic Editor to see the change.")
    erc = None
    delta = None
    if validate:
        root = sch.project.root_schematic or sch.path
        try:
            erc = reports.run_erc(root, severity="all")
        except LayerError as exc:
            warnings.append(f"ERC could not run: {exc}")
        try:
            after = netlist_mod.load_netlist(root, refresh=True, include_components=True, max_nets=5000)
            delta = _delta(before, after)
        except LayerError as exc:
            warnings.append(f"Netlist could not be exported: {exc}")
    return EditResult(
        changed=True, dry_run=False, file=display(sch.path), summary=summary, uuids=uuids, pins=_pins(pins) if pins else [],
        snapshot=str(saved.snapshot) if saved.snapshot else None, sha256_before=saved.sha256_before, sha256_after=saved.sha256_after,
        bytes_before=saved.bytes_before, bytes_after=saved.bytes_after, erc=erc, netlist_delta=delta, warnings=warnings,
    )


def set_property(schematic_path: str, ref: str, name: str, new_value: str, *, dry_run: bool = False, force: bool = False) -> EditResult:
    require_write_mode("sch_set_property")
    sch = load(schematic_path)
    before = _netlist_before(sch)
    old, changed = sch.set_property(ref, name, new_value)
    summary = f"{ref}.{name}: {old!r} -> {new_value!r}" if old is not None else f"{ref}.{name} added = {new_value!r}"
    return _finish(sch, summary, dry_run=dry_run, uuids=[sch.find(new_value if name == 'Reference' else ref).uuid], force=force, validate=name in ("Reference", "Value"), before=before)


def add_component(schematic_path: str, lib_id: str, ref: str, x_mm: float, y_mm: float, *, rotation: int = 0, mirror: str | None = None,
                  value: str | None = None, footprint: str | None = None, unit: int = 1, snap_to_grid: bool = True, dry_run: bool = False, force: bool = False) -> EditResult:
    require_write_mode("sch_add_component")
    sch = load(schematic_path)
    if any((sch._instance_reference(v.node) or v.reference) == ref for v in sch.placed()) and not ref.endswith("?"):
        raise LayerError("EDIT_CONFLICT", f"Reference {ref} already exists on {sch.path.name}.", hint="Pick a free reference, or use a prefix with '?' and run sch_annotate.")
    at: Point = (snap(x_mm), snap(y_mm)) if snap_to_grid else (x_mm, y_mm)
    before = _netlist_before(sch)
    try:
        u, pins = sch.add_symbol(lib_id, ref, at, rotation=rotation, mirror=mirror, value_text=value, footprint=footprint, unit=unit)
    except (KeyError, FileNotFoundError) as exc:
        raise LayerError("NOT_FOUND_IN_DESIGN", f"Cannot place {lib_id}: {exc}", hint="Use lib_search to find the exact lib_id; project libraries need the project's tables.") from exc
    return _finish(sch, f"placed {lib_id} as {ref} at ({at[0]}, {at[1]}) rot {rotation}", dry_run=dry_run, uuids=[u], pins=pins, force=force, before=before)


def wire(schematic_path: str, points: list[list[float]], *, snap_to_grid: bool = True, add_junctions: bool = True, dry_run: bool = False, force: bool = False) -> EditResult:
    require_write_mode("sch_wire")
    if len(points) < 2:
        raise LayerError("INVALID_ARGUMENT", "A wire needs at least two points.")
    sch = load(schematic_path)
    before = _netlist_before(sch)
    pts: list[Point] = [(snap(p[0]), snap(p[1])) if snap_to_grid else (float(p[0]), float(p[1])) for p in points]
    uuids: list[str] = []
    junctions: list[Point] = []
    if add_junctions:
        for end in (pts[0], pts[-1]):
            if sch.needs_junction(end):
                junctions.append(end)
    for a, b in zip(pts, pts[1:]):
        if a != b:
            uuids.append(sch.add_wire(a, b))
    for j in junctions:
        uuids.append(sch.add_junction(j))
    summary = f"wire through {len(pts)} points" + (f", {len(junctions)} junction(s) added" if junctions else "")
    return _finish(sch, summary, dry_run=dry_run, uuids=uuids, force=force, before=before)


def label(schematic_path: str, text: str, x_mm: float, y_mm: float, *, rotation: int = 0, kind: str = "local", shape: str = "input",
          snap_to_grid: bool = True, dry_run: bool = False, force: bool = False) -> EditResult:
    require_write_mode("sch_label")
    sch = load(schematic_path)
    before = _netlist_before(sch)
    p: Point = (snap(x_mm), snap(y_mm)) if snap_to_grid else (x_mm, y_mm)
    u = sch.add_label(text, p, rotation, kind, shape)
    return _finish(sch, f"{kind} label {text!r} at {p}", dry_run=dry_run, uuids=[u], force=force, before=before)


def mark(schematic_path: str, kind: str, x_mm: float, y_mm: float, *, snap_to_grid: bool = True, dry_run: bool = False, force: bool = False) -> EditResult:
    require_write_mode("sch_mark")
    sch = load(schematic_path)
    before = _netlist_before(sch)
    p: Point = (snap(x_mm), snap(y_mm)) if snap_to_grid else (x_mm, y_mm)
    if kind == "junction":
        u = sch.add_junction(p)
    elif kind == "no_connect":
        u = sch.add_no_connect(p)
    else:
        raise LayerError("INVALID_ARGUMENT", f"kind must be junction or no_connect, not {kind!r}.")
    return _finish(sch, f"{kind} at {p}", dry_run=dry_run, uuids=[u], force=force, before=before)


def delete(schematic_path: str, item_uuid: str, *, dry_run: bool = False, force: bool = False) -> EditResult:
    require_write_mode("sch_delete")
    sch = load(schematic_path)
    before = _netlist_before(sch)
    kind = sch.delete(item_uuid)
    return _finish(sch, f"deleted {kind} {item_uuid}", dry_run=dry_run, uuids=[item_uuid], force=force, before=before)


def annotate(schematic_path: str, *, dry_run: bool = False, force: bool = False) -> AnnotateResult:
    require_write_mode("sch_annotate")
    sch = load(schematic_path)
    changes = sch.annotate()
    if not changes:
        return AnnotateResult(changed=False, dry_run=dry_run, file=display(sch.path), assignments={})
    if dry_run:
        return AnnotateResult(changed=True, dry_run=True, file=display(sch.path), assignments=changes)
    saved = sch.save(force=force)
    warnings = []
    if sch.lock_file().exists():
        warnings.append("KiCad still has this sheet open: use File > Revert in the Schematic Editor to see the change.")
    return AnnotateResult(changed=saved.changed, dry_run=False, file=display(sch.path), assignments=changes, snapshot=str(saved.snapshot) if saved.snapshot else None, warnings=warnings)
