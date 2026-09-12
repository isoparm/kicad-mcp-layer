"""Read the board that is open in KiCad's PCB Editor.

Everything here is a pure read: no commit, no save, nothing changes in KiCad. Values are
converted at the boundary: nanometre integers become millimetres, enums become their KiCad
names, layers become canonical names such as ``F.Cu``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from kicad_layer.errors import INVALID_ARGUMENT, LayerError
from kicad_layer.ipc.session import get_session
from kicad_layer.models import (
    BoardItems,
    BoardSummary,
    DiffPairCandidate,
    FootprintItem,
    NetClassInfo,
    NetItem,
    NetStat,
    NetStats,
    PadItem,
    StackupLayer,
    TextItem,
    TitleBlock,
    TrackItem,
    ViaItem,
    ZoneItem,
)

def _kiid(item) -> str:
    """The bare uuid string of an item's KIID."""
    kid = getattr(item, "id", None)
    return str(getattr(kid, "value", kid) or "")


ITEM_KINDS = ("footprint", "pad", "track", "via", "zone", "net", "text")


def mm(nm: int | float | None) -> float | None:
    return None if nm is None else round(nm / 1_000_000, 4)


def layer_name(layer: int | None) -> str:
    from kipy.util.board_layer import canonical_name

    if layer is None:
        return ""
    try:
        return canonical_name(layer)
    except Exception:
        return str(layer)


def layer_id(name: str) -> int:
    from kipy.util.board_layer import canonical_name, layer_from_canonical_name

    bad = LayerError(
        INVALID_ARGUMENT,
        f"{name!r} is not a KiCad layer name.",
        hint="Use canonical names such as F.Cu, B.Cu, In1.Cu, F.SilkS, Edge.Cuts.",
    )
    try:
        lid = layer_from_canonical_name(name)
        # The helper hands back a placeholder for unknown names; round-trip to be sure.
        if lid is None or canonical_name(lid) != name:
            raise bad
        return lid
    except LayerError:
        raise
    except Exception as exc:
        raise bad from exc


def _enum_name(enum_cls: Any, value: int, prefix: str) -> str:
    try:
        return enum_cls.Name(value).removeprefix(prefix).lower()
    except Exception:
        return str(value)


def _net_name(item: Any) -> str | None:
    net = getattr(item, "net", None)
    name = getattr(net, "name", None) if net is not None else None
    return name or None


def _display(path: Path) -> str:
    from kicad_layer.paths import display

    return display(path)


# --------------------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------------------


def _bbox_mm(boxes: list[Any]) -> dict[str, float] | None:
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return None
    x0 = min(b.pos.x for b in boxes)
    y0 = min(b.pos.y for b in boxes)
    x1 = max(b.pos.x + b.size.x for b in boxes)
    y1 = max(b.pos.y + b.size.y for b in boxes)
    return {"x_mm": mm(x0), "y_mm": mm(y0), "width_mm": mm(x1 - x0), "height_mm": mm(y1 - y0)}


def summary(board_path: Path | None = None) -> BoardSummary:
    from kipy.board_types import BoardLayer
    from kipy.proto.board.board_pb2 import BoardStackupLayerType

    session = get_session()
    board, path = session.board(board_path)

    def work() -> BoardSummary:
        notes: list[str] = []
        footprints = board.get_footprints()
        pads = board.get_pads()
        tracks = board.get_tracks()
        vias = board.get_vias()
        zones = board.get_zones()
        nets = [n for n in board.get_nets() if getattr(n, "name", "")]
        shapes = board.get_shapes()
        texts = board.get_text()
        try:
            groups = board.get_groups()
        except Exception:
            groups = []

        tb = board.get_title_block_info()
        raw_comments = getattr(tb, "comments", None)
        if isinstance(raw_comments, dict):
            raw_comments = [raw_comments[k] for k in sorted(raw_comments)]
        comments = [str(c) for c in (raw_comments or []) if isinstance(c, str) and c.strip()]
        title_block = TitleBlock(
            title=getattr(tb, "title", "") or None,
            date=getattr(tb, "date", "") or None,
            revision=getattr(tb, "revision", "") or None,
            company=getattr(tb, "company", "") or None,
            comments=comments,
        )

        stack: list[StackupLayer] = []
        try:
            for sl in board.get_stackup().layers:
                stack.append(
                    StackupLayer(
                        layer=layer_name(getattr(sl, "layer", None)),
                        type=_enum_name(BoardStackupLayerType, getattr(sl, "type", 0), "BSLT_"),
                        thickness_mm=mm(getattr(sl, "thickness", None)),
                        material=getattr(sl, "material_name", "") or None,
                        user_name=getattr(sl, "user_name", "") or None,
                        enabled=bool(getattr(sl, "enabled", True)),
                    )
                )
        except Exception as exc:
            notes.append(f"Stackup unavailable: {exc}")

        edge = [s for s in shapes if getattr(s, "layer", None) == BoardLayer.BL_Edge_Cuts]
        size = None
        outline_source = "none"
        if edge:
            boxes = board.get_item_bounding_box(edge)
            size = _bbox_mm(boxes if isinstance(boxes, list) else [boxes])
            outline_source = "Edge.Cuts"
        if size is None and (footprints or tracks):
            items = list(footprints) + list(tracks)
            boxes = board.get_item_bounding_box(items)
            size = _bbox_mm(boxes if isinstance(boxes, list) else [boxes])
            outline_source = "footprints and tracks (no Edge.Cuts outline)"
            notes.append("The board has no Edge.Cuts outline; size is the extent of placed items.")

        netclasses: list[NetClassInfo] = []
        if nets:
            try:
                by_net = board.get_netclass_for_nets(nets)
                counts: dict[str, int] = defaultdict(int)
                classes: dict[str, Any] = {}
                for net_name, nc in by_net.items():
                    counts[nc.name] += 1
                    classes[nc.name] = nc
                for name, nc in sorted(classes.items()):
                    netclasses.append(
                        NetClassInfo(
                            name=name,
                            net_count=counts[name],
                            clearance_mm=mm(getattr(nc, "clearance", None)),
                            track_width_mm=mm(getattr(nc, "track_width", None)),
                            via_diameter_mm=mm(getattr(nc, "via_diameter", None)),
                            via_drill_mm=mm(getattr(nc, "via_drill", None)),
                            diff_pair_width_mm=mm(getattr(nc, "diff_pair_track_width", None)),
                            diff_pair_gap_mm=mm(getattr(nc, "diff_pair_gap", None)),
                        )
                    )
            except Exception as exc:
                notes.append(f"Netclasses unavailable: {exc}")

        notes.append("Board design rules (min clearance, min track width) are not readable through the API in KiCad 10; run_drc applies them.")
        return BoardSummary(
            board_path=_display(path),
            project=path.parent.name,
            title_block=title_block,
            copper_layer_count=board.get_copper_layer_count(),
            enabled_layers=[layer_name(l) for l in board.get_enabled_layers()],
            size=size,
            outline_source=outline_source,
            counts={
                "footprints": len(footprints),
                "pads": len(pads),
                "tracks": len(tracks),
                "vias": len(vias),
                "zones": len(zones),
                "nets": len(nets),
                "shapes": len(shapes),
                "texts": len(texts),
                "groups": len(groups),
            },
            stackup=stack,
            netclasses=netclasses,
            notes=notes,
        )

    return session.call(work)


# --------------------------------------------------------------------------------------
# items
# --------------------------------------------------------------------------------------


def _footprint_items(board: Any) -> list[FootprintItem]:
    out: list[FootprintItem] = []
    for fp in board.get_footprints():
        definition = getattr(fp, "definition", None)
        lib_id = getattr(definition, "id", None)
        attrs = getattr(fp, "attributes", None)
        out.append(
            FootprintItem(
                id=_kiid(fp),
                ref=fp.reference_field.text.value,
                value=fp.value_field.text.value,
                library=getattr(lib_id, "library", None),
                name=getattr(lib_id, "name", None),
                x_mm=mm(fp.position.x),
                y_mm=mm(fp.position.y),
                rotation_deg=round(fp.orientation.degrees, 3),
                layer=layer_name(fp.layer),
                locked=bool(getattr(fp, "locked", False)),
                dnp=bool(getattr(attrs, "do_not_populate", False)),
                exclude_from_bom=bool(getattr(attrs, "exclude_from_bill_of_materials", False)),
                pad_count=len(getattr(definition, "pads", []) or []),
            )
        )
    return out


def _pad_items(board: Any) -> list[PadItem]:
    from kipy.board_types import PadStackShape, PadType

    refs = {_kiid(fp): fp.reference_field.text.value for fp in board.get_footprints()}
    out: list[PadItem] = []
    for pad in board.get_pads():
        padstack = getattr(pad, "padstack", None)
        shape = size_x = size_y = drill = None
        try:
            layers = list(getattr(padstack, "copper_layers", []) or [])
            if layers:
                shape = _enum_name(PadStackShape, layers[0].shape, "PSS_")
                size_x, size_y = mm(layers[0].size.x), mm(layers[0].size.y)
        except Exception:
            pass
        try:
            d = padstack.drill.diameter
            drill = mm(d.x) if d.x else None
        except Exception:
            pass
        parent = getattr(pad, "parent", None)
        out.append(
            PadItem(
                id=_kiid(pad),
                footprint_ref=refs.get(str(parent)) if parent is not None else None,
                number=str(getattr(pad, "number", "")),
                x_mm=mm(pad.position.x),
                y_mm=mm(pad.position.y),
                net=_net_name(pad),
                pad_type=_enum_name(PadType, getattr(pad, "pad_type", 0), "PT_"),
                shape=shape,
                size_x_mm=size_x,
                size_y_mm=size_y,
                drill_mm=drill,
            )
        )
    return out


def _track_items(board: Any) -> list[TrackItem]:
    out: list[TrackItem] = []
    for t in board.get_tracks():
        is_arc = hasattr(t, "mid")
        out.append(
            TrackItem(
                id=_kiid(t),
                x1_mm=mm(t.start.x),
                y1_mm=mm(t.start.y),
                x2_mm=mm(t.end.x),
                y2_mm=mm(t.end.y),
                width_mm=mm(t.width),
                layer=layer_name(t.layer),
                net=_net_name(t),
                length_mm=mm(t.length()),
                arc=is_arc,
            )
        )
    return out


def _via_items(board: Any) -> list[ViaItem]:
    from kipy.board_types import ViaType

    out: list[ViaItem] = []
    for v in board.get_vias():
        start = end = None
        try:
            drill = v.padstack.drill
            start, end = layer_name(drill.start_layer), layer_name(drill.end_layer)
        except Exception:
            pass
        out.append(
            ViaItem(
                id=_kiid(v),
                x_mm=mm(v.position.x),
                y_mm=mm(v.position.y),
                diameter_mm=mm(getattr(v, "diameter", None)),
                drill_mm=mm(getattr(v, "drill_diameter", None)),
                net=_net_name(v),
                via_type=_enum_name(ViaType, getattr(v, "type", 0), "VT_"),
                start_layer=start,
                end_layer=end,
            )
        )
    return out


def _zone_items(board: Any) -> list[ZoneItem]:
    from kipy.board_types import ZoneType

    out: list[ZoneItem] = []
    for z in board.get_zones():
        out.append(
            ZoneItem(
                id=_kiid(z),
                name=getattr(z, "name", "") or None,
                net=_net_name(z),
                layers=[layer_name(l) for l in getattr(z, "layers", [])],
                zone_type=_enum_name(ZoneType, getattr(z, "type", 0), "ZT_"),
                filled=bool(getattr(z, "filled", False)),
                priority=int(getattr(z, "priority", 0) or 0),
                rule_area=bool(z.is_rule_area()) if hasattr(z, "is_rule_area") else False,
            )
        )
    return out


def _net_items(board: Any) -> list[NetItem]:
    nets = [n for n in board.get_nets() if getattr(n, "name", "")]
    classes: dict[str, Any] = {}
    try:
        classes = board.get_netclass_for_nets(nets) if nets else {}
    except Exception:
        classes = {}
    return [NetItem(name=n.name, netclass=getattr(classes.get(n.name), "name", None)) for n in nets]


def _text_items(board: Any) -> list[TextItem]:
    out: list[TextItem] = []
    for t in board.get_text():
        pos = getattr(t, "position", None) or getattr(t, "top_left", None)
        out.append(
            TextItem(
                id=_kiid(t),
                value=getattr(t, "value", ""),
                layer=layer_name(getattr(t, "layer", None)),
                x_mm=mm(pos.x) if pos is not None else None,
                y_mm=mm(pos.y) if pos is not None else None,
                locked=bool(getattr(t, "locked", False)),
            )
        )
    return out


def list_items(
    kind: str,
    *,
    board_path: Path | None = None,
    net: str | None = None,
    layer: str | None = None,
    ref: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> BoardItems:
    if kind not in ITEM_KINDS:
        raise LayerError(INVALID_ARGUMENT, f"kind must be one of {', '.join(ITEM_KINDS)}, not {kind!r}.")
    if layer:
        layer_id(layer)  # validates early, before any IPC round trip
    session = get_session()
    board, path = session.board(board_path)

    def work() -> BoardItems:
        readers = {
            "footprint": _footprint_items,
            "pad": _pad_items,
            "track": _track_items,
            "via": _via_items,
            "zone": _zone_items,
            "net": _net_items,
            "text": _text_items,
        }
        items: list[Any] = readers[kind](board)
        if net:
            items = [i for i in items if getattr(i, "net", None) == net]
        if layer:
            items = [i for i in items if layer in (getattr(i, "layers", None) or [getattr(i, "layer", None)])]
        if ref:
            items = [
                i for i in items
                if str(getattr(i, "ref", None) or getattr(i, "footprint_ref", None) or "").upper().startswith(ref.upper())
            ]
        total = len(items)
        page = items[offset : offset + limit]
        return BoardItems(
            board_path=_display(path),
            kind=kind,
            total=total,
            offset=offset,
            returned=len(page),
            truncated=offset + len(page) < total,
            items=page,
        )

    return session.call(work)


# --------------------------------------------------------------------------------------
# net statistics
# --------------------------------------------------------------------------------------

_PAIR_RE = re.compile(r"^(?P<stem>.+?)(?P<pol>[PN]|[+-]|_P|_N|_DP|_DN)$", re.IGNORECASE)


def _pair_key(name: str) -> tuple[str, str] | None:
    m = _PAIR_RE.match(name)
    if not m:
        return None
    pol = m.group("pol").upper()
    positive = pol in ("P", "+", "_P", "_DP")
    return m.group("stem"), "P" if positive else "N"


def net_stats(*, board_path: Path | None = None, net: str | None = None, limit: int = 200) -> NetStats:
    session = get_session()
    board, path = session.board(board_path)

    def work() -> NetStats:
        stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"track_count": 0, "length": 0.0, "widths": set(), "layers": set(), "via_count": 0, "pad_count": 0}
        )
        for t in board.get_tracks():
            n = _net_name(t)
            if not n:
                continue
            s = stats[n]
            s["track_count"] += 1
            s["length"] += t.length()
            s["widths"].add(t.width)
            s["layers"].add(layer_name(t.layer))
        for v in board.get_vias():
            n = _net_name(v)
            if n:
                stats[n]["via_count"] += 1
        for p in board.get_pads():
            n = _net_name(p)
            if n:
                stats[n]["pad_count"] += 1
        classes: dict[str, Any] = {}
        try:
            nets = [x for x in board.get_nets() if getattr(x, "name", "")]
            classes = board.get_netclass_for_nets(nets) if nets else {}
        except Exception:
            classes = {}

        rows: list[NetStat] = []
        for name in sorted(stats):
            if net and name != net:
                continue
            s = stats[name]
            hint = None
            if s["pad_count"] >= 2 and s["track_count"] == 0:
                hint = "pads on this net but no tracks: probably unrouted"
            rows.append(
                NetStat(
                    net=name,
                    netclass=getattr(classes.get(name), "name", None),
                    track_count=s["track_count"],
                    total_length_mm=mm(s["length"]) or 0.0,
                    widths_mm=sorted(mm(w) for w in s["widths"]),
                    layers=sorted(s["layers"]),
                    via_count=s["via_count"],
                    pad_count=s["pad_count"],
                    routing_hint=hint,
                )
            )
        if net and not rows:
            raise LayerError(INVALID_ARGUMENT, f"No net named {net!r} has pads, tracks or vias on this board.", hint="Use pcb_list_items with kind='net' to see net names.")

        pairs: dict[str, dict[str, NetStat]] = defaultdict(dict)
        for row in rows:
            key = _pair_key(row.net)
            if key:
                pairs[key[0]][key[1]] = row
        candidates = [
            DiffPairCandidate(
                positive=p["P"].net,
                negative=p["N"].net,
                length_p_mm=p["P"].total_length_mm,
                length_n_mm=p["N"].total_length_mm,
                length_delta_mm=round(abs(p["P"].total_length_mm - p["N"].total_length_mm), 4),
            )
            for p in pairs.values()
            if "P" in p and "N" in p
        ]
        return NetStats(
            board_path=_display(path),
            net_count=len(rows),
            nets=rows[:limit],
            truncated=len(rows) > limit,
            diff_pair_candidates=candidates,
        )

    return session.call(work)
