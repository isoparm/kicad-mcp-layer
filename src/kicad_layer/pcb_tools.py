"""Board editing tools: choose the channel, apply the rules, report what happened.

Channel choice per operation:

* ``ipc`` when the board is open in KiCad's PCB Editor: edits land as undo steps.
* ``file`` when it is not: the file is edited losslessly and snapshotted.
* ``auto`` (default) picks ipc if the board is open, otherwise file. A board this process
  has ever seen live is never edited through the file channel, because KiCad may hold
  unsaved changes to it, with one exception: KiCad is gone (the API is unreachable and no
  KiCad process runs, i.e. it crashed) and no living process holds the board's lock file.
  Then ``auto`` falls back to the file, also when a live edit fails that way mid-call.
* A lock file whose owner is gone (a crash) blocks a file edit only until ``force`` is
  given; a lock held by a running KiCad always blocks it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dataclasses import dataclass, field

from kicad_layer import locks
from kicad_layer.errors import BOARD_NOT_OPEN, EDIT_CONFLICT, INVALID_ARGUMENT, KICAD_NOT_RUNNING, PROJECT_NOT_FOUND, LayerError, require_write_mode
from kicad_layer.ipc import board_write
from kicad_layer.ipc.session import Rejected, Unreachable, get_session
from kicad_layer.models import BoardEditResult
from kicad_layer.paths import BOARD, display, resolve_in_workspace
from kicad_layer.pcb_edit import BoardFile
from kicad_layer.project import board_rule_warnings

Point = tuple[float, float]


def _board_path(board_path: str | None) -> Path | None:
    return resolve_in_workspace(board_path, suffixes=(BOARD,)) if board_path else None


@dataclass
class ChannelChoice:
    channel: str
    path: Path | None
    force_save: bool = False  # the lock on disk is a dead process's: write past it
    warnings: list[str] = field(default_factory=list)


def _kicad_gone(live_error: LayerError | None) -> bool:
    return isinstance(live_error, Unreachable) and live_error.code == KICAD_NOT_RUNNING and not locks.kicad_pids()


def choose_channel(board_path: Path | None, requested: str, *, force: bool = False) -> tuple[str, Path | None]:
    """Return (channel, board path). Raises when the request cannot be honoured safely."""
    c = pick_channel(board_path, requested, force=force)
    return c.channel, c.path


def pick_channel(board_path: Path | None, requested: str, *, force: bool = False) -> ChannelChoice:
    """The channel for one edit, with what the file channel must know (a stale lock) and say."""
    session = get_session()
    open_paths: list[Path] = []
    live_error: LayerError | None = None
    try:
        open_paths = [p for _, p in session.open_boards()]
    except (Unreachable, Rejected) as exc:
        live_error = exc
    is_open = board_path is not None and any(os.path.normcase(os.path.realpath(p)) == os.path.normcase(os.path.realpath(board_path)) for p in open_paths)

    if requested == "ipc" or (requested == "auto" and (is_open or board_path is None)):
        if board_path is None and not open_paths and live_error is None:
            raise LayerError(BOARD_NOT_OPEN, "No board is open in KiCad and no board_path was given.", hint="Open the PCB Editor, or pass board_path for a file edit.")
        if requested == "ipc" and live_error is not None:
            raise live_error
        if requested == "auto" and board_path is None and live_error is not None:
            raise live_error
        return ChannelChoice("ipc", board_path)
    if board_path is None:
        raise LayerError(INVALID_ARGUMENT, "board_path is required for a file edit.")
    return _file_choice(board_path, requested, live_error, force=force)


def _file_choice(board_path: Path, requested: str, live_error: LayerError | None, *, force: bool) -> ChannelChoice:
    choice = ChannelChoice("file", board_path)
    gone = _kicad_gone(live_error)
    if get_session().has_seen_live(board_path):
        if not (gone and (requested == "auto" or force)):
            raise LayerError(
                EDIT_CONFLICT,
                f"{board_path.name} was open in KiCad during this session, so it is not edited on disk.",
                hint="Open it in the PCB Editor and edit it live, or restart the server after KiCad has saved and closed it.",
            )
        choice.warnings.append(f"KiCad is gone (it probably crashed) and had {board_path.name} open: edits it had not saved are lost; this edit went to the file.")
    lock = locks.lock_path(board_path)
    if lock.exists():
        if locks.owner_alive(lock):
            raise LayerError(
                EDIT_CONFLICT,
                f"KiCad has {board_path.name} open (lock file present) but the API does not report it.",
                hint="Enable the KiCad API and use the live channel, or close the PCB Editor for a file edit. force does not override a lock a running KiCad holds.",
            )
        if not (force or (requested == "auto" and isinstance(live_error, Unreachable))):
            raise LayerError(
                EDIT_CONFLICT,
                f"{lock.name} is left over from a KiCad that is no longer running.",
                hint="Pass force=True to write the board anyway (nothing holds it), or open and close it in KiCad.",
            )
        choice.force_save = True
        choice.warnings.append(f"{lock.name} was stale (its KiCad is not running); written past it.")
    return choice


def _fall_back(exc: Unreachable, board_path: Path | None, requested: str, *, force: bool) -> ChannelChoice:
    """After a live edit failed on the transport: the file channel when that is safe, else the error."""
    if requested != "auto" or board_path is None:
        raise exc
    try:
        choice = _file_choice(board_path, requested, exc if exc.code == KICAD_NOT_RUNNING else None, force=force)
    except LayerError:
        raise exc from None
    if not _kicad_gone(exc):
        raise exc
    choice.warnings.insert(0, f"The live edit failed ({exc.code}); KiCad is no longer running, so the file was edited instead.")
    return choice


def _result(channel: str, board: Path | None, summary: str, data: dict[str, Any], *, dry_run: bool = False, saved=None) -> BoardEditResult:
    items = data.get("items", [])
    warnings = list(data.get("warnings", []))
    if channel == "ipc":
        warnings.append("Live edit: KiCad holds the change unsaved until pcb_save; run_drc reads the file on disk.")
    return BoardEditResult(
        changed=not dry_run, dry_run=dry_run, channel=channel, board=display(board) if board else str(data.get("board", "")), summary=summary,
        items=items, item_ids=[i.get("id") for i in items if i.get("id")], deleted=data.get("deleted", []),
        snapshot=str(saved.snapshot) if saved and saved.snapshot else None, sha256_before=saved.sha256_before if saved else None,
        sha256_after=saved.sha256_after if saved else None, warnings=warnings, extra={k: v for k, v in data.items() if k not in ("items", "warnings", "deleted", "board")},
    )


def _file_edit(board_path: Path, summary: str, fn, *, dry_run: bool, force: bool, notes: list[str] | None = None) -> BoardEditResult:
    bf = BoardFile(board_path)
    data = fn(bf) or {}
    if notes:
        data["warnings"] = list(notes) + list(data.get("warnings", []))
    if dry_run:
        changed = bf.render() != bf.source
        return _result("file", board_path, summary + (" (dry run, nothing written)" if changed else " (no change)"), data, dry_run=True)
    saved = bf.save(force=force)
    warnings = list(data.get("warnings", []))
    if any(k in summary for k in ("zone", "track", "via", "place", "move")):
        warnings.append("Zones on disk are unfilled until KiCad refills them: run_drc with refill or open the board.")
    data["warnings"] = warnings
    return _result("file", board_path, summary, data, saved=saved)


# --------------------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------------------


def place_footprint(board_path: str | None, lib_id: str, ref: str, x_mm: float, y_mm: float, *, rotation: float = 0.0, value: str = "",
                    layer: str = "F.Cu", channel: str = "auto", dry_run: bool = False, force: bool = False) -> BoardEditResult:
    require_write_mode("pcb_place_footprint")
    path = _board_path(board_path)
    pick = pick_channel(path, channel, force=force)
    ch, path = pick.channel, pick.path
    summary = f"place {lib_id} as {ref} at ({x_mm}, {y_mm}) rot {rotation} on {layer}"
    if ch == "ipc":
        if dry_run:
            return _result("ipc", path, summary + " (dry run)", {}, dry_run=True)
        try:
            return _result("ipc", path, summary, board_write.place_footprint(path, lib_id, ref, (x_mm, y_mm), rotation, value_text=value, layer=layer))
        except Unreachable as exc:
            pick = _fall_back(exc, path, channel, force=force)

    def edit(bf: BoardFile):
        fp = bf.place_footprint(lib_id, ref, (x_mm, y_mm), rotation, value_text=value, layer=layer)
        return {"items": [{"kind": "footprint", "id": fp.uuid, "ref": fp.reference, "x_mm": fp.at[0], "y_mm": fp.at[1], "rotation_deg": fp.rotation, "layer": fp.layer}]}

    return _file_edit(path, summary, edit, dry_run=dry_run, force=force or pick.force_save, notes=pick.warnings)


def move_footprint(board_path: str | None, ref: str, *, x_mm: float | None = None, y_mm: float | None = None, rotation: float | None = None,
                   layer: str | None = None, channel: str = "auto", dry_run: bool = False, force: bool = False) -> BoardEditResult:
    require_write_mode("pcb_move_footprint")
    if (x_mm is None) != (y_mm is None):
        raise LayerError(INVALID_ARGUMENT, "Give both x_mm and y_mm, or neither.")
    path = _board_path(board_path)
    pick = pick_channel(path, channel, force=force)
    ch, path = pick.channel, pick.path
    at = (x_mm, y_mm) if x_mm is not None else None
    summary = f"move {ref}" + (f" to ({x_mm}, {y_mm})" if at else "") + (f" rot {rotation}" if rotation is not None else "") + (f" to {layer}" if layer else "")
    if ch == "ipc":
        if dry_run:
            return _result("ipc", path, summary + " (dry run)", {}, dry_run=True)
        try:
            return _result("ipc", path, summary, board_write.move_footprint(path, ref, at, rotation, layer))
        except Unreachable as exc:
            pick = _fall_back(exc, path, channel, force=force)
    if layer is not None:
        raise LayerError(INVALID_ARGUMENT, "Changing the layer needs the live channel (KiCad flips the footprint).")

    def edit(bf: BoardFile):
        fp = bf.move_footprint(ref, at, rotation)
        return {"items": [{"kind": "footprint", "id": fp.uuid, "ref": fp.reference, "x_mm": fp.at[0], "y_mm": fp.at[1], "rotation_deg": fp.rotation, "layer": fp.layer}]}

    return _file_edit(path, summary, edit, dry_run=dry_run, force=force or pick.force_save, notes=pick.warnings)


def move_footprints(board_path: str | None, moves: list[dict[str, Any]], *, channel: str = "auto", dry_run: bool = False, force: bool = False) -> BoardEditResult:
    """Every move in one write: one file save (file channel) or one KiCad commit (live)."""
    require_write_mode("pcb_move_footprints")
    if not moves:
        raise LayerError(INVALID_ARGUMENT, "Give at least one move.")
    rows: list[tuple[str, Point | None, float | None, str | None]] = []
    for m in moves:
        ref = str(m.get("ref") or "")
        x, y = m.get("x"), m.get("y")
        if not ref:
            raise LayerError(INVALID_ARGUMENT, "Every move needs a ref.")
        if (x is None) != (y is None):
            raise LayerError(INVALID_ARGUMENT, f"{ref}: give both x and y, or neither.")
        rows.append((ref, (float(x), float(y)) if x is not None else None, float(m["rotation"]) if m.get("rotation") is not None else None, m.get("side")))
    path = _board_path(board_path)
    pick = pick_channel(path, channel, force=force)
    ch, path = pick.channel, pick.path
    summary = f"move {len(rows)} footprint(s) in one " + ("commit" if ch == "ipc" else "write")
    if ch == "ipc":
        if dry_run:
            return _result("ipc", path, summary + " (dry run)", {"moves": [{"ref": r[0]} for r in rows]}, dry_run=True)
        try:
            return _result("ipc", path, summary, board_write.move_footprints(path, rows))
        except Unreachable as exc:
            pick = _fall_back(exc, path, channel, force=force)

    def edit(bf: BoardFile):
        for ref, _, _, side in rows:
            if side is not None and side != bf.find(ref).layer:
                raise LayerError(INVALID_ARGUMENT, f"{ref}: changing the side needs the live channel (KiCad flips the footprint); nothing was moved.")
        done = bf.move_footprints([(ref, at, rot) for ref, at, rot, _ in rows])
        items, report = [], []
        for before, fp in done:
            row = {"kind": "footprint", "id": fp.uuid, "ref": fp.reference, "x_mm": fp.at[0], "y_mm": fp.at[1], "rotation_deg": fp.rotation, "layer": fp.layer}
            items.append(row)
            report.append({"ref": fp.reference, "status": "moved" if (before.at, before.rotation) != (fp.at, fp.rotation) else "unchanged",
                           "before": {"x_mm": before.at[0], "y_mm": before.at[1], "rotation_deg": before.rotation, "layer": before.layer},
                           "after": {k: row[k] for k in ("x_mm", "y_mm", "rotation_deg", "layer")}})
        return {"items": items, "moves": report}

    return _file_edit(path, summary, edit, dry_run=dry_run, force=force or pick.force_save, notes=pick.warnings)


def _file_only(board_path: str | None, tool: str, *, force: bool = False) -> ChannelChoice:
    require_write_mode(tool)
    if not board_path:
        raise LayerError(INVALID_ARGUMENT, f"{tool} edits the board file: give board_path.")
    pick = pick_channel(_board_path(board_path), "file", force=force)
    return pick


def set_outline(board_path: str | None, *, rect: list[float] | None = None, polygon: list[list[float]] | None = None, corner_radius_mm: float = 0.0,
                replace: bool = True, dry_run: bool = False, force: bool = False) -> BoardEditResult:
    if (rect is None) == (polygon is None):
        raise LayerError(INVALID_ARGUMENT, "Give rect [x0, y0, x1, y1] or polygon [[x, y], ...], not both.")
    if rect is not None:
        if len(rect) != 4:
            raise LayerError(INVALID_ARGUMENT, "rect is [x0, y0, x1, y1].")
        x0, y0, x1, y1 = (float(v) for v in rect)
        x0, x1, y0, y1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            raise LayerError(INVALID_ARGUMENT, "rect has no area.")
        pts: list[Point] = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    else:
        pts = [(float(p[0]), float(p[1])) for p in polygon or []]
    pick = _file_only(board_path, "pcb_set_outline", force=force)
    path = pick.path
    summary = f"outline with {len(pts)} corners, radius {corner_radius_mm} mm" + (" replacing Edge.Cuts" if replace else "")
    return _file_edit(path, summary, lambda bf: bf.set_outline(pts, corner_radius=corner_radius_mm, replace=replace), dry_run=dry_run, force=force or pick.force_save, notes=pick.warnings)


def add_mounting_holes(board_path: str | None, holes: list[dict[str, Any]], *, dry_run: bool = False, force: bool = False) -> BoardEditResult:
    if not holes:
        raise LayerError(INVALID_ARGUMENT, "Give at least one hole.")
    pick = _file_only(board_path, "pcb_add_mounting_holes", force=force)
    path = pick.path

    def edit(bf: BoardFile):
        items = []
        for h in holes:
            fp = bf.add_mounting_hole((float(h["x"]), float(h["y"])), drill=float(h["drill"]), pad=float(h.get("pad") or 0.0), net=h.get("net") or "", ref=h.get("ref") or "")
            items.append({"kind": "footprint", "id": fp.uuid, "ref": fp.reference, "x_mm": fp.at[0], "y_mm": fp.at[1], "lib_id": fp.lib_id, "net": h.get("net") or None})
        return {"items": items}

    return _file_edit(path, f"add {len(holes)} mounting hole(s)", edit, dry_run=dry_run, force=force or pick.force_save, notes=pick.warnings)


def add_track(board_path: str | None, points: list[list[float]], *, width: float, layer: str, net: str, channel: str = "auto", dry_run: bool = False, force: bool = False) -> BoardEditResult:
    require_write_mode("pcb_add_track")
    pts: list[Point] = [(float(p[0]), float(p[1])) for p in points]
    path = _board_path(board_path)
    pick = pick_channel(path, channel, force=force)
    ch, path = pick.channel, pick.path
    summary = f"track on {net}, {len(pts) - 1} segment(s), {width} mm on {layer}"
    if ch == "ipc":
        if dry_run:
            return _result("ipc", path, summary + " (dry run)", {}, dry_run=True)
        try:
            return _result("ipc", path, summary, board_write.add_track(path, pts, width=width, layer=layer, net=net))
        except Unreachable as exc:
            pick = _fall_back(exc, path, channel, force=force)

    def edit(bf: BoardFile):
        ids = [bf.add_segment(a, b, width=width, layer=layer, net=net) for a, b in zip(pts, pts[1:]) if a != b]
        return {"items": [{"kind": "track", "id": i, "net": net, "layer": layer} for i in ids]}

    return _file_edit(path, summary, edit, dry_run=dry_run, force=force or pick.force_save, notes=pick.warnings)


def add_via(board_path: str | None, x_mm: float, y_mm: float, *, net: str, size: float = 0.8, drill: float = 0.3, channel: str = "auto", dry_run: bool = False, force: bool = False) -> BoardEditResult:
    require_write_mode("pcb_add_via")
    path = _board_path(board_path)
    pick = pick_channel(path, channel, force=force)
    ch, path = pick.channel, pick.path
    summary = f"via on {net} at ({x_mm}, {y_mm}) {size}/{drill} mm"
    if ch == "ipc":
        if dry_run:
            return _result("ipc", path, summary + " (dry run)", {}, dry_run=True)
        try:
            return _result("ipc", path, summary, board_write.add_via(path, (x_mm, y_mm), net=net, size=size, drill=drill))
        except Unreachable as exc:
            pick = _fall_back(exc, path, channel, force=force)

    def edit(bf: BoardFile):
        i = bf.add_via((x_mm, y_mm), net=net, size=size, drill=drill)
        return {"items": [{"kind": "via", "id": i, "net": net, "x_mm": x_mm, "y_mm": y_mm}]}

    return _file_edit(path, summary, edit, dry_run=dry_run, force=force or pick.force_save, notes=pick.warnings)


def add_zone(board_path: str | None, polygon: list[list[float]], *, net: str, layer: str, name: str = "", channel: str = "auto", dry_run: bool = False, force: bool = False) -> BoardEditResult:
    require_write_mode("pcb_add_zone")
    pts: list[Point] = [(float(p[0]), float(p[1])) for p in polygon]
    path = _board_path(board_path)
    pick = pick_channel(path, channel, force=force)
    ch, path = pick.channel, pick.path
    summary = f"zone {name or net} on {layer} with {len(pts)} points"
    if ch == "ipc":
        if dry_run:
            return _result("ipc", path, summary + " (dry run)", {}, dry_run=True)
        try:
            return _result("ipc", path, summary, board_write.add_zone(path, pts, net=net, layer=layer, name=name))
        except Unreachable as exc:
            pick = _fall_back(exc, path, channel, force=force)

    def edit(bf: BoardFile):
        i = bf.add_zone(pts, net=net, layer=layer, name=name)
        return {"items": [{"kind": "zone", "id": i, "net": net, "layer": layer, "name": name}]}

    return _file_edit(path, summary, edit, dry_run=dry_run, force=force or pick.force_save, notes=pick.warnings)


def refill_zones(board_path: str | None, *, channel: str = "auto", allow_default_rules: bool = False) -> BoardEditResult:
    require_write_mode("pcb_refill_zones")
    path = _board_path(board_path)
    ch, path = choose_channel(path, channel)
    rule_warnings = board_rule_warnings(path) if path is not None else []
    if rule_warnings and not allow_default_rules:
        # a fill against the wrong clearances is copper in the wrong place, saved into the board
        raise LayerError(PROJECT_NOT_FOUND, "; ".join(rule_warnings) + ". Refusing to refill against the wrong rules.",
                         hint=f"Put {path.with_suffix('.kicad_pro').name} (and {path.with_suffix('.kicad_dru').name} for custom rules) next to the board, "
                              "or pass allow_default_rules=true to fill with KiCad's defaults anyway.")
    if ch == "ipc":
        data = board_write.refill_zones(path)
        return _result("ipc", path, "refill zones", {"items": [], "board": data.get("board"), "filled": data.get("filled"), "seconds": data.get("seconds"),
                                                     "warnings": rule_warnings + ([data["note"]] if data.get("note") else [])})
    from kicad_layer.cli import runner
    from kicad_layer.cli.discovery import find_kicad_cli
    from kicad_layer.config import settings

    cli = find_kicad_cli()
    r = runner.run([cli.path, "pcb", "drc", "--refill-zones", "--save-board", "--format", "json", "-o", str(settings().cache_dir / "reports" / f"{path.stem}-refill.json"), path],
                   timeout_s=settings().cli_long_timeout_s, cwd=path.parent)
    ok = r.returncode in (0, 5)
    return _result("file", path, "refill zones with kicad-cli and save", {"items": [], "filled": ok, "exit_code": r.returncode, "warnings": rule_warnings + ([] if ok else [r.tail()])})


def delete_items(board_path: str | None, ids: list[str], *, channel: str = "auto", dry_run: bool = False, force: bool = False) -> BoardEditResult:
    require_write_mode("pcb_delete_items")
    if not ids:
        raise LayerError(INVALID_ARGUMENT, "Give at least one item id.")
    path = _board_path(board_path)
    pick = pick_channel(path, channel, force=force)
    ch, path = pick.channel, pick.path
    summary = f"delete {len(ids)} item(s)"
    if ch == "ipc":
        if dry_run:
            return _result("ipc", path, summary + " (dry run)", {}, dry_run=True)
        try:
            return _result("ipc", path, summary, board_write.delete_items(path, ids))
        except Unreachable as exc:
            pick = _fall_back(exc, path, channel, force=force)

    def edit(bf: BoardFile):
        return {"deleted": [{"id": i, "kind": bf.delete(i)} for i in ids]}

    return _file_edit(path, summary, edit, dry_run=dry_run, force=force or pick.force_save, notes=pick.warnings)


def save_board(board_path: str | None) -> BoardEditResult:
    require_write_mode("pcb_save")
    path = _board_path(board_path)
    data = board_write.save(path)
    return _result("ipc", data["board"], "save the open board to disk", {"items": [], "written": data["written"], "size": data["size"], "warnings": ["Saved by KiCad itself; run_drc now reads the current state."]})
