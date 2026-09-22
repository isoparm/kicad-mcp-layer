"""Edit the board open in KiCad's PCB Editor, one undo step per operation.

Every mutation runs inside a commit: begin, change, push with a message that appears in
KiCad's undo history, drop on any failure. After the push the affected items are read back
over the API and reported, so the result describes what KiCad holds, not what was asked.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from kicad_layer.errors import INVALID_ARGUMENT, IPC_REJECTED, NOT_FOUND_IN_DESIGN, LayerError
from kicad_layer.ipc.board_read import layer_id, layer_name, mm
from kicad_layer.ipc.session import Session, get_session
from kicad_layer.kicad_libs import load_footprint
from kicad_layer.pcb_writer import BoardBuilder
from kicad_layer.sexpr import dumps

Point = tuple[float, float]


def _kiid(item) -> str:
    """The bare uuid string of an item's KIID."""
    kid = getattr(item, "id", None)
    return str(getattr(kid, "value", kid) or "")


def _nm(v: float) -> int:
    return int(round(v * 1_000_000))


def _vec(x: float, y: float):
    from kipy.geometry import Vector2

    return Vector2.from_xy(_nm(x), _nm(y))


def _net(name: str):
    from kipy.board_types import Net

    try:
        return Net(name=name)
    except TypeError:
        n = Net()
        n.name = name
        return n


def _commit(session: Session, board, message: str, fn):
    """Run ``fn()`` inside one KiCad commit; push on success, drop on failure."""

    def work():
        commit = board.begin_commit()
        try:
            result = fn()
        except Exception:
            try:
                board.drop_commit(commit)
            except Exception:
                pass
            raise
        board.push_commit(commit, message)
        return result

    return session.call(work, label=f"commit: {message}")


def _fp_row(fp) -> dict[str, Any]:
    return {
        "kind": "footprint", "id": _kiid(fp), "ref": fp.reference_field.text.value,
        "x_mm": mm(fp.position.x), "y_mm": mm(fp.position.y), "rotation_deg": round(fp.orientation.degrees, 3), "layer": layer_name(fp.layer),
    }


def _track_row(t) -> dict[str, Any]:
    return {"kind": "track", "id": _kiid(t), "x1_mm": mm(t.start.x), "y1_mm": mm(t.start.y), "x2_mm": mm(t.end.x), "y2_mm": mm(t.end.y),
            "width_mm": mm(t.width), "layer": layer_name(t.layer), "net": getattr(getattr(t, "net", None), "name", None)}


def _via_row(v) -> dict[str, Any]:
    return {"kind": "via", "id": _kiid(v), "x_mm": mm(v.position.x), "y_mm": mm(v.position.y), "diameter_mm": mm(v.diameter), "drill_mm": mm(v.drill_diameter),
            "net": getattr(getattr(v, "net", None), "name", None)}


def find_footprint(board, ref: str):
    for fp in board.get_footprints():
        if fp.reference_field.text.value == ref:
            return fp
    refs = sorted(fp.reference_field.text.value for fp in board.get_footprints())
    raise LayerError(NOT_FOUND_IN_DESIGN, f"No footprint {ref!r} on the open board.", hint=f"References: {', '.join(refs[:40])}")


# --------------------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------------------


_SHAPES = {"circle": "PSS_CIRCLE", "rect": "PSS_RECTANGLE", "oval": "PSS_OVAL", "roundrect": "PSS_ROUNDRECT", "trapezoid": "PSS_TRAPEZOID", "chamfered_rect": "PSS_CHAMFEREDRECT", "custom": "PSS_CUSTOM"}


def _expand_layers(names: list[str]) -> list[int]:
    out: list[str] = []
    for n in names:
        if n == "*.Cu":
            out += ["F.Cu", "B.Cu"]
        elif n == "*.Mask":
            out += ["F.Mask", "B.Mask"]
        elif n == "*.Paste":
            out += ["F.Paste", "B.Paste"]
        elif n.startswith("*."):
            out += ["F." + n[2:], "B." + n[2:]]
        else:
            out.append(n)
    ids = []
    for n in out:
        try:
            ids.append(layer_id(n))
        except LayerError:
            pass
    return ids


def _build_instance(fp_def, ref: str, value_text: str, at: Point, rotation: float, layer: str):
    """A kipy FootprintInstance built from a library footprint file.

    KiCad 10's API cannot place from a library, and its parse-from-text handler is a stub, so
    the pads, outlines and fields are constructed one by one. Children are created in
    absolute, unrotated coordinates; setting the orientation last makes kicad-python rotate
    them, pads' padstack angles included. Polygons and footprint texts other than the two
    fields are not carried over; pads, lines, arcs, circles and rectangles are.
    """
    from kipy.board_types import (
        BoardArc, BoardCircle, BoardLayer, BoardRectangle, BoardSegment, FootprintInstance, Pad, PadStackShape, PadType,
    )
    from kipy.common_types import LibraryIdentifier
    from kipy.geometry import Angle, Vector2
    from kipy.proto.board.board_types_pb2 import DrillShape, FootprintMountingStyle, PadStackType

    from kicad_layer.sexpr import child, children, tag, value

    fx, fy = at
    inst = FootprintInstance()
    inst.position = Vector2.from_xy(_nm(fx), _nm(fy))
    inst.layer = layer_id(layer)
    lid = LibraryIdentifier()
    lid.library = fp_def.lib
    lid.name = fp_def.name
    inst.definition.id = lid
    attr_node = child(fp_def.tree, "attr")
    attrs = [str(a) for a in attr_node[1:]] if attr_node is not None else []
    inst.attributes.mounting_style = FootprintMountingStyle.FMS_THROUGH_HOLE if "through_hole" in attrs else FootprintMountingStyle.FMS_SMD
    inst.attributes.not_in_schematic = True
    if "exclude_from_bom" in attrs:
        inst.attributes.exclude_from_bill_of_materials = True

    def field(f, text: str, dy_mm: float, lyr: str, visible: bool):
        f.text.value = text
        f.text.position = Vector2.from_xy(_nm(fx), _nm(fy + dy_mm))
        f.text.layer = layer_id(lyr)
        f.text.attributes.size = Vector2.from_xy(_nm(1.0), _nm(1.0))
        f.text.attributes.stroke_width = _nm(0.15)
        f.visible = visible
        return f

    inst.reference_field = field(inst.reference_field, ref, -2.0, "F.SilkS", True)
    inst.value_field = field(inst.value_field, value_text, 2.0, "F.Fab", True)

    # pads
    for p in fp_def.pads:
        pad = Pad()
        pad.number = p.number
        pad.position = Vector2.from_xy(_nm(fx + p.x), _nm(fy + p.y))
        pad.pad_type = {"smd": PadType.PT_SMD, "thru_hole": PadType.PT_PTH, "np_thru_hole": PadType.PT_NPTH}.get(p.kind, PadType.PT_SMD)
        ps = pad.padstack
        ps.type = PadStackType.PST_NORMAL
        ps.layers = _expand_layers(p.layers)
        if p.rotation:
            ps.angle = Angle.from_degrees(p.rotation)
        cl = ps.copper_layers
        if cl:
            top = cl[0]
            top.shape = getattr(PadStackShape, _SHAPES.get(p.shape, "PSS_RECTANGLE"))
            top.size = Vector2.from_xy(_nm(p.size[0]), _nm(p.size[1]))
            if p.shape == "roundrect":
                top.corner_rounding_ratio = 0.25
        if p.drill:
            ps.drill.diameter = Vector2.from_xy(_nm(p.drill), _nm(p.drill))
            ps.drill.shape = DrillShape.DS_CIRCLE
            ps.drill.start_layer = BoardLayer.BL_F_Cu
            ps.drill.end_layer = BoardLayer.BL_B_Cu
        inst.definition.add_item(pad)

    # graphics: lines, rectangles, circles, arcs (polygons and texts are skipped)
    def pt(node_name: str, g):
        c = child(g, node_name)
        return Vector2.from_xy(_nm(fx + float(c[1])), _nm(fy + float(c[2]))) if c is not None and len(c) > 2 else None

    def stroke_width(g) -> int:
        s = child(g, "stroke")
        w = value(s, "width") if s is not None else None
        return _nm(float(w)) if w else _nm(0.12)

    for g in fp_def.tree:
        if not isinstance(g, list):
            continue
        t = tag(g)
        lyr_name = value(g, "layer")
        if t not in ("fp_line", "fp_rect", "fp_circle", "fp_arc") or not lyr_name:
            continue
        try:
            lyr = layer_id(lyr_name)
        except LayerError:
            continue
        shape = None
        if t == "fp_line":
            shape = BoardSegment()
            shape.start, shape.end = pt("start", g), pt("end", g)
        elif t == "fp_rect":
            shape = BoardRectangle()
            shape.top_left, shape.bottom_right = pt("start", g), pt("end", g)
        elif t == "fp_circle":
            shape = BoardCircle()
            shape.center, shape.radius_point = pt("center", g), pt("end", g)
        elif t == "fp_arc":
            shape = BoardArc()
            shape.start, shape.mid, shape.end = pt("start", g), pt("mid", g), pt("end", g)
        if shape is None:
            continue
        shape.layer = lyr
        shape.attributes.stroke.width = stroke_width(g)
        inst.definition.add_item(shape)

    if rotation:
        inst.orientation = Angle.from_degrees(rotation)
    return inst


def place_footprint(board_path: Path | None, lib_id: str, ref: str, at: Point, rotation: float = 0.0, *, value_text: str = "", layer: str = "F.Cu") -> dict[str, Any]:
    session = get_session()
    board, path = session.board(board_path)
    lib, _, name = lib_id.partition(":")
    fp_def = load_footprint(lib, name)

    def exists(refname: str) -> bool:
        return any(fp.reference_field.text.value == refname for fp in board.get_footprints())

    if session.call(lambda: exists(ref)):
        raise LayerError(INVALID_ARGUMENT, f"Reference {ref} already exists on the open board.")

    def create():
        inst = _build_instance(fp_def, ref, value_text or name, at, rotation, layer)
        created = board.create_items(inst)
        if not created:
            raise LayerError(IPC_REJECTED, f"KiCad did not create {ref}; the footprint may contain a pad or shape it rejects.")
        return created

    _commit(session, board, f"kicad-mcp-layer: place {ref} ({lib_id})", create)
    fp = session.call(lambda: find_footprint(board, ref))
    row = _fp_row(fp)
    warnings = ["Placed through the API without a schematic symbol: pads have no nets and the footprint is marked 'not in schematic'."]
    skipped = sum(1 for g in fp_def.tree if isinstance(g, list) and str(g[0]) in ("fp_poly", "fp_text", "fp_text_box"))
    if skipped:
        warnings.append(f"{skipped} polygon or text item(s) of the library footprint were not carried over (KiCad 10 API limitation).")
    if abs(row["x_mm"] - at[0]) > 0.01 or abs(row["y_mm"] - at[1]) > 0.01:
        warnings.append(f"KiCad placed {ref} at ({row['x_mm']}, {row['y_mm']}), not at the requested point; use pcb_move_footprint.")
    return {"board": path, "items": [row], "warnings": warnings}


def move_footprint(board_path: Path | None, ref: str, at: Point | None = None, rotation: float | None = None, layer: str | None = None) -> dict[str, Any]:
    from kipy.geometry import Angle

    session = get_session()
    board, path = session.board(board_path)
    fp = session.call(lambda: find_footprint(board, ref))
    before = _fp_row(fp)

    def change():
        target = find_footprint(board, ref)
        if at is not None:
            target.position = _vec(*at)
        if rotation is not None:
            target.orientation = Angle.from_degrees(rotation)
        updated = board.update_items(target)
        if not updated:
            raise LayerError(IPC_REJECTED, f"KiCad did not accept the update of {ref}.")
        return updated

    _commit(session, board, f"kicad-mcp-layer: move {ref}", change)
    if layer is not None and layer != before["layer"]:
        _commit(session, board, f"kicad-mcp-layer: flip {ref}", lambda: board.flip_items(find_footprint(board, ref)))
    after = _fp_row(session.call(lambda: find_footprint(board, ref)))
    return {"board": path, "items": [after], "before": before}


def move_footprints(board_path: Path | None, moves: list[tuple[str, Point | None, float | None, str | None]]) -> dict[str, Any]:
    """Every move and flip in one commit, so the batch is one undo step and one round of redraws."""
    from kipy.geometry import Angle

    session = get_session()
    board, path = session.board(board_path)

    def lookup():
        by_ref = {fp.reference_field.text.value: fp for fp in board.get_footprints()}
        missing = [m[0] for m in moves if m[0] not in by_ref]
        if missing:
            raise LayerError(NOT_FOUND_IN_DESIGN, f"No footprint {', '.join(missing)} on the open board; nothing was moved.")
        return by_ref

    before = {ref: _fp_row(fp) for ref, fp in session.call(lookup).items() if ref in {m[0] for m in moves}}

    def change():
        by_ref = lookup()
        targets = []
        for ref, at, rotation, _ in moves:
            t = by_ref[ref]
            if at is not None:
                t.position = _vec(*at)
            if rotation is not None:
                t.orientation = Angle.from_degrees(rotation)
            targets.append(t)
        updated = board.update_items(targets)
        if len(updated) != len(targets):
            raise LayerError(IPC_REJECTED, f"KiCad accepted {len(updated)} of {len(targets)} footprint updates; the commit was dropped.")
        flips = [by_ref[ref] for ref, _, _, side in moves if side is not None and side != before[ref]["layer"]]
        if flips:
            board.flip_items(flips)
        return updated

    _commit(session, board, f"kicad-mcp-layer: move {len(moves)} footprints", change)
    after = session.call(lookup)
    rows = [_fp_row(after[ref]) for ref, *_ in moves]
    report = [{"ref": r["ref"], "status": "moved" if any(r[k] != before[r["ref"]][k] for k in ("x_mm", "y_mm", "rotation_deg", "layer")) else "unchanged",
               "before": {k: before[r["ref"]][k] for k in ("x_mm", "y_mm", "rotation_deg", "layer")}, "after": {k: r[k] for k in ("x_mm", "y_mm", "rotation_deg", "layer")}}
              for r in rows]
    return {"board": path, "items": rows, "moves": report}


def add_track(board_path: Path | None, points: list[Point], *, width: float, layer: str, net: str) -> dict[str, Any]:
    from kipy.board_types import Track

    if len(points) < 2:
        raise LayerError(INVALID_ARGUMENT, "A track needs at least two points.")
    lid = layer_id(layer)
    session = get_session()
    board, path = session.board(board_path)

    def create():
        tracks = []
        for a, b in zip(points, points[1:]):
            if a == b:
                continue
            t = Track()
            t.start = _vec(*a)
            t.end = _vec(*b)
            t.width = _nm(width)
            t.layer = lid
            t.net = _net(net)
            tracks.append(t)
        created = board.create_items(tracks)
        if len(created) != len(tracks):
            raise LayerError(IPC_REJECTED, f"KiCad created {len(created)} of {len(tracks)} track segments.")
        return created

    created = _commit(session, board, f"kicad-mcp-layer: track on {net} ({len(points) - 1} segments)", create)
    return {"board": path, "items": [_track_row(t) for t in created]}


def add_via(board_path: Path | None, p: Point, *, net: str, size: float = 0.8, drill: float = 0.3) -> dict[str, Any]:
    from kipy.board_types import Via

    session = get_session()
    board, path = session.board(board_path)

    def create():
        v = Via()
        v.position = _vec(*p)
        v.diameter = _nm(size)
        v.drill_diameter = _nm(drill)
        v.net = _net(net)
        created = board.create_items(v)
        if not created:
            raise LayerError(IPC_REJECTED, "KiCad did not create the via.")
        return created

    created = _commit(session, board, f"kicad-mcp-layer: via on {net}", create)
    return {"board": path, "items": [_via_row(v) for v in created]}


def add_zone(board_path: Path | None, polygon: list[Point], *, net: str, layer: str, name: str = "", min_thickness: float = 0.25) -> dict[str, Any]:
    if len(polygon) < 3:
        raise LayerError(INVALID_ARGUMENT, "A zone needs at least three points.")
    lid = layer_id(layer)
    session = get_session()
    board, path = session.board(board_path)

    def create():
        from kipy.board_types import Zone
        from kipy.geometry import PolygonWithHoles, PolyLine, PolyLineNode

        try:
            outline = PolyLine()
            for x, y in polygon:
                outline.nodes.append(PolyLineNode.from_xy(_nm(x), _nm(y)))
            outline.closed = True
            poly = PolygonWithHoles()
            poly.outline = outline
            z = Zone()
            z.outline = poly
            z.layers = [lid]
            z.net = _net(net)
            if name:
                z.name = name
            z.min_thickness = _nm(min_thickness)
        except (AttributeError, TypeError) as exc:
            raise LayerError(IPC_REJECTED, f"Zone creation through the API is not available in this kicad-python build: {exc}",
                             hint="Close the board and use channel='file', then refill with pcb_refill_zones after reopening.") from exc
        created = board.create_items(z)
        if not created:
            raise LayerError(IPC_REJECTED, "KiCad did not create the zone.")
        return created

    created = _commit(session, board, f"kicad-mcp-layer: zone {name or net} on {layer}", create)
    return {"board": path, "items": [{"kind": "zone", "id": _kiid(z), "net": net, "layer": layer, "name": name} for z in created]}


def refill_zones(board_path: Path | None, *, max_seconds: float = 60.0) -> dict[str, Any]:
    session = get_session()
    board, path = session.board(board_path)

    def work():
        started = time.time()
        try:
            board.refill_zones(block=True, max_poll_seconds=max_seconds)
            return {"filled": True, "seconds": round(time.time() - started, 1)}
        except Exception as exc:  # kipy raises its ConnectionError on a long fill
            if "imed out" in str(exc) or "usy" in str(exc):
                return {"filled": False, "seconds": round(time.time() - started, 1), "note": "KiCad is still filling; poll with pcb_summary before running DRC."}
            raise

    result = session.call(work)
    return {"board": path, **result}


def delete_items(board_path: Path | None, ids: list[str]) -> dict[str, Any]:
    from kipy.proto.common.types.base_types_pb2 import KIID

    session = get_session()
    board, path = session.board(board_path)

    def work():
        kiids = [KIID(value=i) for i in ids]
        found = list(board.get_items_by_id(kiids))
        kinds = {_kiid(item): type(item).__name__ for item in found}
        missing = [i for i in ids if i not in kinds]
        if missing:
            raise LayerError(NOT_FOUND_IN_DESIGN, f"Items not on the open board: {', '.join(missing)}")
        return kinds

    kinds = session.call(work)

    def remove():
        from kipy.proto.common.types.base_types_pb2 import KIID as K

        board.remove_items_by_id([K(value=i) for i in ids])
        return True

    _commit(session, board, f"kicad-mcp-layer: delete {len(ids)} item(s)", remove)
    return {"board": path, "deleted": [{"id": i, "kind": kinds[i]} for i in ids]}


def save(board_path: Path | None) -> dict[str, Any]:
    session = get_session()
    board, path = session.board(board_path)
    before = path.stat().st_mtime_ns if path.exists() else None
    session.call(board.save)
    time.sleep(0.2)
    after = path.stat().st_mtime_ns if path.exists() else None
    return {"board": path, "written": after is not None and after != before, "size": path.stat().st_size if path.exists() else None}
