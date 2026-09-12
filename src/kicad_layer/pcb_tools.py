"""Board editing tools: choose the channel, apply the rules, report what happened.

Channel choice per operation:

* ``ipc`` when the board is open in KiCad's PCB Editor: edits land as undo steps.
* ``file`` when it is not: the file is edited losslessly and snapshotted.
* ``auto`` (default) picks ipc if the board is open, otherwise file. A board this process
  has ever seen live is never edited through the file channel, whatever is asked, because
  KiCad may hold unsaved changes to it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from kicad_layer.errors import BOARD_NOT_OPEN, EDIT_CONFLICT, INVALID_ARGUMENT, LayerError, require_write_mode
from kicad_layer.ipc import board_write
from kicad_layer.ipc.session import Rejected, Unreachable, get_session
from kicad_layer.models import BoardEditResult
from kicad_layer.paths import BOARD, display, resolve_in_workspace
from kicad_layer.pcb_edit import BoardFile

Point = tuple[float, float]


def _board_path(board_path: str | None) -> Path | None:
    return resolve_in_workspace(board_path, suffixes=(BOARD,)) if board_path else None


def choose_channel(board_path: Path | None, requested: str) -> tuple[str, Path | None]:
    """Return (channel, board path). Raises when the request cannot be honoured safely."""
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
        return "ipc", board_path
    if board_path is None:
        raise LayerError(INVALID_ARGUMENT, "board_path is required for a file edit.")
    if session.has_seen_live(board_path):
        raise LayerError(
            EDIT_CONFLICT,
            f"{board_path.name} was open in KiCad during this session, so it is not edited on disk.",
            hint="Open it in the PCB Editor and edit it live, or restart the server after KiCad has saved and closed it.",
        )
    lock = board_path.parent / f"~{board_path.name}.lck"
    if lock.exists():
        raise LayerError(
            EDIT_CONFLICT,
            f"KiCad has {board_path.name} open (lock file present) but the API does not report it.",
            hint="Enable the KiCad API and use the live channel, or close the PCB Editor for a file edit.",
        )
    return "file", board_path


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


def _file_edit(board_path: Path, summary: str, fn, *, dry_run: bool, force: bool) -> BoardEditResult:
    bf = BoardFile(board_path)
    data = fn(bf) or {}
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
    ch, path = choose_channel(path, channel)
    summary = f"place {lib_id} as {ref} at ({x_mm}, {y_mm}) rot {rotation} on {layer}"
    if ch == "ipc":
        if dry_run:
            return _result("ipc", path, summary + " (dry run)", {}, dry_run=True)
        return _result("ipc", path, summary, board_write.place_footprint(path, lib_id, ref, (x_mm, y_mm), rotation, value_text=value, layer=layer))

    def edit(bf: BoardFile):
        fp = bf.place_footprint(lib_id, ref, (x_mm, y_mm), rotation, value_text=value, layer=layer)
        return {"items": [{"kind": "footprint", "id": fp.uuid, "ref": fp.reference, "x_mm": fp.at[0], "y_mm": fp.at[1], "rotation_deg": fp.rotation, "layer": fp.layer}]}

    return _file_edit(path, summary, edit, dry_run=dry_run, force=force)


def move_footprint(board_path: str | None, ref: str, *, x_mm: float | None = None, y_mm: float | None = None, rotation: float | None = None,
                   layer: str | None = None, channel: str = "auto", dry_run: bool = False, force: bool = False) -> BoardEditResult:
    require_write_mode("pcb_move_footprint")
    if (x_mm is None) != (y_mm is None):
        raise LayerError(INVALID_ARGUMENT, "Give both x_mm and y_mm, or neither.")
    path = _board_path(board_path)
    ch, path = choose_channel(path, channel)
    at = (x_mm, y_mm) if x_mm is not None else None
    summary = f"move {ref}" + (f" to ({x_mm}, {y_mm})" if at else "") + (f" rot {rotation}" if rotation is not None else "") + (f" to {layer}" if layer else "")
    if ch == "ipc":
        if dry_run:
            return _result("ipc", path, summary + " (dry run)", {}, dry_run=True)
        return _result("ipc", path, summary, board_write.move_footprint(path, ref, at, rotation, layer))
    if layer is not None:
        raise LayerError(INVALID_ARGUMENT, "Changing the layer needs the live channel (KiCad flips the footprint).")

    def edit(bf: BoardFile):
        fp = bf.move_footprint(ref, at, rotation)
        return {"items": [{"kind": "footprint", "id": fp.uuid, "ref": fp.reference, "x_mm": fp.at[0], "y_mm": fp.at[1], "rotation_deg": fp.rotation, "layer": fp.layer}]}

    return _file_edit(path, summary, edit, dry_run=dry_run, force=force)


def add_track(board_path: str | None, points: list[list[float]], *, width: float, layer: str, net: str, channel: str = "auto", dry_run: bool = False, force: bool = False) -> BoardEditResult:
    require_write_mode("pcb_add_track")
    pts: list[Point] = [(float(p[0]), float(p[1])) for p in points]
    path = _board_path(board_path)
    ch, path = choose_channel(path, channel)
    summary = f"track on {net}, {len(pts) - 1} segment(s), {width} mm on {layer}"
    if ch == "ipc":
        if dry_run:
            return _result("ipc", path, summary + " (dry run)", {}, dry_run=True)
        return _result("ipc", path, summary, board_write.add_track(path, pts, width=width, layer=layer, net=net))

    def edit(bf: BoardFile):
        ids = [bf.add_segment(a, b, width=width, layer=layer, net=net) for a, b in zip(pts, pts[1:]) if a != b]
        return {"items": [{"kind": "track", "id": i, "net": net, "layer": layer} for i in ids]}

    return _file_edit(path, summary, edit, dry_run=dry_run, force=force)


def add_via(board_path: str | None, x_mm: float, y_mm: float, *, net: str, size: float = 0.8, drill: float = 0.3, channel: str = "auto", dry_run: bool = False, force: bool = False) -> BoardEditResult:
    require_write_mode("pcb_add_via")
    path = _board_path(board_path)
    ch, path = choose_channel(path, channel)
    summary = f"via on {net} at ({x_mm}, {y_mm}) {size}/{drill} mm"
    if ch == "ipc":
        if dry_run:
            return _result("ipc", path, summary + " (dry run)", {}, dry_run=True)
        return _result("ipc", path, summary, board_write.add_via(path, (x_mm, y_mm), net=net, size=size, drill=drill))

    def edit(bf: BoardFile):
        i = bf.add_via((x_mm, y_mm), net=net, size=size, drill=drill)
        return {"items": [{"kind": "via", "id": i, "net": net, "x_mm": x_mm, "y_mm": y_mm}]}

    return _file_edit(path, summary, edit, dry_run=dry_run, force=force)


def add_zone(board_path: str | None, polygon: list[list[float]], *, net: str, layer: str, name: str = "", channel: str = "auto", dry_run: bool = False, force: bool = False) -> BoardEditResult:
    require_write_mode("pcb_add_zone")
    pts: list[Point] = [(float(p[0]), float(p[1])) for p in polygon]
    path = _board_path(board_path)
    ch, path = choose_channel(path, channel)
    summary = f"zone {name or net} on {layer} with {len(pts)} points"
    if ch == "ipc":
        if dry_run:
            return _result("ipc", path, summary + " (dry run)", {}, dry_run=True)
        return _result("ipc", path, summary, board_write.add_zone(path, pts, net=net, layer=layer, name=name))

    def edit(bf: BoardFile):
        i = bf.add_zone(pts, net=net, layer=layer, name=name)
        return {"items": [{"kind": "zone", "id": i, "net": net, "layer": layer, "name": name}]}

    return _file_edit(path, summary, edit, dry_run=dry_run, force=force)


def refill_zones(board_path: str | None, *, channel: str = "auto") -> BoardEditResult:
    require_write_mode("pcb_refill_zones")
    path = _board_path(board_path)
    ch, path = choose_channel(path, channel)
    if ch == "ipc":
        data = board_write.refill_zones(path)
        return _result("ipc", path, "refill zones", {"items": [], "board": data.get("board"), "filled": data.get("filled"), "seconds": data.get("seconds"), "warnings": [data["note"]] if data.get("note") else []})
    from kicad_layer.cli import runner
    from kicad_layer.cli.discovery import find_kicad_cli
    from kicad_layer.config import settings

    cli = find_kicad_cli()
    r = runner.run([cli.path, "pcb", "drc", "--refill-zones", "--save-board", "--format", "json", "-o", str(settings().cache_dir / "reports" / f"{path.stem}-refill.json"), path],
                   timeout_s=settings().cli_long_timeout_s, cwd=path.parent)
    ok = r.returncode in (0, 5)
    return _result("file", path, "refill zones with kicad-cli and save", {"items": [], "filled": ok, "exit_code": r.returncode, "warnings": [] if ok else [r.tail()]})


def delete_items(board_path: str | None, ids: list[str], *, channel: str = "auto", dry_run: bool = False, force: bool = False) -> BoardEditResult:
    require_write_mode("pcb_delete_items")
    if not ids:
        raise LayerError(INVALID_ARGUMENT, "Give at least one item id.")
    path = _board_path(board_path)
    ch, path = choose_channel(path, channel)
    summary = f"delete {len(ids)} item(s)"
    if ch == "ipc":
        if dry_run:
            return _result("ipc", path, summary + " (dry run)", {}, dry_run=True)
        return _result("ipc", path, summary, board_write.delete_items(path, ids))

    def edit(bf: BoardFile):
        return {"deleted": [{"id": i, "kind": bf.delete(i)} for i in ids]}

    return _file_edit(path, summary, edit, dry_run=dry_run, force=force)


def save_board(board_path: str | None) -> BoardEditResult:
    require_write_mode("pcb_save")
    path = _board_path(board_path)
    data = board_write.save(path)
    return _result("ipc", data["board"], "save the open board to disk", {"items": [], "written": data["written"], "size": data["size"], "warnings": ["Saved by KiCad itself; run_drc now reads the current state."]})
