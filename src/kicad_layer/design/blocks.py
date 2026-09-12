"""Blocks: footprints whose placement relative to each other is data, applied to a hand-made board.

A ``Block`` names an anchor footprint and members with offsets from it (mm and degrees, in the anchor's
frame). ``apply`` moves every member to the anchor's position plus the rotated offset, sets its rotation, and
wraps anchor and members in a KiCad group of the block's name, so the layout tool moves them as one and the
user only places the anchor. It edits the board file as text: the members' ``(at ...)`` lines and one
``(group ...)`` per block, nothing else, so a board KiCad wrote stays KiCad's. KiCad must not hold the
board while it runs (its lock file); the user closes the PCB editor, runs ``build.py --blocks``, reopens.

The first block is the Compute Module: its two connector footprints share one coordinate frame, so MOD2 at
MOD1's position and rotation is the module, holes and outline included.
"""

from __future__ import annotations

import math
import re
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path

_TOP = re.compile(r"\n\t\((?=footprint |group )")  # a top-level footprint or group starts here (KiCad indents them one tab)
_AT = re.compile(r"\n\t\t\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)")  # the footprint's own position: the first two-tab (at ...)
_UUID = re.compile(r'\n\t\t\(uuid "([^"]+)"\)')
_REF = re.compile(r'\n\t\t\(property "Reference" "([^"]+)"')


@dataclass(frozen=True)
class Block:
    name: str
    anchor: str  # reference of the footprint the user places
    members: dict[str, tuple[float, float, float]] = field(default_factory=dict)  # ref -> (dx, dy, drot) from the anchor, mm and degrees


def _fmt(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def place(anchor: tuple[float, float, float], offset: tuple[float, float, float]) -> tuple[float, float, float]:
    """A member's board position from the anchor's ``(x, y, rot)`` and its offset in the anchor's frame.

    KiCad's board y points down and a positive rotation turns counter-clockwise on screen, so an offset to the
    right of an anchor rotated by 90 degrees points up (negative y)."""
    ax, ay, arot = anchor
    dx, dy, drot = offset
    t = math.radians(arot)
    x = ax + dx * math.cos(t) + dy * math.sin(t)
    y = ay - dx * math.sin(t) + dy * math.cos(t)
    rot = (arot + drot) % 360
    return round(x, 4), round(y, 4), round(rot, 4)


def _chunks(text: str) -> list[tuple[int, int]]:
    """(start, end) of every top-level footprint or group chunk; ``start`` is the index of the newline before ``\\t(``."""
    starts = [m.start() for m in _TOP.finditer(text)]
    out = []
    for i, s in enumerate(starts):
        nxt = text.find("\n\t(", s + 1)  # the next top-level child of any kind
        end = len(text) - 2 if nxt < 0 else nxt  # before the file's closing "\n)"
        out.append((s, end))
    return out


def footprints(text: str) -> dict[str, dict]:
    """Reference -> {"span": (start, end), "uuid": ..., "at": (x, y, rot)} for every footprint on the board."""
    out: dict[str, dict] = {}
    for s, e in _chunks(text):
        chunk = text[s:e]
        if not chunk.startswith("\n\t(footprint "):
            continue
        ref = _REF.search(chunk)
        at = _AT.search(chunk)
        uid = _UUID.search(chunk)
        if not (ref and at and uid):
            continue
        out[ref.group(1)] = {"span": (s, e), "uuid": uid.group(1), "at": (float(at.group(1)), float(at.group(2)), float(at.group(3) or 0.0)), "at_span": (s + at.start(), s + at.end())}
    return out


def _group_text(name: str, members: list[str]) -> str:
    lines = " ".join(f'"{u}"' for u in members)
    return f'\n\t(group "{name}"\n\t\t(uuid "{_uuid.uuid4()}")\n\t\t(members {lines})\n\t)'


def apply(board: Path, blocks: list[Block]) -> list[str]:
    """Apply every block to the board file; returns one line per placement and group, for the log."""
    text = board.read_text(encoding="utf-8")
    if not text.rstrip().endswith(")"):
        raise ValueError(f"{board} does not look like a KiCad board")
    report: list[str] = []
    for block in blocks:
        fps = footprints(text)
        if block.anchor not in fps:
            report.append(f"{block.name}: anchor {block.anchor} is not on the board yet (Update PCB from Schematic first)")
            continue
        anchor = fps[block.anchor]["at"]
        # members: rewrite each (at ...) line, last one first so earlier spans stay valid
        edits = []
        for ref, offset in block.members.items():
            if ref not in fps:
                report.append(f"{block.name}: member {ref} is not on the board")
                continue
            x, y, rot = place(anchor, offset)
            new = f"\n\t\t(at {_fmt(x)} {_fmt(y)}" + (f" {_fmt(rot)}" if rot else "") + ")"
            edits.append((fps[ref]["at_span"], new, ref, (x, y, rot)))
        for (a, b), new, ref, pos in sorted(edits, reverse=True):
            text = text[:a] + new + text[b:]
            report.append(f"{block.name}: {ref} at ({_fmt(pos[0])}, {_fmt(pos[1])}, {_fmt(pos[2])})")
        # the group: replace one of the same name, else append before the closing paren
        fps = footprints(text)
        members = [fps[r]["uuid"] for r in [block.anchor, *block.members] if r in fps]
        for s, e in reversed(_chunks(text)):
            if text[s:e].startswith(f'\n\t(group "{block.name}"'):
                text = text[:s] + text[e:]
        tail = text.rstrip()
        assert tail.endswith(")")
        text = tail[:-1].rstrip("\n") + _group_text(block.name, members) + "\n)\n"
        report.append(f"{block.name}: group of {len(members)}")
    board.write_text(text, encoding="utf-8", newline="\n")
    return report
