"""The seed's row packing: no overlaps, rows wrap at the width, references sort by letters then number."""

from __future__ import annotations

from kicad_layer.design.seed import Pile, bottom, pack, sort_key


def test_sort_key_groups_prefixes_and_orders_numbers_numerically():
    refs = ["C10", "C2", "R1", "MOD2", "MOD1", "J14", "J3"]
    assert sorted(refs, key=sort_key) == ["C2", "C10", "J3", "J14", "MOD1", "MOD2", "R1"]


def _placed(boxes, pos):
    return {r: (x + b[0], y + b[1], x + b[2], y + b[3]) for r, b in boxes.items() for x, y in [pos[r]]}


def _no_overlap(placed):
    refs = list(placed)
    for i, a in enumerate(refs):
        for b in refs[i + 1:]:
            ax0, ay0, ax1, ay1 = placed[a]
            bx0, by0, bx1, by1 = placed[b]
            assert ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0, f"{a} and {b} overlap"


def test_pack_rows_do_not_overlap_and_wrap_at_the_width():
    boxes = {"A": (-5, -2.5, 5, 2.5), "B": (-5, -4, 5, 4), "C": (-5, -2.5, 5, 2.5), "D": (-15, -2, 15, 2)}
    pos = pack(boxes, Pile(origin=(0, 0), width=25, gap=1))
    assert pos["A"] == (5, 2.5) and pos["B"] == (16, 4), "first row: A then B, origins at the centres of centred boxes"
    assert pos["C"] == (5, 11.5), "C wraps to the second row below the tallest part of the first (8) plus the gap"
    assert pos["D"] == (15, 17), "a part wider than the width still gets its own row: below C (5 tall) plus the gap"
    _no_overlap(_placed(boxes, pos))
    assert bottom(boxes, pos) == 19


def test_pack_puts_an_off_centre_box_by_its_corner_not_its_origin():
    boxes = {"MOD1": (-3.5, -51.5, 36.5, 3.5), "C1": (-1, -1, 1, 1)}  # the Compute Module hangs above its origin
    pos = pack(boxes, Pile(origin=(20, 20), width=160, gap=2))
    assert pos["MOD1"] == (23.5, 71.5), "origin placed so the box's top-left corner lands on the pile's origin"
    placed = _placed(boxes, pos)
    assert placed["MOD1"] == (20, 20, 60, 75)
    _no_overlap(placed)
