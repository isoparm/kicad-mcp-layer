"""A footprint from numbers: pads, a body outline, the courtyard around both, written into a project library.

For parts KiCad's libraries do not carry (a bridge rectifier in a vendor package, a power resistor
of a given body and pitch) the datasheet gives a land pattern as a table of pads. ``create_footprint``
turns that table into a ``.kicad_mod`` with fabrication and silkscreen outlines, a courtyard with
the IPC-7351 margin, a pin-1 mark, and the right attribute (smd or through_hole), then registers the
library in the project's ``fp-lib-table`` when it is not there yet.

The pads are what the datasheet says; nothing here invents a land pattern.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from kicad_layer.errors import EDIT_CONFLICT, INVALID_ARGUMENT, LayerError
from kicad_layer.models import FootprintCreated
from kicad_layer.paths import display
from kicad_layer.sexpr import S, Sym, dumps

PAD_KINDS = ("smd", "thru_hole", "np_thru_hole")
PAD_SHAPES = ("rect", "roundrect", "circle", "oval")


def _u() -> str:
    return str(uuid.uuid4())


def _line(a, b, layer: str, width: float):
    return S("fp_line", S("start", round(a[0], 4), round(a[1], 4)), S("end", round(b[0], 4), round(b[1], 4)),
             S("stroke", S("width", width), S("type", Sym("solid"))), S("layer", layer), S("uuid", _u()))


def _rect(x0, y0, x1, y1, layer: str, width: float):
    return S("fp_rect", S("start", round(x0, 4), round(y0, 4)), S("end", round(x1, 4), round(y1, 4)),
             S("stroke", S("width", width), S("type", Sym("solid"))), S("fill", Sym("no")), S("layer", layer), S("uuid", _u()))


def footprint_tree(name: str, pads: list[dict[str, Any]], *, body: list[float] | None = None, courtyard_margin: float = 0.25,
                   description: str = "", tags: str = "", silk_gap: float = 0.2) -> tuple[list, list[float]]:
    """The s-expression of the footprint and its courtyard box [x0, y0, x1, y1]."""
    if not pads:
        raise LayerError(INVALID_ARGUMENT, "A footprint needs at least one pad.")
    pad_nodes = []
    xs: list[float] = []
    ys: list[float] = []
    kinds = set()
    for i, p in enumerate(pads):
        kind = p.get("kind", p.get("type", "smd"))
        shape = p.get("shape", "rect" if kind == "smd" else "circle")
        if kind not in PAD_KINDS:
            raise LayerError(INVALID_ARGUMENT, f"pad {i}: kind is one of {PAD_KINDS}")
        if shape not in PAD_SHAPES:
            raise LayerError(INVALID_ARGUMENT, f"pad {i}: shape is one of {PAD_SHAPES}")
        try:
            x, y = float(p["x"]), float(p["y"])
            w = float(p.get("w", p.get("size", 0)))
            h = float(p.get("h", w))
        except (KeyError, TypeError, ValueError) as exc:
            raise LayerError(INVALID_ARGUMENT, f"pad {i}: give x, y and w (h defaults to w): {exc}") from exc
        if w <= 0 or h <= 0:
            raise LayerError(INVALID_ARGUMENT, f"pad {i}: size must be positive")
        drill = p.get("drill")
        if kind != "smd" and not drill:
            raise LayerError(INVALID_ARGUMENT, f"pad {i}: a {kind} pad needs drill")
        if drill and float(drill) >= min(w, h) and kind == "thru_hole":
            raise LayerError(INVALID_ARGUMENT, f"pad {i}: drill {drill} leaves no annular ring in a {w} x {h} pad")
        rot = float(p.get("rotation", 0.0))
        number = "" if kind == "np_thru_hole" else str(p.get("number", i + 1))
        kinds.add(kind)
        layers = ("F.Cu", "F.Paste", "F.Mask") if kind == "smd" else ("*.Cu", "*.Mask")
        node = S("pad", number, Sym(kind), Sym(shape), S("at", x, y, rot) if rot else S("at", x, y), S("size", w, h))
        if drill:
            node.append(S("drill", float(drill)))
        node.append(S("layers", *layers))
        if shape == "roundrect":
            node.append(S("roundrect_rratio", float(p.get("rratio", 0.25))))
        if kind == "thru_hole":
            node.append(S("remove_unused_layers", Sym("no")))
        node.append(S("uuid", _u()))
        pad_nodes.append(node)
        hw, hh = (h / 2, w / 2) if round(rot) % 180 == 90 else (w / 2, h / 2)
        xs += [x - hw, x + hw]
        ys += [y - hh, y + hh]
    bx0, by0, bx1, by1 = min(xs), min(ys), max(xs), max(ys)
    if body:
        if len(body) != 2:
            raise LayerError(INVALID_ARGUMENT, "body is [width, height] in mm, centred on the origin.")
        bw, bh = float(body[0]), float(body[1])
        fx0, fy0, fx1, fy1 = -bw / 2, -bh / 2, bw / 2, bh / 2
    else:
        fx0, fy0, fx1, fy1 = bx0, by0, bx1, by1
    cx0, cy0 = min(fx0, bx0) - courtyard_margin, min(fy0, by0) - courtyard_margin
    cx1, cy1 = max(fx1, bx1) + courtyard_margin, max(fy1, by1) + courtyard_margin
    r2 = lambda v: round(round(v / 0.01) * 0.01, 2)  # noqa: E731 - KiCad's 0.01 mm courtyard grid
    cx0, cy0, cx1, cy1 = r2(cx0), r2(cy0), r2(cx1), r2(cy1)
    attr = S("attr", Sym("smd")) if kinds == {"smd"} else S("attr", Sym("through_hole"))
    font = S("effects", S("font", S("size", 1, 1), S("thickness", 0.15)))
    small = S("effects", S("font", S("size", 1.27, 1.27), S("thickness", 0.15)))
    tree = S("footprint", name, S("version", 20241229), S("generator", "kicad_layer"), S("generator_version", "10.0"), S("layer", "F.Cu"))
    if description:
        tree.append(S("descr", description))
    if tags:
        tree.append(S("tags", tags))
    tree += [
        S("property", "Reference", "REF**", S("at", 0, cy0 - 0.8, 0), S("layer", "F.SilkS"), S("uuid", _u()), font),
        S("property", "Value", name, S("at", 0, cy1 + 0.8, 0), S("layer", "F.Fab"), S("uuid", _u()), font),
        S("property", "Datasheet", "", S("at", 0, 0, 0), S("layer", "F.Fab"), S("hide", Sym("yes")), S("uuid", _u()), small),
        S("property", "Description", description, S("at", 0, 0, 0), S("layer", "F.Fab"), S("hide", Sym("yes")), S("uuid", _u()), small),
        attr,
        _rect(fx0, fy0, fx1, fy1, "F.Fab", 0.1),
        _rect(cx0, cy0, cx1, cy1, "F.CrtYd", 0.05),
    ]
    # silkscreen: the body outline pushed out past the pads' clearance, broken where it would touch a pad
    sx0, sy0, sx1, sy1 = fx0 - 0.11, fy0 - 0.11, fx1 + 0.11, fy1 + 0.11
    pad_boxes = [(xs[2 * i] - silk_gap, ys[2 * i] - silk_gap, xs[2 * i + 1] + silk_gap, ys[2 * i + 1] + silk_gap) for i in range(len(pad_nodes))]
    for a, b in (((sx0, sy0), (sx1, sy0)), ((sx1, sy0), (sx1, sy1)), ((sx1, sy1), (sx0, sy1)), ((sx0, sy1), (sx0, sy0))):
        tree += [_line(p, q, "F.SilkS", 0.12) for p, q in _clip_line(a, b, pad_boxes)]
    # pin 1 mark beside the first numbered pad
    first = next((i for i, p in enumerate(pads) if str(p.get("number", i + 1)) in ("1", "A1")), 0)
    px = (xs[2 * first] + xs[2 * first + 1]) / 2
    py = (ys[2 * first] + ys[2 * first + 1]) / 2
    mx = min(sx0, xs[2 * first]) - 0.4
    tree.append(S("fp_circle", S("center", round(mx, 3), round(py, 3)), S("end", round(mx + 0.15, 3), round(py, 3)),
                  S("stroke", S("width", 0.3), S("type", Sym("solid"))), S("fill", Sym("yes")), S("layer", "F.SilkS"), S("uuid", _u())))
    tree += pad_nodes
    tree.append(S("embedded_fonts", Sym("no")))
    return tree, [cx0, cy0, cx1, cy1]


def _clip_line(a, b, boxes):
    """The parts of the axis-aligned segment a-b outside every box."""
    if a[1] == b[1]:  # horizontal
        y = a[1]
        lo, hi = sorted((a[0], b[0]))
        cuts = sorted((max(lo, x0), min(hi, x1)) for x0, y0, x1, y1 in boxes if y0 <= y <= y1 and x1 > lo and x0 < hi)
        out, cur = [], lo
        for c0, c1 in cuts:
            if c0 > cur + 0.2:
                out.append(((cur, y), (c0, y)))
            cur = max(cur, c1)
        if hi > cur + 0.2:
            out.append(((cur, y), (hi, y)))
        return out
    x = a[0]
    lo, hi = sorted((a[1], b[1]))
    cuts = sorted((max(lo, y0), min(hi, y1)) for x0, y0, x1, y1 in boxes if x0 <= x <= x1 and y1 > lo and y0 < hi)
    out, cur = [], lo
    for c0, c1 in cuts:
        if c0 > cur + 0.2:
            out.append(((x, cur), (x, c0)))
        cur = max(cur, c1)
    if hi > cur + 0.2:
        out.append(((x, cur), (x, hi)))
    return out


def _register(project_dir: Path, library: str, pretty: Path) -> Path | None:
    """Add ``library`` to the project's fp-lib-table when it is missing; the table path when it was written."""
    table = project_dir / "fp-lib-table"
    uri = "${KIPRJMOD}/" + pretty.relative_to(project_dir).as_posix()
    entry = f'  (lib (name "{library}")(type "KiCad")(uri "{uri}")(options "")(descr ""))\n'
    if table.is_file():
        text = table.read_text(encoding="utf-8")
        if re.search(r'\(name\s+"?' + re.escape(library) + r'"?\)', text):
            return None
        end = text.rstrip().rfind(")")
        text = text[:end].rstrip() + "\n" + entry + ")\n"
    else:
        text = "(fp_lib_table\n  (version 7)\n" + entry + ")\n"
    table.write_text(text, encoding="utf-8", newline="\n")
    return table


def create_footprint(project_dir: Path, library: str, name: str, pads: list[dict[str, Any]], *, body: list[float] | None = None,
                     courtyard_margin: float = 0.25, description: str = "", tags: str = "", overwrite: bool = False, dry_run: bool = False) -> FootprintCreated:
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", library) or not re.fullmatch(r"[A-Za-z0-9_.+,-]+", name):
        raise LayerError(INVALID_ARGUMENT, "library and name use letters, digits and _ . + - only.")
    tree, court = footprint_tree(name, pads, body=body, courtyard_margin=courtyard_margin, description=description, tags=tags)
    pretty = project_dir / f"{library}.pretty"
    path = pretty / f"{name}.kicad_mod"
    warnings: list[str] = []
    if path.exists() and not overwrite:
        raise LayerError(EDIT_CONFLICT, f"{display(path)} exists.", hint="Pass overwrite=True to replace it.")
    nums = [str(p.get("number", i + 1)) for i, p in enumerate(pads) if p.get("kind", p.get("type", "smd")) != "np_thru_hole"]
    dup = sorted({n for n in nums if nums.count(n) > 1})
    if dup:
        warnings.append(f"pad numbers used more than once: {', '.join(dup)} (fine for a shield or thermal tab, otherwise a typo)")
    table = None
    if not dry_run:
        pretty.mkdir(parents=True, exist_ok=True)
        path.write_text(dumps(tree) + "\n", encoding="utf-8", newline="\n")
        table = _register(project_dir, library, pretty)
    return FootprintCreated(lib_id=f"{library}:{name}", path=display(path), pads=len(pads), courtyard_mm=court,
                            table=display(table) if table else None, warnings=warnings + (["dry run: nothing written"] if dry_run else []))
