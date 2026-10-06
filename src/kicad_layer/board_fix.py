"""Board edits that clean up after routing: track widths, footprint swaps, reference placement.

All three edit a board file KiCad does not have open (the file channel of ``pcb_tools``): the tree
keeps every untouched byte, the write is atomic and snapshotted, and each change is checked against
the copper model of ``design.copper`` (net classes and ``.kicad_dru``) before it is made.

* ``set_track_width``  widen (or set) tracks of chosen nets or net classes, segment by segment,
  only where the new width keeps every clearance; the rest are reported, not forced. This is what
  an autorouter's necked-down tracks need.
* ``swap_footprint``   replace a footprint with a library one in place: position, rotation, side,
  reference, value, fields, the schematic link and the pad nets (by pad number) carried over.
* ``tidy_silkscreen``  place every reference designator on F.SilkS where it touches no pad,
  silkscreen line, other text or the board edge, nearest its own part.
"""

from __future__ import annotations

import fnmatch
import math
import re
from pathlib import Path
from typing import Any

from kicad_layer.cst import CNode, mark_dirty, to_cnode
from kicad_layer.errors import INVALID_ARGUMENT, NOT_FOUND_IN_DESIGN, LayerError
from kicad_layer.pcb_edit import BoardFile
from kicad_layer.sexpr import S, Sym, child, children, tag, value


def _num(v: float) -> Sym:
    return S("x", round(float(v), 4))[1]


# ---------------------------------------------------------------- track widths
def set_track_width(bf: BoardFile, *, nets: list[str] | None = None, netclasses: list[str] | None = None, width: float | None = None,
                    only_narrower: bool = True, pro: Path | None = None) -> dict[str, Any]:
    """Set the width of matching segments to ``width`` (or each net's class/rule minimum when None) where clearance allows."""
    from kicad_layer.design import copper

    if not nets and not netclasses:
        raise LayerError(INVALID_ARGUMENT, "Give nets (names or globs) or netclasses.")
    model = copper.load(bf.path, pro)
    rules = model.rules
    changed: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    same = 0
    for seg in children(bf.root, "segment"):
        net = value(seg, "net")
        layer = value(seg, "layer") or ""
        if not net:
            continue
        klass = str(rules.netclass(net).get("name", "Default"))
        if not ((nets and any(fnmatch.fnmatch(net, p) for p in nets)) or (netclasses and klass in netclasses)):
            continue
        w_node = child(seg, "width")
        st, en = child(seg, "start"), child(seg, "end")
        if w_node is None or st is None or en is None:
            continue
        old = float(w_node[1])
        target = float(width) if width is not None else rules.track(net)
        if (only_narrower and old >= target - 1e-6) or abs(old - target) < 1e-6:
            same += 1
            continue
        a, b = (float(st[1]), float(st[2])), (float(en[1]), float(en[2]))
        v = model.check_segment(net, layer, a, b, target)
        if v:
            d, req, it = v[0]
            blocked.append({"net": net, "layer": layer, "from": [round(a[0], 3), round(a[1], 3)], "to": [round(b[0], 3), round(b[1], 3)], "width": old,
                            "wanted": target, "blocked_by": copper._fmt_item(it), "gap_mm": round(d, 3), "needs_mm": round(req, 3)})
            continue
        w_node[1] = _num(target)
        mark_dirty(w_node)  # type: ignore[arg-type]
        model.add_segment(net, layer, a, b, target)
        changed.append({"net": net, "layer": layer, "from": [round(a[0], 3), round(a[1], 3)], "to": [round(b[0], 3), round(b[1], 3)], "old": old, "new": target})
    warnings = []
    if blocked:
        warnings.append(f"{len(blocked)} segment(s) left as they were: the new width would break a clearance (listed in extra.blocked).")
    return {"items": [{"kind": "track", **c} for c in changed], "changed": len(changed), "blocked": blocked, "unchanged": same, "warnings": warnings}


# ---------------------------------------------------------------- footprint swap
def swap_footprint(bf: BoardFile, ref: str, lib_id: str, *, keep_fields: bool = True) -> dict[str, Any]:
    """Replace footprint ``ref`` by library footprint ``lib_id`` at the same place, keeping the schematic link and pad nets."""
    from kicad_layer.kicad_libs import load_footprint
    from kicad_layer.pcb_writer import BoardBuilder

    fv = bf.find(ref)
    node = fv.node
    lib, _, name = lib_id.partition(":")
    if not lib or not name:
        raise LayerError(INVALID_ARGUMENT, "lib_id is LIBRARY:FOOTPRINT.")
    fp = load_footprint(lib, name)
    pad_nets: dict[str, str] = {}
    old_pads: dict[str, set[str]] = {}
    for p in children(node, "pad"):
        num = str(p[1])
        n = value(p, "net")
        old_pads.setdefault(num, set())
        if n:
            old_pads[num].add(n)
            pad_nets.setdefault(num, n)
    props = {str(p[1]): str(p[2]) for p in children(node, "property") if len(p) > 2}
    fields = {k: v for k, v in props.items() if k not in ("Reference", "Value", "Footprint", "Datasheet", "Description")} if keep_fields else {}
    builder = BoardBuilder(sheetfile=value(node, "sheetfile") or "")
    new = builder.footprint(fp, ref, props.get("Value", name), fv.at, fv.rotation, pad_nets=pad_nets, path=value(node, "path") or "",
                            sheetname=value(node, "sheetname") or "/", sheetfile=value(node, "sheetfile") or "", layer=fv.layer,
                            description=props.get("Description", ""), datasheet=props.get("Datasheet", ""), fields=fields)
    if not value(node, "path"):
        new[:] = [c for c in new if not (isinstance(c, list) and tag(c) in ("path", "sheetname", "sheetfile"))]
    new_pads = {p.number for p in fp.pads} if hasattr(fp, "pads") else set()
    lost = sorted(n for n, nets in old_pads.items() if nets and n not in new_pads)
    unnamed = sorted(n for n in new_pads if n and n not in old_pads)
    # keep the board's own uuid so other tools' references stay valid
    old_uuid = value(node, "uuid")
    for c in new:
        if isinstance(c, list) and tag(c) == "uuid" and old_uuid:
            c[1] = old_uuid
    idx = bf.root.index(node)
    bf.root[idx] = to_cnode(new)
    mark_dirty(bf.root)
    warnings = []
    if lost:
        warnings.append(f"pads {', '.join(lost)} carried nets but the new footprint has no pad with that number: those connections are gone.")
    if unnamed:
        warnings.append(f"new pads {', '.join(unnamed)} have no net (the old footprint had no pad with that number).")
    return {"items": [{"kind": "footprint", "ref": ref, "old": fv.lib_id, "new": lib_id, "x_mm": fv.at[0], "y_mm": fv.at[1], "rotation_deg": fv.rotation}],
            "warnings": warnings, "lost_pads": lost, "new_unnamed_pads": unnamed}


# ---------------------------------------------------------------- silkscreen references
def _rot(x: float, y: float, deg: float) -> tuple[float, float]:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return (x * c + y * s, -x * s + y * c)


def tidy_silkscreen(bf: BoardFile, *, size: float = 1.0, thickness: float = 0.15, min_size: float = 0.8, refs: list[str] | None = None,
                    margin: float = 0.15, reach: float = 4.0) -> dict[str, Any]:
    """Move each F.SilkS reference to the nearest spot clear of pads (+margin), silkscreen graphics, other references and
    the edge; horizontal first, vertical when that is all that fits; shrink to ``min_size`` only when needed."""
    from kicad_layer.design.copper import copper_layers, edge_segments, pad_item, d_seg_rect
    from kicad_layer.review import load_board

    bm = load_board(bf.path)
    copper = copper_layers(bm.copper_layers)
    pads = []
    for fp in bm.footprints:
        for p in fp.pads:
            it = pad_item(p, copper)
            if it is not None and ("F.Cu" in it.layers):
                pads.append(it)
    edges = edge_segments(bf.path)
    # silkscreen graphics of every footprint, in board coordinates, as (a, b, half width)
    silk: list[tuple[tuple[float, float], tuple[float, float], float]] = []
    for n in children(bf.root, "footprint"):
        at = child(n, "at") or []
        fx, fy = float(at[1]), float(at[2])
        frot = float(at[3]) if len(at) > 3 else 0.0
        for g in n:
            if not isinstance(g, list) or tag(g) not in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc") or value(g, "layer") != "F.SilkS":
                continue
            stroke = child(g, "stroke")
            wd = float(child(stroke, "width")[1]) if stroke is not None and child(stroke, "width") is not None else 0.12
            pts = []
            for k in ("start", "mid", "end"):
                c = child(g, k)
                if c is not None:
                    pts.append((float(c[1]), float(c[2])))
            if tag(g) == "fp_poly" and child(g, "pts") is not None:
                pts = [(float(xy[1]), float(xy[2])) for xy in children(child(g, "pts"), "xy")]
                pts.append(pts[0])
            if tag(g) == "fp_rect" and len(pts) >= 2:
                (x0, y0), (x1, y1) = pts[0], pts[-1]
                pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
            if tag(g) == "fp_circle":
                c, e = child(g, "center"), child(g, "end")
                if c is not None and e is not None:
                    cx, cy = float(c[1]), float(c[2])
                    r = math.hypot(float(e[1]) - cx, float(e[2]) - cy)
                    pts = [(cx + r * math.cos(2 * math.pi * i / 24), cy + r * math.sin(2 * math.pi * i / 24)) for i in range(25)]
            abs_pts = [(fx + _rot(x, y, frot)[0], fy + _rot(x, y, frot)[1]) for x, y in pts]
            silk += [(a, b, wd / 2) for a, b in zip(abs_pts, abs_pts[1:])]
    placed: list[tuple[float, float, float, float]] = []
    moved, failed = [], []
    order = sorted(bm.footprints, key=lambda f: (f.courtyard[2] - f.courtyard[0]) * (f.courtyard[3] - f.courtyard[1]) if f.courtyard else 0.0)

    def box_clear(cx, cy, w, h) -> float | None:
        """Smallest clearance of the text box to anything, or None when it overlaps."""
        best = 9.0
        if edges:
            for a, b in edges:
                d = d_seg_rect(a, b, cx, cy, w, h, 0.0)
                if d < 0.3:
                    return None
        for it in pads:
            x0, y0, x1, y1 = it.bbox
            if x1 < cx - w / 2 - 1 or x0 > cx + w / 2 + 1 or y1 < cy - h / 2 - 1 or y0 > cy + h / 2 + 1:
                continue
            from kicad_layer.design.copper import _rect_to_item

            d = _rect_to_item(cx, cy, w, h, 0.0, it)
            if d < margin:
                return None
            best = min(best, d)
        for a, b, hw in silk:
            if max(a[0], b[0]) < cx - w / 2 - 1 or min(a[0], b[0]) > cx + w / 2 + 1 or max(a[1], b[1]) < cy - h / 2 - 1 or min(a[1], b[1]) > cy + h / 2 + 1:
                continue
            d = d_seg_rect(a, b, cx, cy, w, h, 0.0) - hw
            if d < 0.1:
                return None
            best = min(best, d)
        for (px, py, pw, ph) in placed:
            if abs(px - cx) < (pw + w) / 2 + 0.15 and abs(py - cy) < (ph + h) / 2 + 0.15:
                return None
        return best

    for fp in order:
        if refs and fp.ref not in refs:
            continue
        if not fp.courtyard or fp.layer != "F.Cu":
            continue
        node = bf.find(fp.ref).node
        prop = next((p for p in children(node, "property") if len(p) > 2 and str(p[1]) == "Reference"), None)
        if prop is None or value(prop, "layer") != "F.SilkS" or (child(prop, "hide") is not None and str(child(prop, "hide")[1]) == "yes"):
            continue
        x0, y0, x1, y1 = fp.courtyard
        cx0, cy0 = (x0 + x1) / 2, (y0 + y1) / 2
        best = None
        for sz in (size, min_size) if min_size < size else (size,):
            tw, th = len(fp.ref) * 0.92 * sz + 0.2, sz * 1.15 + 0.2
            for ang in (0, 90):
                w, h = (tw, th) if ang == 0 else (th, tw)
                step = 0.25
                nx = int((x1 - x0 + 2 * reach + w) / step) + 1
                ny = int((y1 - y0 + 2 * reach + h) / step) + 1
                for i in range(nx):
                    px = x0 - reach - w / 2 + i * step
                    for j in range(ny):
                        py = y0 - reach - h / 2 + j * step
                        dx = max(x0 - (px + w / 2), (px - w / 2) - x1, 0.0)
                        dy = max(y0 - (py + h / 2), (py - h / 2) - y1, 0.0)
                        d = math.hypot(dx, dy)
                        inside = dx == 0.0 and dy == 0.0
                        cost = d + (0.3 if ang else 0.0) + (1.5 if sz < size else 0.0) + (1.0 if inside else 0.0) + 0.05 * min(abs(px - cx0), abs(py - cy0))
                        if d > reach or (best is not None and cost >= best[0]):
                            continue
                        if box_clear(px, py, w, h) is None:
                            continue
                        best = (cost, px, py, ang, sz, w, h)
            if best:
                break
        if best is None:
            failed.append(fp.ref)
            continue
        _, px, py, ang, sz, w, h = best
        placed.append((px, py, w, h))
        lx, ly = _rot(px - fp.x, py - fp.y, -fp.rotation)
        at = child(prop, "at")
        del at[1:]
        at.extend([_num(lx), _num(ly), _num(ang)])
        mark_dirty(at)  # type: ignore[arg-type]
        eff = child(prop, "effects")
        font = child(eff, "font") if eff is not None else None
        if font is not None:
            sznode, thn = child(font, "size"), child(font, "thickness")
            if sznode is not None:
                del sznode[1:]
                sznode.extend([_num(sz), _num(sz)])
                mark_dirty(sznode)  # type: ignore[arg-type]
            if thn is not None:
                thn[1] = _num(thickness if sz >= size else min(thickness, 0.12))
                mark_dirty(thn)  # type: ignore[arg-type]
            else:
                font.append(to_cnode(S("thickness", thickness)))
                mark_dirty(font)  # type: ignore[arg-type]
        if eff is not None:
            for j in [c for c in eff if isinstance(c, list) and tag(c) == "justify"]:
                eff.remove(j)
                mark_dirty(eff)  # type: ignore[arg-type]
        moved.append({"ref": fp.ref, "x_mm": round(px, 3), "y_mm": round(py, 3), "rotation_deg": ang, "size_mm": sz})
    warnings = [f"no clear spot within {reach} mm for: {', '.join(failed)}; left as they were"] if failed else []
    return {"items": [{"kind": "text", **m} for m in moved], "placed": len(moved), "failed": failed, "warnings": warnings}
