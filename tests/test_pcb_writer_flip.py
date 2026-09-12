"""Back-side footprint placement: the writer must mirror a footprint the way KiCad's own flip does.

The conventions are read off KiCad-authored boards (pcb_writer's module docstring names them) and
pinned here two ways: against the back-side parts in the fixture boards, re-placed from the same
library footprint at the same origin, rotation and side; and against kicad-cli's library-parity
DRC check, which flips the library copy itself and reports every pad, angle or shape that differs.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest

from kicad_layer import libtables, review
from kicad_layer.cli import reports
from kicad_layer.config import load_settings, set_settings
from kicad_layer.kicad_libs import Footprint, load_footprint, register_footprint_lib
from kicad_layer.pcb_writer import BoardBuilder, flip_layer, toggle_mirror
from kicad_layer.sexpr import child, children, parse, tag, value
from tests.conftest import FIXTURES, real_kicad

def _research_fixture(name: str) -> Path:
    """The research/fixtures directory sits beside the repository; from a git worktree it is a few
    levels further up, so walk the parents rather than count them."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "research" / "fixtures" / name
        if candidate.is_dir():
            return candidate
    return Path(__file__).parent.parent.parent / "research" / "fixtures" / name


CM5_MINIMA = _research_fixture("cm5_minima")
MULTICHANNEL = FIXTURES / "multichannel"
SOIC = "multichannel_mixer:SOIC127P600X175-8N"


def _fixture_footprint(board: Path, lib_id: str, at: tuple[float, float]):
    root = parse(board.read_text(encoding="utf-8", errors="replace"))
    for fp in children(root, "footprint"):
        a = child(fp, "at")
        if str(fp[1]) == lib_id and (float(a[1]), float(a[2])) == at:
            return fp
    raise AssertionError(f"{lib_id} at {at} not in {board.name}")


def _project_footprint(project_dir: Path, lib_id: str) -> Footprint:
    """The library footprint behind a lib_id, resolved through the project's fp-lib-table as the index does."""
    lib, _, name = lib_id.partition(":")
    for e in libtables.project_entries("footprint", project_dir):
        if e.nickname == lib:
            register_footprint_lib(lib, e.path)
    return load_footprint(lib, name)


def _pads(node) -> dict[str, tuple[float, float, float]]:
    """Pad number -> (local x, local y, angle) as written."""
    out = {}
    for p in children(node, "pad"):
        a = child(p, "at")
        out[str(p[1])] = (float(a[1]), float(a[2]), float(a[3]) % 360 if len(a) > 3 else 0.0)
    return out


def _props(node) -> dict[str, list]:
    return {str(p[1]): p for p in children(node, "property")}


def _justify(text_node) -> list[str]:
    eff = child(text_node, "effects")
    j = child(eff, "justify") if eff is not None else None
    return [str(a) for a in j[1:]] if j is not None else []


def _shapes(fp_node) -> list:
    """(tag, layer, sorted points) of every graphic, order-free, so KiCad's re-sorting on save does not matter."""
    out = []
    for g in fp_node:
        if tag(g) not in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly"):
            continue
        pts = [(k, float(c[1]), float(c[2])) for k in ("start", "end", "mid", "center") if (c := child(g, k)) is not None]
        if child(g, "pts") is not None:
            pts += [("xy", float(xy[1]), float(xy[2])) for xy in children(child(g, "pts"), "xy")]
        out.append((tag(g), value(g, "layer"), tuple(sorted((k, round(x, 4) + 0.0, round(y, 4) + 0.0) for k, x, y in pts))))
    return sorted(out)


# ---------------------------------------------------------------------------------------------
# the transform itself
# ---------------------------------------------------------------------------------------------


def test_flip_layer_names():
    assert flip_layer("F.Cu") == "B.Cu" and flip_layer("B.SilkS") == "F.SilkS" and flip_layer("F.CrtYd") == "B.CrtYd"
    for stays in ("*.Cu", "*.Mask", "F&B.Cu", "In1.Cu", "Edge.Cuts", "Dwgs.User", "User.1"):
        assert flip_layer(stays) == stays


SYNTHETIC = """(footprint "Synthetic"
	(version 20260206)
	(generator "pcbnew")
	(layer "F.Cu")
	(property "Reference" "REF**" (at 1 -2 45) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15)) (justify left)))
	(property "Value" "V" (at 0 3 0) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15)) (justify mirror)))
	(fp_line (start -1 -2) (end 1 -2) (stroke (width 0.1) (type solid)) (layer "F.SilkS"))
	(fp_arc (start 0 1) (mid 0.7071 0.7071) (end 1 0) (stroke (width 0.1) (type solid)) (layer "F.Fab"))
	(fp_circle (center 0.5 0.5) (end 0.7 0.5) (stroke (width 0.1) (type solid)) (fill no) (layer "F.CrtYd"))
	(fp_poly (pts (xy 0 0) (xy 1 0) (xy 1 1)) (stroke (width 0.1) (type solid)) (fill yes) (layer "B.SilkS"))
	(fp_text user "${REFERENCE}" (at 0 0.5 90) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))
	(pad "1" smd roundrect (at -1 0.5 30) (size 1 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (chamfer_ratio 0.2) (chamfer top_left))
	(pad "2" thru_hole oval (at 1 0.5) (size 1 1.6) (drill oval 0.6 1.2 (offset 0 0.2)) (layers "*.Cu" "*.Mask"))
	(pad "3" smd custom (at 0 -1 0) (size 0.3 0.3) (layers "F.Cu" "F.Mask") (options (clearance outline) (anchor rect))
		(primitives (gr_poly (pts (xy 1 0) (xy 0.5 0.75) (xy -0.5 0.75)) (fill yes))))
)"""


def test_back_side_transform_is_kicads_flip():
    """Mirror about the footprint's X axis: y negated, pad angles negated, texts turned 180 and mirrored,
    F.* <-> B.*, arcs swapped end for end. Every rule here was read off a KiCad-authored board."""
    fp = Footprint(lib="Synthetic", name="Synthetic", tree=parse(SYNTHETIC), pads=[])
    front = BoardBuilder(sheetfile="s").footprint(fp, "U1", "V", (10, 20), 90, path_uuid="p", pad_nets={"1": "A"}, hide_ref=False)
    back = BoardBuilder(sheetfile="s").footprint(fp, "U1", "V", (10, 20), 90, path_uuid="p", pad_nets={"1": "A"}, hide_ref=False, layer="B.Cu")
    assert value(back, "layer") == "B.Cu" and child(back, "at")[1:] == ["10", "20", "90"]

    # pads: x kept, y negated, angle = rot - own angle (front is rot + own angle)
    assert _pads(front)["1"] == (-1, 0.5, 120)
    assert _pads(back) == {"1": (-1, -0.5, 60), "2": (1, -0.5, 90), "3": (0, 1, 90)}
    p1, p2, p3 = (next(p for p in children(back, "pad") if str(p[1]) == n) for n in ("1", "2", "3"))
    assert child(p1, "layers")[1:] == ["B.Cu", "B.Paste", "B.Mask"]
    assert child(p2, "layers")[1:] == ["*.Cu", "*.Mask"]
    assert child(p1, "chamfer")[1:] == ["bottom_left"]
    assert child(child(p2, "drill"), "offset")[1:] == ["0", "-0.2"]
    poly = child(child(child(p3, "primitives"), "gr_poly"), "pts")
    assert [xy[1:] for xy in children(poly, "xy")] == [["1", "0"], ["0.5", "-0.75"], ["-0.5", "-0.75"]]

    # graphics: y negated, layers swapped both ways, arcs keep their sweep by swapping start and end
    line = child(back, "fp_line")
    assert child(line, "start")[1:] == ["-1", "2"] and child(line, "end")[1:] == ["1", "2"] and value(line, "layer") == "B.SilkS"
    arc = child(back, "fp_arc")
    assert (child(arc, "start")[1:], child(arc, "mid")[1:], child(arc, "end")[1:]) == (["1", "0"], ["0.7071", "-0.7071"], ["0", "-1"])
    assert value(child(back, "fp_poly"), "layer") == "F.SilkS"
    assert child(child(back, "fp_circle"), "center")[1:] == ["0.5", "-0.5"]

    # texts: angle = rot + 180 - own angle, position mirrored, mirror flag toggled, layer swapped
    props = _props(back)
    assert child(props["Reference"], "at")[1:] == ["1", "2", "225"] and value(props["Reference"], "layer") == "B.SilkS"
    assert _justify(props["Reference"]) == ["left", "mirror"]
    assert child(props["Value"], "at")[1:] == ["0", "-3", "270"] and _justify(props["Value"]) == []  # was mirrored in the library
    assert child(props["Datasheet"], "at")[1:] == ["0", "0", "270"] and _justify(props["Datasheet"]) == ["mirror"]
    txt = child(back, "fp_text")
    assert child(txt, "at")[1:] == ["0", "-0.5", "180"] and value(txt, "layer") == "B.Fab" and _justify(txt) == ["mirror"]
    # the front keeps its geometry; text angles are absolute in KiCad files, so they turn with the part
    assert child(child(front, "fp_text"), "at")[1:] == ["0", "0.5", "180"]
    assert child(child(front, "fp_arc"), "start")[1:] == ["0", "1"]


def test_only_copper_sides_are_accepted():
    fp = Footprint(lib="Synthetic", name="Synthetic", tree=parse(SYNTHETIC), pads=[])
    with pytest.raises(ValueError):
        BoardBuilder(sheetfile="s").footprint(fp, "U1", "V", (0, 0), 0, path_uuid="p", pad_nets={}, layer="In1.Cu")


# ---------------------------------------------------------------------------------------------
# against KiCad-authored boards
# ---------------------------------------------------------------------------------------------


def test_flipped_rotated_soic_pads_land_where_kicad_put_them(tmp_path):
    """IC4 of the multichannel demo: an SOIC-8 on B.Cu at -90. Re-placed from the project library at the
    same origin, rotation and side, every pad must have the coordinates KiCad wrote, and pad 1 must sit
    where the demo's routed B.Cu track on Net-(IC4A-OUT) starts."""
    board = MULTICHANNEL / "multichannel_mixer.kicad_pcb"
    theirs = _fixture_footprint(board, SOIC, (168.365, 84.1))
    assert value(theirs, "layer") == "B.Cu" and child(theirs, "at")[3] == "-90"
    ref = _props(theirs)["Reference"][2]
    fp = _project_footprint(MULTICHANNEL, SOIC)
    nets = {str(p[1]): value(p, "net") for p in children(theirs, "pad")}

    pcb = BoardBuilder(sheetfile="multichannel_mixer.kicad_sch")
    pcb.rounded_rect_outline(150, 70, 190, 100, 1)
    ours = pcb.footprint(fp, ref, "TL072CD", (168.365, 84.1), -90, path_uuid="x", pad_nets=nets, layer="B.Cu")
    out = tmp_path / "soic.kicad_pcb"
    pcb.write(str(out))

    mine, kicad = _pads(ours), _pads(theirs)
    assert mine.keys() == kicad.keys() == {str(i) for i in range(1, 9)}
    for n in kicad:
        assert mine[n][:2] == pytest.approx(kicad[n][:2], abs=1e-3), n
    assert mine["1"][:2] == (-2.7, 1.905)  # library (-2.7 -1.905): y negated, x kept
    assert _shapes(ours) == _shapes(theirs)

    shutil.copy(board, tmp_path / board.name)
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    try:
        got = {p.number: p for f in review.load_board(out).footprints for p in f.pads}
        expect = {p.number: p for f in review.load_board(tmp_path / board.name).footprints if f.ref == ref for p in f.pads}
    finally:
        set_settings(None)
    for n, p in expect.items():
        assert (got[n].x, got[n].y) == pytest.approx((p.x, p.y), abs=1e-3), n
        assert got[n].layers == p.layers == ["B.Cu", "B.Mask", "B.Paste"]
    assert (got["1"].x, got["1"].y) == pytest.approx((166.46, 81.4), abs=1e-3)


def test_load_board_reads_back_side_pads_from_a_kicad_file(workspace):
    """The routed multichannel demo: IC4's pad 1 is where its B.Cu track starts, on the back layers."""
    bm = review.load_board(MULTICHANNEL / "multichannel_mixer.kicad_pcb")
    ic4 = next(f for f in bm.footprints if f.ref == "IC4")
    assert ic4.layer == "B.Cu" and ic4.rotation == -90
    pad1 = next(p for p in ic4.pads if p.number == "1")
    assert (pad1.x, pad1.y) == pytest.approx((166.46, 81.4), abs=1e-3)
    assert pad1.layers == ["B.Cu", "B.Mask", "B.Paste"] and pad1.net == "Net-(IC4A-OUT)"
    assert any(s.layer == "B.Cu" and s.net == pad1.net and (s.x1, s.y1) == pytest.approx((pad1.x, pad1.y), abs=1e-3) for s in bm.segments)


def test_flipped_custom_pads_match_the_pic_programmer_demo():
    """JP1 of the pic_programmer demo: a solder jumper with custom pads, flipped to the back at 0."""
    board = FIXTURES / "pic_programmer" / "pic_programmer.kicad_pcb"
    lib_id = "Jumper:SolderJumper-2_P1.3mm_Open_TrianglePad1.0x1.5mm"
    theirs = _fixture_footprint(board, lib_id, (148.082, 97.79))
    fp = load_footprint(*lib_id.split(":"))
    ours = BoardBuilder(sheetfile="pic_programmer.kicad_sch").footprint(fp, "JP1", "JUMPER", (148.082, 97.79), 0, path_uuid="x", pad_nets={}, layer="B.Cu", hide_ref=False)

    assert _pads(ours) == _pads(theirs)

    def primitives(node):
        return [[(float(xy[1]), float(xy[2])) for xy in children(child(g, "pts"), "xy")] for p in children(node, "pad") for g in children(child(p, "primitives"), "gr_poly")]

    assert primitives(ours) == primitives(theirs)
    assert _shapes(ours) == _shapes(theirs)
    for name in ("Reference", "Value", "Datasheet"):
        assert child(_props(ours)[name], "at")[1:] == child(_props(theirs)[name], "at")[1:], name
        assert value(_props(ours)[name], "layer") == value(_props(theirs)[name], "layer"), name
        assert _justify(_props(ours)[name]) == _justify(_props(theirs)[name]) == ["mirror"], name
    assert child(_props(ours)["Reference"], "at")[1:] == ["0", "1.8", "180"]  # library (0 -1.8 0)


@pytest.mark.skipif(not (CM5_MINIMA / "CM5_MINIMA_3.kicad_pcb").is_file(), reason="CM5 MINIMA board not on this machine")
def test_flipped_rotated_0402_matches_the_cm5_minima_board():
    """R104 sits on B.Cu at 90 in a native KiCad 9 design: pad angles and the text angles follow, and a
    library field that was already mirrored comes out plain."""
    board = CM5_MINIMA / "CM5_MINIMA_3.kicad_pcb"
    theirs = _fixture_footprint(board, "CM5IO:R_0402_1005Metric", (131.69, 48.54))
    fp = _project_footprint(CM5_MINIMA, "CM5IO:R_0402_1005Metric")
    ours = BoardBuilder(sheetfile="CM5_MINIMA_3.kicad_sch").footprint(fp, "R104", "10k", (131.69, 48.54), 90, path_uuid="x", pad_nets={}, layer="B.Cu")
    assert _pads(ours) == _pads(theirs) == {"1": (-0.51, 0.0, 90.0), "2": (0.51, 0.0, 90.0)}
    assert _shapes(ours) == _shapes(theirs)
    for name in ("Value", "Datasheet", "Description"):
        assert child(_props(ours)[name], "at")[1:] == child(_props(theirs)[name], "at")[1:], name
    assert child(_props(ours)["Value"], "at")[1:] == ["0", "-1.17", "90"]  # library (0 1.17 180)
    assert child(_props(ours)["Datasheet"], "at")[1:] == ["0", "0", "270"]
    assert child(child(ours, "fp_text"), "at")[1:] == child(child(theirs, "fp_text"), "at")[1:] == ["0", "0", "90"]
    lcsc = copy.deepcopy(_props(fp.tree)["LCSC"])
    assert _justify(lcsc) == ["mirror"]
    toggle_mirror(lcsc)
    assert _justify(lcsc) == _justify(_props(theirs)["LCSC"]) == []


# ---------------------------------------------------------------------------------------------
# against kicad-cli
# ---------------------------------------------------------------------------------------------


@real_kicad
def test_back_side_footprints_pass_drc_and_library_parity(tmp_path):
    """The same footprint on F.Cu and, rotated, on B.Cu; an SOIC with rotated pads and a jumper with
    custom pads on the back, joined by B.Cu tracks that end where pad_position says the pads are.
    kicad-cli's library-parity check flips the library copy itself, so a pad, angle or shape that
    differs from KiCad's result shows up as a footprint finding."""
    shutil.copytree(MULTICHANNEL / "multichannel_mixer.pretty", tmp_path / "multichannel_mixer.pretty")
    shutil.copy(MULTICHANNEL / "fp-lib-table", tmp_path / "fp-lib-table")
    r = load_footprint("Resistor_SMD", "R_0603_1608Metric")
    jp = load_footprint("Jumper", "SolderJumper-2_P1.3mm_Open_TrianglePad1.0x1.5mm")
    soic = _project_footprint(tmp_path, SOIC)

    pcb = BoardBuilder(sheetfile="flip.kicad_sch", title="flip")
    pcb.rounded_rect_outline(50, 50, 90, 70, 1)
    pcb.footprint(r, "R1", "10k", (58, 58), 0, path_uuid="a", pad_nets={"1": "A", "2": "B"}, hide_ref=False)
    pcb.footprint(r, "R2", "10k", (58, 64), 45, path_uuid="b", pad_nets={"1": "N1"}, hide_ref=False, layer="B.Cu")
    pcb.footprint(jp, "JP1", "JP", (70, 64), 90, path_uuid="c", pad_nets={"1": "N1", "2": "N2"}, hide_ref=False, layer="B.Cu")
    pcb.footprint(soic, "IC1", "TL072", (82, 60), -90, path_uuid="d", pad_nets={"1": "N2"}, hide_ref=False, layer="B.Cu")
    pcb.track([r.pad_position("1", 58, 64, 45, layer="B.Cu"), jp.pad_position("1", 70, 64, 90, layer="B.Cu")], width=0.25, layer="B.Cu", net="N1")
    pcb.track([soic.pad_position("1", 82, 60, -90, layer="B.Cu"), jp.pad_position("2", 70, 64, 90, layer="B.Cu")], width=0.25, layer="B.Cu", net="N2")
    out = tmp_path / "flip.kicad_pcb"
    pcb.write(str(out))

    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    try:
        drc = reports.run_drc(out, severity="all", schematic_parity=False)
    finally:
        set_settings(None)
    listing = [(f.severity, f.type, f.description) for f in drc.findings]
    assert drc.verdict in ("PASS", "WARN"), listing
    assert not [f for f in drc.findings if f.severity == "error"], listing
    assert not [f for f in drc.findings if "footprint" in f.type or f.category == "parity"], listing
    assert not [f for f in drc.findings if f.category == "unconnected"], listing
