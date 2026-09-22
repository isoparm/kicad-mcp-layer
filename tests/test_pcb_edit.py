"""Board editing through the file channel, on copies of fixture boards. No KiCad needed."""

from __future__ import annotations

import shutil

import pytest

from kicad_layer.config import load_settings, set_settings
from kicad_layer.cst import mark_dirty, parse_cst, render_file
from kicad_layer.errors import LayerError
from kicad_layer.ipc.session import get_session, reset_session
from kicad_layer.pcb_edit import BoardFile
from kicad_layer.sexpr import child, children
from tests.conftest import FIXTURES, real_kicad

BOARDS = sorted(FIXTURES.rglob("*.kicad_pcb"))


@pytest.mark.parametrize("board", BOARDS, ids=[b.name for b in BOARDS])
def test_board_round_trip_is_byte_identical(board):
    text = board.read_text(encoding="utf-8")
    root = parse_cst(text)
    assert render_file(root, text) == text
    mark_dirty(root)
    assert render_file(root, text) == text


@pytest.fixture
def project(tmp_path):
    src = FIXTURES / "pic_programmer"
    dst = tmp_path / "pic_programmer"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*-backups", ".history", "~*.lck", "*.kicad_prl", "fab", "renders", "_test-*"))
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_MODE": "write", "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    reset_session()
    yield dst
    set_settings(None)
    reset_session()


def test_move_and_rotate_updates_header_and_pad_angles(project):
    path = project / "pic_programmer.kicad_pcb"
    bf = BoardFile(path)
    ref = bf.footprints()[0].reference
    before = bf.find(ref)
    moved = bf.move_footprint(ref, (before.at[0] + 5.0, before.at[1] + 2.5), before.rotation + 90)
    assert moved.at == (round(before.at[0] + 5.0, 4), round(before.at[1] + 2.5, 4))
    assert moved.rotation == (before.rotation + 90) % 360
    for pad in children(moved.node, "pad"):
        at = child(pad, "at")
        assert len(at) > 3, "every pad must now carry the footprint's absolute angle"
    saved = bf.save()
    assert saved.changed and saved.snapshot
    again = BoardFile(path).find(ref)
    assert again.rotation == moved.rotation


def test_file_channel_places_on_the_back(project):
    """The file channel flips a footprint like KiCad does instead of leaving it on F.Cu with a warning."""
    path = project / "pic_programmer.kicad_pcb"
    bf = BoardFile(path)
    fp = bf.place_footprint("Resistor_SMD:R_0603_1608Metric", "R901", (100.0, 60.0), 90, value_text="10k", layer="B.Cu")
    assert fp.layer == "B.Cu" and fp.rotation == 90 and fp.at == (100.0, 60.0)
    bf.save()
    again = BoardFile(path).find("R901")
    assert again.layer == "B.Cu"
    pads = children(again.node, "pad")
    assert len(pads) == 2
    for pad in pads:
        assert {str(a) for a in child(pad, "layers")[1:]} == {"B.Cu", "B.Paste", "B.Mask"}
        assert str(child(pad, "at")[3]) == "90", "a pad with no angle of its own takes the footprint's"


def test_place_track_via_delete(project):
    path = project / "pic_programmer.kicad_pcb"
    bf = BoardFile(path)
    fp = bf.place_footprint("Resistor_SMD:R_0603_1608Metric", "R900", (100.0, 60.0), 0, value_text="10k")
    assert fp.lib_id == "Resistor_SMD:R_0603_1608Metric" and fp.at == (100.0, 60.0)
    assert child(fp.node, "path") is None, "a footprint without a symbol must not claim a schematic path"
    seg = bf.add_segment((100.0, 62.0), (104.0, 62.0), width=0.25, layer="F.Cu", net="GND")
    via = bf.add_via((104.0, 62.0), net="GND")
    bf.save()
    bf = BoardFile(path)
    assert bf.find("R900")
    assert bf.delete(seg) == "segment" and bf.delete(via) == "via"
    bf.save()
    with pytest.raises(LayerError):
        BoardFile(path).find_uuid(seg)


def test_lock_and_seen_live_refusals(project):
    from kicad_layer import pcb_tools

    path = project / "pic_programmer.kicad_pcb"
    rel = "pic_programmer/pic_programmer.kicad_pcb"
    get_session().seen_live.add(pcb_tools._canon(path) if hasattr(pcb_tools, "_canon") else __import__("os").path.normcase(__import__("os").path.realpath(str(path))))
    with pytest.raises(LayerError) as exc:
        pcb_tools.choose_channel(path, "file")
    assert exc.value.code == "EDIT_CONFLICT"
    get_session().seen_live.clear()
    lock = project / "~pic_programmer.kicad_pcb.lck"
    lock.write_text("{}")
    with pytest.raises(LayerError) as exc:
        pcb_tools.choose_channel(path, "file")
    assert exc.value.code == "EDIT_CONFLICT"
    lock.unlink()
    assert pcb_tools.choose_channel(path, "file") == ("file", path)


@real_kicad
@pytest.mark.anyio
async def test_file_channel_through_mcp_passes_drc(project):
    from mcp import Client

    from kicad_layer.server import build_server

    board = "pic_programmer/pic_programmer.kicad_pcb"
    # an empty spot: the grid point inside the outline farthest from every existing footprint
    bf = BoardFile(project / "pic_programmer.kicad_pcb")
    placed_at = [f.at for f in bf.footprints()]
    candidates = [(x, y) for x in range(80, 226, 5) for y in range(46, 135, 5)]
    fx, fy = max(candidates, key=lambda c: min(((c[0] - a) ** 2 + (c[1] - b) ** 2) ** 0.5 for a, b in placed_at))
    assert min(((fx - a) ** 2 + (fy - b) ** 2) ** 0.5 for a, b in placed_at) > 5
    async with Client(build_server(tier="full"), raise_exceptions=True) as c:
        placed = (await c.call_tool("pcb_place_footprint", {"board_path": board, "lib_id": "Resistor_SMD:R_0603_1608Metric", "ref": "R900", "x_mm": fx, "y_mm": fy, "channel": "file"})).structured_content
        assert placed["changed"] and placed["channel"] == "file" and placed["snapshot"]
        moved = (await c.call_tool("pcb_move_footprint", {"board_path": board, "ref": "R900", "x_mm": fx + 1.0, "y_mm": fy + 1.0, "rotation": 90, "channel": "file"})).structured_content
        assert moved["items"][0]["rotation_deg"] == 90
        drc = (await c.call_tool("run_drc", {"board_path": board, "schematic_parity": False})).structured_content
        assert drc["verdict"] in ("PASS", "WARN"), [f["description"] for f in drc["findings"] if f["severity"] == "error"][:5]
        gone = (await c.call_tool("pcb_delete_items", {"board_path": board, "ids": [placed["item_ids"][0]], "channel": "file"})).structured_content
        assert gone["deleted"][0]["kind"] == "footprint"


# --------------------------------------------------------------------------------------
# batch moves and zones inside footprints
# --------------------------------------------------------------------------------------

KEEPOUT = """		(zone
			(layers "F.Cu" "B.Cu")
			(uuid "5e1f0000-0000-4000-8000-00000000c0de")
			(name "own keepout")
			(hatch edge 0.5)
			(connect_pads
				(clearance 0)
			)
			(min_thickness 0.25)
			(filled_areas_thickness no)
			(keepout
				(tracks not_allowed)
				(vias not_allowed)
				(pads allowed)
				(copperpour allowed)
				(footprints allowed)
			)
			(placement
				(enabled no)
				(sheetname "")
			)
			(fill
				(thermal_gap 0.5)
				(thermal_bridge_width 0.5)
			)
			(polygon
				(pts
					(xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1})
				)
			)
		)
"""


def _with_footprint_keepout(path, ref):
    """Give footprint ``ref`` a keep-out zone 1 to 3 mm right of its origin, in board coordinates as KiCad stores it."""
    fp = BoardFile(path).find(ref)
    x, y = fp.at
    text = path.read_text(encoding="utf-8")
    start = text.index(f'"Reference" "{ref}"')
    end = text.index("\n\t)\n", start)
    zone = KEEPOUT.format(x0=x + 1, y0=y - 1, x1=x + 3, y1=y + 1)
    path.write_text(text[: end + 1] + zone.rstrip("\n") + text[end:], encoding="utf-8")
    return fp


def _zone_pts(fp_node):
    z = children(fp_node, "zone")[0]
    return [(float(xy[1]), float(xy[2])) for xy in children(child(child(z, "polygon"), "pts"), "xy")]


def test_single_move_carries_the_footprints_own_zone(project):
    path = project / "pic_programmer.kicad_pcb"
    ref = BoardFile(path).footprints()[0].reference
    fp = _with_footprint_keepout(path, ref)
    x, y = fp.at
    bf = BoardFile(path)
    rot = fp.rotation
    moved = bf.move_footprint(ref, (x + 10, y + 5), rot)
    assert _zone_pts(moved.node)[0] == pytest.approx((x + 11, y + 4)), "a pure translation shifts the zone by the same amount"
    bf.save()
    # a quarter turn about the footprint origin: local (1..3, -1..1) turns with the part
    bf = BoardFile(path)
    turned = bf.move_footprint(ref, None, (rot + 90) % 360)
    from kicad_layer.kicad_libs import rotate_about

    lx, ly = rotate_about(1, -1, -rot, 0, 0)  # the first corner (board offset 1, -1) in the footprint's frame
    want = rotate_about(lx, ly, (rot + 90) % 360, x + 10, y + 5)
    assert _zone_pts(turned.node)[0] == pytest.approx(want, abs=1e-3)


def test_batch_move_writes_once_and_reports_each_ref(project):
    from kicad_layer import pcb_tools

    path = project / "pic_programmer.kicad_pcb"
    fps = BoardFile(path).footprints()[:3]
    _with_footprint_keepout(path, fps[1].reference)
    snaps = project / ".kicad-layer" / "snapshots"
    before_snaps = len(list(snaps.glob("*"))) if snaps.exists() else 0
    moves = [{"ref": fps[0].reference, "x": fps[0].at[0] + 2, "y": fps[0].at[1], "rotation": None, "side": None},
             {"ref": fps[1].reference, "x": fps[1].at[0], "y": fps[1].at[1] + 3, "rotation": (fps[1].rotation + 90) % 360},
             {"ref": fps[2].reference}]
    res = pcb_tools.move_footprints(str(path), moves, channel="file")
    assert res.channel == "file" and res.snapshot and len(list(snaps.glob("*"))) == before_snaps + 1, "one write, one snapshot"
    status = {m["ref"]: m["status"] for m in res.extra["moves"]}
    assert status == {fps[0].reference: "moved", fps[1].reference: "moved", fps[2].reference: "unchanged"}
    bf = BoardFile(path)
    assert bf.find(fps[0].reference).at == (round(fps[0].at[0] + 2, 4), fps[0].at[1])
    second = bf.find(fps[1].reference)
    assert second.rotation == (fps[1].rotation + 90) % 360
    assert all(len(child(p, "at")) > 3 for p in children(second.node, "pad")), "pads carry the absolute angle"
    assert _zone_pts(second.node)[0] != pytest.approx((fps[1].at[0] + 1, fps[1].at[1] - 1)), "the embedded zone moved"


def test_batch_move_is_all_or_nothing(project):
    from kicad_layer import pcb_tools

    path = project / "pic_programmer.kicad_pcb"
    sha = path.read_bytes()
    ref = BoardFile(path).footprints()[0].reference
    with pytest.raises(LayerError) as exc:
        pcb_tools.move_footprints(str(path), [{"ref": ref, "x": 1.0, "y": 1.0}, {"ref": "NOPE99", "x": 1.0, "y": 1.0}], channel="file")
    assert exc.value.code == "NOT_FOUND_IN_DESIGN" and path.read_bytes() == sha
    with pytest.raises(LayerError) as exc:
        pcb_tools.move_footprints(str(path), [{"ref": ref, "x": 1.0}], channel="file")
    assert exc.value.code == "INVALID_ARGUMENT"
    with pytest.raises(LayerError) as exc:
        pcb_tools.move_footprints(str(path), [{"ref": ref, "side": "B.Cu"}], channel="file")
    assert exc.value.code == "INVALID_ARGUMENT" and path.read_bytes() == sha
    dry = pcb_tools.move_footprints(str(path), [{"ref": ref, "x": 1.0, "y": 1.0}], channel="file", dry_run=True)
    assert dry.dry_run and path.read_bytes() == sha


class _FakeBoard:
    """Just enough of kipy's Board for a batch move: counts commits."""

    def __init__(self, fps):
        self.fps, self.commits, self.pushed, self.flipped = fps, 0, [], []

    def get_footprints(self):
        return self.fps

    def begin_commit(self):
        self.commits += 1
        return object()

    def push_commit(self, commit, message):
        self.pushed.append(message)

    def drop_commit(self, commit):
        pass

    def update_items(self, items):
        return list(items)

    def flip_items(self, items):
        from kipy.board_types import BoardLayer

        for f in items:
            f.layer = BoardLayer.BL_B_Cu
        self.flipped += list(items)


def test_live_batch_move_is_one_commit(monkeypatch):
    from kipy.board_types import BoardLayer, FootprintInstance
    from kipy.geometry import Vector2

    from kicad_layer.ipc import board_write

    fps = []
    for i, ref in enumerate(("R1", "R2")):
        f = FootprintInstance()
        f.reference_field.text.value = ref
        f.position = Vector2.from_xy(i * 1_000_000, 0)
        f.layer = BoardLayer.BL_F_Cu
        fps.append(f)
    fake = _FakeBoard(fps)

    class _Session:
        def board(self, path):
            return fake, path

        def call(self, fn, **kw):
            return fn()

    monkeypatch.setattr(board_write, "get_session", lambda: _Session())
    out = board_write.move_footprints(None, [("R1", (5.0, 6.0), 90.0, None), ("R2", None, None, "B.Cu")])
    assert fake.commits == 1 and len(fake.pushed) == 1 and fake.flipped == [fps[1]]
    rows = {r["ref"]: r for r in out["items"]}
    assert (rows["R1"]["x_mm"], rows["R1"]["y_mm"], rows["R1"]["rotation_deg"]) == (5.0, 6.0, 90.0)
    assert rows["R2"]["layer"] == "B.Cu" and [m["status"] for m in out["moves"]] == ["moved", "moved"]
    with pytest.raises(LayerError):
        board_write.move_footprints(None, [("R9", (1.0, 1.0), None, None)])
    assert fake.commits == 1, "an unknown ref opens no commit"


# --------------------------------------------------------------------------------------
# outline and mounting holes
# --------------------------------------------------------------------------------------


def _edge_items(path):
    from kicad_layer.sexpr import parse, tag, value

    root = parse(path.read_text(encoding="utf-8"))
    return [n for n in root if isinstance(n, list) and tag(n) in ("gr_line", "gr_arc", "gr_rect", "gr_poly", "gr_circle") and value(n, "layer") == "Edge.Cuts"]


def test_rounded_rect_outline_is_one_closed_chain(project):
    import math

    from kicad_layer import pcb_tools
    from kicad_layer.review import load_board
    from kicad_layer.routers.dsn import outline_loops

    path = project / "pic_programmer.kicad_pcb"
    res = pcb_tools.set_outline(str(path), rect=[80, 50, 180, 120], corner_radius_mm=2)
    assert res.extra["removed"], "the old Edge.Cuts lines are gone"
    items = _edge_items(path)
    lines = [n for n in items if n[0] == "gr_line"]
    arcs = [n for n in items if n[0] == "gr_arc"]
    assert len(lines) == 4 and len(arcs) == 4 and len(items) == 8
    pt = lambda n, k: (float(child(n, k)[1]), float(child(n, k)[2]))
    ends = [(pt(n, "start"), pt(n, "end")) for n in items]
    for a in [p for e in ends for p in e]:
        assert sum(1 for e in ends for p in e if math.dist(a, p) < 1e-6) == 2, f"{a} is the end of exactly two pieces"
    for arc in arcs:
        s, m, e = pt(arc, "start"), pt(arc, "mid"), pt(arc, "end")
        # a tangent fillet: radius 2, centre 2 mm inside the nearest corner of the rectangle
        (x1, y1), (x2, y2), (x3, y3) = s, m, e
        d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
        cx = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1) + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
        cy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3) + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
        assert math.dist((cx, cy), s) == pytest.approx(2, abs=1e-4)
        corner = min(((80, 50), (180, 50), (180, 120), (80, 120)), key=lambda c: math.dist(c, m))
        assert (abs(cx - corner[0]), abs(cy - corner[1])) == pytest.approx((2, 2), abs=1e-4)
        assert str(child(arc, "layer")[1]) == "Edge.Cuts" and child(arc, "uuid") is not None
    loops = outline_loops(path)
    assert len(loops) == 1 and math.dist(loops[0][0], loops[0][-1]) < 1e-3
    assert load_board(path).outline == pytest.approx((80, 50, 180, 120))


def test_outline_polygon_and_radius_checks(project):
    from kicad_layer import pcb_tools
    from kicad_layer.pcb_edit import outline_pieces

    path = project / "pic_programmer.kicad_pcb"
    pcb_tools.set_outline(str(path), polygon=[[0, 0], [50, 0], [50, 30], [20, 30], [0, 10]])
    assert len(_edge_items(path)) == 5
    tri = outline_pieces([(0, 0), (10, 0), (0, 10)], 1.0)
    assert [p[0] for p in tri].count("arc") == 3 and [p[0] for p in tri].count("line") == 3
    with pytest.raises(LayerError) as exc:
        pcb_tools.set_outline(str(path), rect=[0, 0, 10, 3], corner_radius_mm=2)
    assert exc.value.code == "INVALID_ARGUMENT"
    with pytest.raises(LayerError):
        pcb_tools.set_outline(str(path), rect=[0, 0, 10, 10], polygon=[[0, 0], [1, 0], [0, 1]])


def test_mounting_holes_are_board_only_footprints(project):
    from kicad_layer import pcb_tools
    from kicad_layer.review import load_board

    path = project / "pic_programmer.kicad_pcb"
    res = pcb_tools.add_mounting_holes(str(path), [{"x": 85, "y": 55, "drill": 3.2, "pad": 6, "net": "GND"}, {"x": 175, "y": 55, "drill": 3.2, "ref": "MH2"}])
    assert [i["ref"] for i in res.items] == ["H1", "MH2"]
    bf = BoardFile(path)
    plated, bare = bf.find("H1"), bf.find("MH2")
    assert [str(a) for a in child(plated.node, "attr")[1:]] == ["board_only", "exclude_from_pos_files", "exclude_from_bom"]
    pad = children(plated.node, "pad")[0]
    assert (str(pad[1]), str(pad[2])) == ("1", "thru_hole") and str(child(pad, "net")[1]) == "GND" and float(child(pad, "drill")[1]) == 3.2
    assert str(children(bare.node, "pad")[0][2]) == "np_thru_hole"
    geo = {f.ref: f for f in load_board(path).footprints}
    assert geo["H1"].pads[0].net == "GND" and geo["H1"].pads[0].size == (6.0, 6.0) and (geo["H1"].x, geo["H1"].y) == (85.0, 55.0)
    assert geo["MH2"].pads[0].kind == "np_thru_hole" and geo["MH2"].pads[0].drill == 3.2
    with pytest.raises(LayerError) as exc:
        pcb_tools.add_mounting_holes(str(path), [{"x": 1, "y": 1, "drill": 3, "ref": "MH2"}])
    assert exc.value.code == "EDIT_CONFLICT"
    with pytest.raises(LayerError):
        pcb_tools.add_mounting_holes(str(path), [{"x": 1, "y": 1, "drill": 3, "pad": 2}])
