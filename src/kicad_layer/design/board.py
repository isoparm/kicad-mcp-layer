"""A board from data: outline, cutouts, poured planes, every footprint's place, texts, and the saved copper.

A placement check runs before the file is written: every courtyard must be inside the outline by
the edge margin, clear of the other courtyards, and clear of every keepout it is not exempt from.
Saved routes (written by the routing step) are re-applied after placement; routes of nets whose
pads moved away are dropped and reported.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from kicad_layer import routes as routes_mod
from kicad_layer.ids import IdFactory
from kicad_layer.kicad_libs import Footprint, load_footprint, rotate_about
from kicad_layer.models import Netlist
from kicad_layer.pcb_writer import BoardBuilder
from kicad_layer.review import COURTYARD_SHAPES, courtyard_points
from kicad_layer.sexpr import value

Point = tuple[float, float]
Rect = tuple[float, float, float, float]  # x0, y0, x1, y1


@dataclass(frozen=True)
class Place:
    """Where a footprint goes. With ``fit`` (a radius in mm) or ``near`` (a pad to sit by), ``at`` is only the
    starting point: the build looks for the place nearest ``at`` where the part fits (courtyards, keep-outs, the
    edge, and the copper under its pads), on a 0.5 mm grid through ``at`` and within the radius of ``at``, or of
    the pad with ``near``, and records it in ``placed.json`` beside the routes, so later builds reuse it."""

    ref: str
    at: Point
    rot: float = 0.0
    hide_ref: bool = False
    layer: str = "F.Cu"
    near: tuple[str, str] | None = None  # (reference, pad number): the part stays within the radius of this pad
    fit: float = 0.0  # search radius in mm; 0 means exactly ``at``; ``near`` alone searches 6 mm around the pad


@dataclass(frozen=True)
class Header:
    """A pin header with pin 1 at ``at``, running toward +x (``along_x``) or +y."""

    ref: str
    at: Point
    along_x: bool = True
    hide_ref: bool = False


@dataclass(frozen=True)
class Keepout:
    name: str
    rect: Rect
    exempt: tuple[str, ...] = ()  # references allowed inside
    exempt_prefix: tuple[str, ...] = ()

    def allows(self, ref: str) -> bool:
        return ref in self.exempt or any(ref.startswith(p) for p in self.exempt_prefix)


@dataclass(frozen=True)
class Text:
    text: str
    at: Point
    size: float = 1.0
    thickness: float = 0.15
    bold: bool = False
    layer: str = "F.SilkS"
    rot: float = 0.0
    justify: tuple[str, ...] = ()  # left | right | top | bottom; text on a back layer is mirrored as KiCad expects


@dataclass(frozen=True)
class Plane:
    """A poured zone: over the whole board less ``Board.plane_inset``, or over ``polygon``; ``priority`` decides which of
    two overlapping zones wins, ``clearance`` is the zone's own (None: the writer's 0.2 mm)."""

    layer: str
    net: str
    name: str
    polygon: tuple[Point, ...] | None = None
    clearance: float | None = None
    priority: int = 0


@dataclass(frozen=True)
class Board:
    title: str
    scope: str  # the identifier scope: every uuid in the file derives from it and the item
    outline: tuple[float, float, float, float]  # x0, y0, width, height
    radius: float = 3.0
    copper_layers: int = 4
    cutouts: tuple[tuple[Rect, float], ...] = ()  # rounded-rectangle slots: rect, corner radius
    planes: tuple[Plane | tuple[str, str, str], ...] = ()  # a Plane, or (layer, net, zone name) poured over the board less plane_inset
    plane_inset: float = 0.5
    placements: tuple[Place | Header, ...] = ()
    keepouts: tuple[Keepout, ...] = ()
    edge_margin: float = 0.3
    overlap_ok: tuple[frozenset[str], ...] = ()  # pairs whose courtyards may overlap (a module on two connectors)
    texts: tuple[Text, ...] = ()
    routes: Path | None = None  # saved copper, re-applied after placement

    def rect(self) -> Rect:
        x0, y0, w, h = self.outline
        return (x0, y0, x0 + w, y0 + h)


def courtyard_bbox(fp: Footprint, at: Point, rot: float) -> Rect | None:
    """Absolute bounding box of the footprint's front courtyard (lines, rectangles, arcs, polygons, circles), or of its
    pads when it has none."""
    xs: list[float] = []
    ys: list[float] = []
    for g in fp.tree:
        if not isinstance(g, list) or str(g[0]) not in COURTYARD_SHAPES:
            continue
        if value(g, "layer") != "F.CrtYd":
            continue
        for x, y in courtyard_points(g):
            xs.append(x)
            ys.append(y)
    if not xs:
        for p in fp.pads:
            xs += [p.x - p.size[0] / 2, p.x + p.size[0] / 2]
            ys += [p.y - p.size[1] / 2, p.y + p.size[1] / 2]
    if not xs:
        return None
    corners = [rotate_about(x, y, rot, 0.0, 0.0) for x in (min(xs), max(xs)) for y in (min(ys), max(ys))]
    return (at[0] + min(c[0] for c in corners), at[1] + min(c[1] for c in corners), at[0] + max(c[0] for c in corners), at[1] + max(c[1] for c in corners))


def _overlap(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def check_placement(board: Board, boxes: dict[str, Rect]) -> list[str]:
    """Every placement inside the outline, courtyards apart unless ``overlap_ok``, keep-outs respected; returns the problems."""
    x0, y0, x1, y1 = board.rect()
    m = board.edge_margin
    problems: list[str] = []
    refs = sorted(boxes)
    for r in refs:
        b = boxes[r]
        if b[0] < x0 + m or b[1] < y0 + m or b[2] > x1 - m or b[3] > y1 - m:
            problems.append(f"{r} courtyard {tuple(round(v, 2) for v in b)} leaves the board")
        for k in board.keepouts:
            if not k.allows(r) and _overlap(b, k.rect):
                problems.append(f"{r} courtyard {tuple(round(v, 2) for v in b)} overlaps the {k.name} {tuple(round(v, 1) for v in k.rect)}")
    for i, a in enumerate(refs):
        for bref in refs[i + 1:]:
            if frozenset((a, bref)) in board.overlap_ok:
                continue
            if _overlap(boxes[a], boxes[bref]):
                problems.append(f"{a} and {bref} courtyards overlap: {tuple(round(v, 2) for v in boxes[a])} vs {tuple(round(v, 2) for v in boxes[bref])}")
    return problems


def build_board(board: Board, out_path: Path, sheetfile: str, netlist: Netlist, symbol_paths: dict[str, tuple[str, str, str]], setup_template: Path | None,
                *, date: str, rev: str, company: str, with_routes: bool = True) -> dict:
    """``symbol_paths`` maps a reference to (instance path, sheet name, sheet file), as the build has them.

    Placements with ``fit`` or ``near`` are resolved first on a provisional build of the same board
    (``fit_placements``), then the board is written with every part at its place.
    """
    wanted = [p for p in board.placements if isinstance(p, Place) and (p.fit or p.near)]
    positions: dict[str, tuple[Point, float]] = {}
    notes: list[str] = []
    if wanted:
        positions, notes = fit_placements(board, wanted, out_path, sheetfile, netlist, symbol_paths, setup_template, date=date, rev=rev, company=company,
                                          with_routes=with_routes)
    stats = _build(board, out_path, sheetfile, netlist, symbol_paths, setup_template, date=date, rev=rev, company=company, with_routes=with_routes,
                   positions=positions)
    if notes:
        stats["fitted"] = notes
    return stats


def fit_placements(board: Board, wanted: list[Place], out_path: Path, sheetfile: str, netlist: Netlist, symbol_paths: dict[str, tuple[str, str, str]],
                   setup_template: Path | None, *, date: str, rev: str, company: str, with_routes: bool) -> tuple[dict[str, tuple[Point, float]], list[str]]:
    """The nearest free place for every ``fit``/``near`` placement, from ``placed.json`` when it already holds one.

    A provisional board with the wanted parts at their starting points gives the copper model; each part
    is then tried on a 0.5 mm grid through its ``at``, nearest ``at`` first, within the radius of ``at``
    (or of the ``near`` pad), against the design's placement rules and the copper questions, and the
    first place that fits is recorded. A recorded place is reused while ``rot``, ``near`` and ``at`` stay.
    """
    record_path = board.routes.with_name("placed.json") if board.routes is not None else None
    recorded: dict[str, dict] = json.loads(record_path.read_text(encoding="utf-8")) if record_path is not None and record_path.is_file() else {}
    positions: dict[str, tuple[Point, float]] = {}
    notes: list[str] = []
    pending: list[Place] = []
    for p in wanted:
        r = recorded.get(p.ref)
        if r and r.get("rot") == p.rot and r.get("near") == (list(p.near) if p.near else None) and r.get("seed", list(p.at)) == list(p.at):
            positions[p.ref] = ((float(r["at"][0]), float(r["at"][1])), p.rot)
            notes.append(f"{p.ref} at ({r['at'][0]}, {r['at'][1]}) from placed.json")
        else:
            pending.append(p)
    if not pending:
        return positions, notes
    from kicad_layer.review import load_board

    from . import copper

    tmp = out_path.with_name(out_path.stem + "-fit" + out_path.suffix)
    _build(board, tmp, sheetfile, netlist, symbol_paths, setup_template, date=date, rev=rev, company=company, with_routes=with_routes,
           positions=positions, lenient=frozenset(p.ref for p in pending))
    bm = load_board(tmp)
    model = copper.Model(bm, copper.Rules.load(out_path.with_suffix(".kicad_pro")), tmp)
    pending_refs = {p.ref for p in pending}
    boxes: dict[str, Rect] = {f.ref: f.courtyard for f in bm.footprints if f.courtyard and f.ref not in pending_refs}
    comps = {c.ref: c for c in netlist.components}
    for p in pending:
        centre, anchor = p.at, f"({p.at[0]}, {p.at[1]})"
        if p.near:
            fp_near = next((f for f in bm.footprints if f.ref == p.near[0]), None)
            pad = next((q for q in fp_near.pads if q.number == p.near[1]), None) if fp_near else None
            if pad is None:
                raise AssertionError(f"{p.ref}: near={p.near} names a pad that is not on the board")
            centre, anchor = (pad.x, pad.y), f"{p.near[0]}.{p.near[1]}"
        radius = p.fit or 6.0
        lib, name = comps[p.ref].footprint.split(":", 1)
        fp = load_footprint(lib, name)
        spot = _fit_search(board, model, boxes, p, fp, centre, radius, seed=p.at)
        if spot is None:
            tmp.unlink(missing_ok=True)
            raise AssertionError(f"{p.ref}: no place fits within {radius} mm of {anchor} (rot {p.rot:g}); widen fit or move the start")
        positions[p.ref] = (spot, p.rot)
        bb = courtyard_bbox(fp, spot, (p.rot + 180) % 360 if p.layer == "B.Cu" else p.rot)
        if bb is not None:
            boxes[p.ref] = bb
        recorded[p.ref] = {"at": [spot[0], spot[1]], "rot": p.rot, "near": list(p.near) if p.near else None, "fit": radius, "centre": [round(centre[0], 3), round(centre[1], 3)],
                           "seed": list(p.at)}
        notes.append(f"{p.ref} fitted at ({spot[0]}, {spot[1]}), {math.dist(spot, p.at):.1f} mm from its start" + (f", {math.dist(spot, centre):.1f} mm from {anchor}" if p.near else ""))
    tmp.unlink(missing_ok=True)
    if record_path is not None:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(recorded, indent=1), encoding="utf-8", newline="\n")
    return positions, notes


def _fit_search(board: Board, model, boxes: dict[str, Rect], p: Place, fp: Footprint, centre: Point, radius: float, step: float = 0.5,
                *, seed: Point | None = None) -> Point | None:
    """The grid point nearest ``seed`` (default ``centre``), within ``radius`` of ``centre``, where ``p`` keeps the placement
    rules and the copper clearances; the grid runs through ``seed``."""
    from . import copper

    seed = centre if seed is None else seed
    x0, y0, x1, y1 = board.rect()
    m = board.edge_margin
    n = math.ceil((radius + math.dist(seed, centre)) / step)
    cands = [(round(seed[0] + i * step, 3), round(seed[1] + j * step, 3)) for i in range(-n, n + 1) for j in range(-n, n + 1)]
    cands = [c for c in cands if math.dist(c, centre) <= radius + 1e-9]
    cands.sort(key=lambda c: (math.dist(c, seed), math.dist(c, centre)))
    rot_eff = (p.rot + 180) % 360 if p.layer == "B.Cu" else p.rot
    for c in cands:
        bb = courtyard_bbox(fp, c, rot_eff)
        if bb is not None:
            if bb[0] < x0 + m or bb[1] < y0 + m or bb[2] > x1 - m or bb[3] > y1 - m:
                continue
            if any(not k.allows(p.ref) and _overlap(bb, k.rect) for k in board.keepouts):
                continue
            if any(_overlap(bb, other) for ref, other in boxes.items() if frozenset((ref, p.ref)) not in board.overlap_ok):
                continue
        if copper.free(model, p.ref, c[0], c[1], p.rot, quiet=True)[0].endswith("fits"):
            return c
    return None


def _build(board: Board, out_path: Path, sheetfile: str, netlist: Netlist, symbol_paths: dict[str, tuple[str, str, str]], setup_template: Path | None,
           *, date: str, rev: str, company: str, with_routes: bool = True, positions: dict[str, tuple[Point, float]] | None = None,
           lenient: frozenset[str] = frozenset()) -> dict:
    """Write the board with every placement at its place (``positions`` override ``Place.at``/``rot``); ``lenient`` parts skip the placement check."""
    positions = positions or {}
    pcb = BoardBuilder(sheetfile=sheetfile, ids=IdFactory(scope=board.scope), title=board.title, date=date, rev=rev, company=company, setup_template=setup_template,
                       copper_layers=board.copper_layers)
    pad_net: dict[tuple[str, str], str] = {}
    for net in netlist.nets:
        for node in net.nodes:
            pad_net[(node.ref, node.pin)] = net.name
    comps = {c.ref: c for c in netlist.components}
    boxes: dict[str, Rect] = {}
    placed: set[str] = set()
    placements: dict[str, tuple[Point, float]] = {}

    def nets_for(ref: str) -> dict[str, str]:
        return {pin: name for (r, pin), name in pad_net.items() if r == ref}

    def place(ref: str, at: Point, rot: float = 0.0, *, hide_ref: bool = False, layer: str = "F.Cu") -> Footprint:
        comp = comps[ref]
        placements[ref] = ((float(at[0]), float(at[1])), float(rot))
        lib, name = comp.footprint.split(":", 1)
        fp = load_footprint(lib, name)
        path, sheetname, sheetfile_ = symbol_paths[ref]
        pcb.footprint(fp, ref, comp.value or "", at, rot, path=path, sheetname=sheetname, sheetfile=sheetfile_, pad_nets=nets_for(ref), hide_ref=hide_ref,
                      description=comp.fields.get("Description", ""), datasheet=comp.fields.get("Datasheet", ""), fields=comp.fields, layer=layer,
                      dnp="dnp" in comp.properties)  # KiCad's netlist lists a DNP symbol's flag as a property
        # a back-side part is mirrored about its X axis and then rotated; for the bounding box that equals the
        # front-side courtyard turned by a further 180 degrees (exact for left-right symmetric parts)
        bb = courtyard_bbox(fp, at, (rot + 180) % 360 if layer == "B.Cu" else rot)
        if bb is not None:
            boxes[ref] = bb
        placed.add(ref)
        return fp

    def header(ref: str, at: Point, along_x: bool, *, hide_ref: bool = False) -> None:
        comp = comps[ref]
        lib, name = comp.footprint.split(":", 1)
        fp = load_footprint(lib, name)
        last = max((p.number for p in fp.pads if p.number.isdigit()), key=int)
        rot = next(r for r in (90, 270) if fp.pad_position(last, at[0], at[1], r)[0] > at[0]) if along_x else 0
        place(ref, at, rot, hide_ref=hide_ref)

    x0, y0, x1, y1 = board.rect()
    pcb.rounded_rect_outline(x0, y0, x1, y1, board.radius)
    for rect, r in board.cutouts:
        pcb.rounded_rect_outline(*rect, r)
    inset = board.plane_inset
    pour = [(x0 + inset, y0 + inset), (x1 - inset, y0 + inset), (x1 - inset, y1 - inset), (x0 + inset, y1 - inset)]
    for plane in board.planes:
        pl = plane if isinstance(plane, Plane) else Plane(*plane)
        extra = {} if pl.clearance is None else {"clearance": pl.clearance}
        pcb.zone(net=pl.net, layer=pl.layer, polygon=list(pl.polygon) if pl.polygon else pour, name=pl.name, priority=pl.priority, **extra)
    for p in board.placements:
        if isinstance(p, Header):
            header(p.ref, p.at, p.along_x, hide_ref=p.hide_ref)
        else:
            at, rot = positions.get(p.ref, (p.at, p.rot))
            place(p.ref, at, rot, hide_ref=p.hide_ref, layer=p.layer)
    for t in board.texts:
        justify = tuple(t.justify) + (("mirror",) if t.layer.startswith("B.") and "mirror" not in t.justify else ())
        pcb.text(t.text, t.at, layer=t.layer, size=t.size, thickness=t.thickness, rot=t.rot, justify=justify or None, bold=t.bold)

    missing = sorted(set(comps) - placed)
    if missing:
        raise AssertionError(f"components without a place on the board: {missing}")
    problems = check_placement(board, {r: b for r, b in boxes.items() if r not in lenient})
    if problems:
        raise AssertionError("placement problems:\n  " + "\n  ".join(problems))
    n_seg = n_via = 0
    stale: list[str] = []
    if with_routes and board.routes is not None and board.routes.is_file():
        routes = routes_mod.load(board.routes)
        pad_points: dict[str, list[Point]] = {}
        for ref, (at, rot) in placements.items():
            lib, name = comps[ref].footprint.split(":", 1)
            fp = load_footprint(lib, name)
            for pad in fp.pads:
                net = pad_net.get((ref, pad.number))
                if net:
                    pad_points.setdefault(net, []).append(fp.pad_position(pad.number, at[0], at[1], rot))
        stale = routes_mod.stale(routes, pad_points)
        if stale:
            routes = routes_mod.Routes(segments=[x for x in routes.segments if x.net not in stale], vias=[x for x in routes.vias if x.net not in stale], nets=routes.nets - set(stale))
        n_seg, n_via = routes_mod.apply(pcb, routes)
    pcb.write(str(out_path))
    return {"footprints": len(pcb.footprints), "zones": len(board.planes), "size_mm": f"{board.outline[2]:.0f} x {board.outline[3]:.0f}",
            "routed_segments": n_seg, "routed_vias": n_via, "stale_route_nets_dropped": stale}
