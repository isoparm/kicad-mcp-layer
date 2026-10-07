"""The board question and clean-up tools: copper queries, zone islands, parity, plot, track widths, footprint
swaps, reference placement, footprints from numbers, one-net routing, and the autoroute and DRC fixes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_layer import board_fix, board_query, fpgen, netroute
from kicad_layer.config import load_settings, set_settings
from kicad_layer.pcb_edit import BoardFile
from kicad_layer.review import load_board
from tests.synth_board import Board, project


@pytest.fixture
def ws(tmp_path):
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache"), "KICAD_LAYER_MODE": "write"}))
    yield tmp_path
    set_settings(None)


def _two_pads(gap_net: str | None = "X"):
    b = Board(40, 30)
    b.footprint("R1", 8, 15, [("1", "A", 0, 0, 1.0, 1.2), ("2", "B", 2, 0, 1.0, 1.2)])
    b.footprint("R2", 32, 15, [("1", "A", 0, 0, 1.0, 1.2), ("2", "C", 2, 0, 1.0, 1.2)])
    if gap_net:
        b.track(gap_net, "F.Cu", 0.5, (20, 5), (20, 25))  # a wall on F.Cu between the two A pads
    return b


# ---------------------------------------------------------------- copper questions
def test_copper_query_clear_and_region(ws):
    board = project(ws, _two_pads())
    q = board_query.copper_query(board, "clear", net="A", layer="F.Cu", points=[[8, 15], [32, 15]])
    assert not q.ok and any("track X" in ln for ln in q.lines)
    q = board_query.copper_query(board, "clear", net="A", layer="B.Cu", points=[[8, 15], [32, 15]])
    assert q.ok, q.lines
    q = board_query.copper_query(board, "region", rect=[19, 14, 21, 16])
    assert q.ok and any("track" in ln and "X" in ln for ln in q.lines)
    assert "t.kicad_pro" in q.rules


# ---------------------------------------------------------------- islands
def test_zone_islands_names_the_detached_pads(ws):
    b = Board(40, 30)
    b.footprint("J1", 5, 5, [("1", "GND", 0, 0, 1.7, 1.7, "thru_hole", "circle", 1.0)])
    b.footprint("J2", 35, 25, [("1", "GND", 0, 0, 1.7, 1.7, "thru_hole", "circle", 1.0)])
    outline = [(1, 1), (39, 1), (39, 29), (1, 29)]
    b.zone("GND", "B.Cu", outline, [[(1, 1), (15, 1), (15, 29), (1, 29)], [(25, 1), (39, 1), (39, 29), (25, 29)]])
    rep = board_query.zone_islands(project(ws, b))
    (gnd,) = rep.nets
    assert gnd.groups == 2 and gnd.detached and gnd.detached[0].pads in (["J1.1"], ["J2.1"])
    b.track("GND", "F.Cu", 0.5, (5, 5), (35, 25))
    rep = board_query.zone_islands(project(ws, b))
    assert rep.nets[0].groups == 1


# ---------------------------------------------------------------- parity
def test_parity_names_the_pin_without_a_pad(ws, monkeypatch):
    b = Board().footprint("J7", 20, 20, [("A1", "GND", 0, 0, 0.6, 1.0), ("SH", None, 3, 0, 1, 2, "thru_hole", "oval", 0.6)])
    board = project(ws, b)
    nl = SimpleNamespace(nets=[SimpleNamespace(name="GND", nodes=[SimpleNamespace(ref="J7", pin="A1"), SimpleNamespace(ref="J7", pin="S1")])],
                         components=[SimpleNamespace(ref="J7", footprint="Test:FP", pins=[])])
    from kicad_layer.cli import netlist as netlist_mod

    monkeypatch.setattr(netlist_mod, "load_netlist", lambda *a, **k: nl)
    rep = board_query.parity(board, ws / "t.kicad_sch")
    kinds = {(i.kind, i.pin) for i in rep.issues}
    assert ("pin_without_pad", "S1") in kinds and ("pad_without_pin", "SH") in kinds
    assert "SH" in next(i.hint for i in rep.issues if i.pin == "S1")


def test_parity_reads_the_netlist_pin_list_as_strings(ws, monkeypatch):
    """Component.pins is a list of pin numbers; a clean part gives no issue (the Human Kinetik run gave 146)."""
    b = Board().footprint("R1", 20, 20, [("1", "A", 0, 0, 0.6, 1.0), ("2", "B", 2, 0, 0.6, 1.0)]).footprint("R2", 30, 20, [("1", "A", 0, 0, 0.6, 1.0), ("2", None, 2, 0, 0.6, 1.0)])
    board = project(ws, b)
    nl = SimpleNamespace(nets=[SimpleNamespace(name="A", nodes=[SimpleNamespace(ref="R1", pin="1"), SimpleNamespace(ref="R2", pin="1")]),
                               SimpleNamespace(name="B", nodes=[SimpleNamespace(ref="R1", pin="2")])],
                         components=[SimpleNamespace(ref="R1", footprint="Test:FP", pins=["1", "2"]), SimpleNamespace(ref="R2", footprint="Test:FP", pins=["1", "2"])])
    from kicad_layer.cli import netlist as netlist_mod

    monkeypatch.setattr(netlist_mod, "load_netlist", lambda *a, **k: nl)
    rep = board_query.parity(board, ws / "t.kicad_sch")
    assert rep.issues == [], rep.issues


# ---------------------------------------------------------------- plot
def test_plot_writes_a_png(ws):
    board = project(ws, _two_pads())
    png = board_query.plot(board, ws / "p.png", rect=[0, 0, 40, 30], nets=["A"])
    from PIL import Image

    assert Image.open(png).size == (800, 600)


# ---------------------------------------------------------------- track widths
def test_set_track_width_widens_where_room(ws):
    b = Board(40, 30).track("VIN", "F.Cu", 0.2, (5, 10), (35, 10)).track("VIN", "F.Cu", 0.2, (5, 20), (35, 20)).track("Z", "F.Cu", 0.25, (5, 20.5), (35, 20.5))
    board = project(ws, b)
    bf = BoardFile(board)
    data = board_fix.set_track_width(bf, nets=["VIN"], pro=ws / "t.kicad_pro")  # Power class: 0.5 mm
    assert data["changed"] == 1 and len(data["blocked"]) == 1 and "track Z" in data["blocked"][0]["blocked_by"]
    bf.save()
    widths = sorted(s.width for s in load_board(board).segments if s.net == "VIN")
    assert widths == [0.2, 0.5]


# ---------------------------------------------------------------- reference placement
def test_silk_tidy_moves_references_off_pads(ws):
    b = Board(40, 30)
    for i in range(4):
        b.footprint(f"R{i + 1}", 10 + 6 * i, 15, [("1", "A", -0.95, 0, 1.0, 1.4), ("2", "B", 0.95, 0, 1.0, 1.4)])
    board = project(ws, b)
    bf = BoardFile(board)
    data = board_fix.tidy_silkscreen(bf)
    assert data["placed"] == 4 and not data["failed"]
    bf.save()
    text = board.read_text(encoding="utf-8")
    assert text.count("(size 1 1)") >= 4 and "(thickness 0.15)" in text


# ---------------------------------------------------------------- footprints from numbers
def test_fp_create_writes_library_and_table(ws):
    pads = [{"number": "1", "kind": "smd", "x": -1.25, "y": -2.66, "w": 1.05, "h": 1.875}, {"number": "4", "kind": "smd", "x": 1.25, "y": -2.66, "w": 1.05, "h": 1.875},
            {"number": "2", "kind": "smd", "x": -1.25, "y": 2.66, "w": 1.05, "h": 1.875}, {"number": "3", "kind": "smd", "x": 1.25, "y": 2.66, "w": 1.05, "h": 1.875}]
    res = fpgen.create_footprint(ws, "HK", "MB320F_MBF", pads, body=[4.0, 5.0], description="bridge")
    mod = ws / "HK.pretty" / "MB320F_MBF.kicad_mod"
    assert mod.is_file() and res.pads == 4 and res.lib_id == "HK:MB320F_MBF"
    text = mod.read_text(encoding="utf-8")
    assert "(attr smd)" in text and text.count("(pad ") == 4 and "F.CrtYd" in text
    assert '(name "HK")' in (ws / "fp-lib-table").read_text(encoding="utf-8")
    assert res.courtyard_mm == [-2.25, -3.85, 2.25, 3.85]
    from kicad_layer.errors import LayerError

    with pytest.raises(LayerError):
        fpgen.create_footprint(ws, "HK", "MB320F_MBF", pads)  # exists
    with pytest.raises(LayerError):
        fpgen.create_footprint(ws, "HK", "bad", [{"number": "1", "kind": "thru_hole", "x": 0, "y": 0, "w": 1.0}])  # a hole needs a drill


# ---------------------------------------------------------------- one-net routing
def test_route_net_goes_around_and_crosses_short(ws):
    from kicad_layer.design import copper

    board = project(ws, _two_pads())
    m = copper.load(board, ws / "t.kicad_pro")
    a, b = netroute.endpoint(m, "R1.1", "A"), netroute.endpoint(m, "R2.1", "A")
    pts, n = netroute.route_connection(m, "A", a, b, layers=("F.Cu",), margin=12)
    assert pts is not None
    pts = netroute.simplify(m, "A", pts, 0.25)
    r = netroute.to_routes("A", pts, 0.25, 0.6, 0.3)
    assert not r.vias and all(s.layer == "F.Cu" for s in r.segments)
    # with both layers it may use B.Cu under the X wall; keep_under makes that crossing short
    pts2, _ = netroute.route_connection(m, "A", a, b, keep_under=["X"], layer_cost={"B.Cu": 1.0})
    r2 = netroute.to_routes("A", netroute.simplify(m, "A", pts2, 0.25), 0.25, 0.6, 0.3)
    under = sum(1 for s in r2.segments if s.layer == "B.Cu")
    assert under <= 1


# ---------------------------------------------------------------- autoroute fixes
def test_autoroute_drops_existing_copper_and_widens_necks(ws):
    from kicad_layer.routers import routing_tools
    from kicad_layer.routes import RouteSegment, Routes

    b = Board(40, 30).track("VIN", "F.Cu", 0.5, (5, 5), (10, 5))
    board = project(ws, b)
    r = Routes(segments=[RouteSegment("VIN", "F.Cu", 0.5, 5, 5, 10, 5), RouteSegment("VIN", "F.Cu", 0.2, 10, 5, 20, 5)], nets={"VIN"})
    r2, dropped = routing_tools.drop_existing(r, board)
    assert dropped == 1 and len(r2.segments) == 1
    widened, narrow = routing_tools.fix_necks(r2, board, ws / "t.kicad_pro")
    assert widened == 1 and not narrow and r2.segments[0].width == 0.5


# ---------------------------------------------------------------- DRC report filters and output paths
def test_drc_types_and_offset(ws):
    from kicad_layer.cli import reports, runner

    data = json.loads((Path(__file__).parent / "data" / "drc-multichannel.json").read_text(encoding="utf-8"))
    rp = ws / "r.json"
    rp.write_text(json.dumps(data), encoding="utf-8")
    res = SimpleNamespace(returncode=runner.EXIT_VIOLATIONS, command=["kicad-cli"], duration_s=1.0, tail=lambda: "")
    full = reports._build("drc", ws / "b.kicad_pcb", res, rp, reports.parse_drc)
    kinds = sorted({f.type for f in full.findings})
    one = reports._build("drc", ws / "b.kicad_pcb", res, rp, reports.parse_drc, types=[kinds[0]])
    assert one.findings and all(f.type == kinds[0] for f in one.findings) and one.counts == full.counts
    paged = reports._build("drc", ws / "b.kicad_pcb", res, rp, reports.parse_drc, offset=2)
    assert paged.findings[0].id == full.findings[2].id


def test_relative_output_paths_are_not_doubled(ws):
    from kicad_layer.cli.exports import relative_target

    (ws / "Proj" / "board").mkdir(parents=True)
    base = ws / "Proj" / "board"
    assert relative_target(base, "Proj/board/renders/x.png") == ws / "Proj" / "board" / "renders" / "x.png"
    assert relative_target(base, "renders/x.png") == base / "renders" / "x.png"


def _have_lib(lib: str, name: str) -> bool:
    try:
        from kicad_layer.kicad_libs import load_footprint

        load_footprint(lib, name)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _have_lib("Resistor_SMD", "R_0805_2012Metric"), reason="KiCad footprint libraries not installed")
def test_swap_footprint_keeps_nets_and_link(ws):
    b = Board().footprint("R1", 20, 20, [("1", "A", -0.95, 0, 1.0, 1.4), ("2", "B", 0.95, 0, 1.0, 1.4)], extra='\t\t(path "/abc/def")\n\t\t(sheetfile "s.kicad_sch")')
    board = project(ws, b)
    bf = BoardFile(board)
    data = board_fix.swap_footprint(bf, "R1", "Resistor_SMD:R_1206_3216Metric") if _have_lib("Resistor_SMD", "R_1206_3216Metric") else board_fix.swap_footprint(bf, "R1", "Resistor_SMD:R_0805_2012Metric")
    bf.save()
    bm = load_board(board)
    (fp,) = bm.footprints
    assert fp.lib_id.startswith("Resistor_SMD:") and {p.number: p.net for p in fp.pads} == {"1": "A", "2": "B"}
    assert '(path "/abc/def")' in board.read_text(encoding="utf-8") and not data["lost_pads"]


@pytest.mark.skipif(not _have_lib("MountingHole", "MountingHole_3.2mm_M3"), reason="KiCad footprint libraries not installed")
def test_stock_mounting_hole_comes_from_the_library(ws):
    board = project(ws, Board())
    bf = BoardFile(board)
    fp = bf.add_mounting_hole((5, 5), drill=3.2)
    odd = bf.add_mounting_hole((45, 5), drill=3.3)
    assert fp.lib_id == "MountingHole:MountingHole_3.2mm_M3" and odd.lib_id == "MountingHole_3.3mm"


def test_route_net_puts_no_via_in_a_pad(ws):
    """A layer change starts beside the pad, not under it (the review flags vias in SMD pads, same net or not)."""
    b = Board().footprint("TP1", 10, 20, [("1", "S", 0, 0, 1.0, 1.0, "smd", "circle")]).footprint("R1", 30, 20, [("1", "S", 0, 0, 1.0, 1.4), ("2", None, 0, 2.5, 1.0, 1.4)])
    b.track("X", "F.Cu", 0.25, (20, 1), (20, 39))  # a wall across the top: the route must change layer
    board = project(ws, b)
    from kicad_layer.design import copper
    from kicad_layer import netroute as NR

    m = copper.load(board, ws / "t.kicad_pro")
    a, z = NR.endpoint(m, "TP1.1", "S"), NR.endpoint(m, "R1.1", "S")
    pts, _ = NR.route_connection(m, "S", a, z, width=0.25, margin=12)
    assert pts and any(p[2] == "B.Cu" for p in pts)
    vias = [(p[0], p[1]) for p, q in zip(pts, pts[1:]) if p[2] != q[2]]
    pads = [(10, 20, 0.5, 0.5), (30, 20, 0.5, 0.7)]
    for vx, vy in vias:
        for px, py, hw, hh in pads:
            assert abs(vx - px) >= hw + 0.3 - 1e-6 or abs(vy - py) >= hh + 0.3 - 1e-6, (vx, vy)


def test_silk_tidy_of_some_references_avoids_the_others(ws):
    """With refs given, a reference left in place is an obstacle (Human Kinetik: TP11 was put on JP801's reference)."""
    b = Board(40, 30)
    b.footprint("R1", 10, 15, [("1", "A", -0.95, 0, 1.0, 1.4), ("2", "B", 0.95, 0, 1.0, 1.4)])
    b.footprint("R2", 30, 15, [("1", "A", -0.95, 0, 1.0, 1.4), ("2", "B", 0.95, 0, 1.0, 1.4)])
    board = project(ws, b)
    t = board.read_text(encoding="utf-8")
    i = t.index('(property "Reference" "R2" (at 0 -2 0)')
    board.write_text(t[:i] + '(property "Reference" "R2" (at -17.22 -0.125 0)' + t[i + len('(property "Reference" "R2" (at 0 -2 0)'):], encoding="utf-8")
    bf = BoardFile(board)
    data = board_fix.tidy_silkscreen(bf, refs=["R1"])
    (m,) = data["items"]
    r2_box = (12.78, 14.875, 2 * 0.92 + 0.2, 1.15 + 0.2)  # R2's reference sits where R1's would go alone
    w, h = (2 * 0.92 + 0.2, 1.35) if m["rotation_deg"] == 0 else (1.35, 2 * 0.92 + 0.2)
    assert not (abs(m["x_mm"] - r2_box[0]) < (w + r2_box[2]) / 2 and abs(m["y_mm"] - r2_box[1]) < (h + r2_box[3]) / 2), m


def test_swap_footprint_keeps_attributes_and_reference_place(ws):
    """A board-only mounting hole stays board-only (else DRC parity calls it an extra footprint) and its text stays put."""
    lib = ("Resistor_SMD", "R_0805_2012Metric")
    if not _have_lib(*lib):
        pytest.skip("KiCad footprint libraries not installed")
    b = Board().footprint("R1", 20, 20, [("1", "A", -0.95, 0, 1.0, 1.4), ("2", "B", 0.95, 0, 1.0, 1.4)], extra="\t\t(attr smd board_only exclude_from_bom)")
    board = project(ws, b)
    t = board.read_text(encoding="utf-8").replace('(property "Reference" "R1" (at 0 -2 0)', '(property "Reference" "R1" (at 1.5 -3.25 0)')
    board.write_text(t, encoding="utf-8")
    bf = BoardFile(board)
    board_fix.swap_footprint(bf, "R1", ":".join(lib))
    bf.save()
    text = board.read_text(encoding="utf-8")
    assert "board_only" in text and "exclude_from_bom" in text
    assert '(property "Reference" "R1" (at 1.5 -3.25 0)' in text


def test_short_value_keeps_what_a_board_needs():
    sv = board_fix.short_value
    assert sv("100nF (VDD)") == "100nF" and sv("10uF 100V X7R") == "10uF 100V" and sv("10k 1%") == "10k"
    assert sv("Ferrita 600R@100MHz") == "600R" and sv("100k pull-down") == "100k" and sv("Supercap 2.7V D16 (C por definir)") == ""


def test_silk_tidy_prints_values_for_passives(ws):
    """values_for: the value goes on the silkscreen as a user text, the reference to F.Fab, the Value field stays."""
    b = Board(40, 30)
    b.footprint("R1", 10, 15, [("1", "A", -0.95, 0, 1.0, 1.4), ("2", "B", 0.95, 0, 1.0, 1.4)])
    b.footprint("U1", 30, 15, [("1", "A", -0.95, 0, 1.0, 1.4), ("2", "B", 0.95, 0, 1.0, 1.4)])
    board = project(ws, b)
    board.write_text(board.read_text(encoding="utf-8").replace('(property "Value" "v"', '(property "Value" "10k 1% (pull-up)"', 1), encoding="utf-8")
    bf = BoardFile(board)
    data = board_fix.tidy_silkscreen(bf, size=0.7, thickness=0.12, min_size=0.7, values_for=["R*", "C*"])
    bf.save()
    texts = {m["ref"]: m["text"] for m in data["items"]}
    assert texts == {"R1": "10k", "U1": "U1"}
    t = board.read_text(encoding="utf-8")
    assert '(fp_text user "10k"' in t and '(property "Value" "10k 1% (pull-up)"' in t
    i = t.index('(property "Reference" "R1"')
    assert '"F.Fab"' in t[i:i + 120]
    # a second run reuses the label instead of adding another
    bf = BoardFile(board)
    board_fix.tidy_silkscreen(bf, size=0.7, thickness=0.12, min_size=0.7, values_for=["R*"])
    bf.save()
    assert board.read_text(encoding="utf-8").count('(fp_text user "10k"') == 1


def test_silk_tidy_leaves_a_value_with_no_room_as_it_was(ws):
    """No place for the value: no label on the pads, the reference stays on the silkscreen."""
    b = Board(4, 3)  # a board too small for any text beside the part
    b.footprint("R1", 2, 1.5, [("1", "A", -0.95, 0, 1.0, 1.4), ("2", "B", 0.95, 0, 1.0, 1.4)])
    board = project(ws, b)
    board.write_text(board.read_text(encoding="utf-8").replace('(property "Value" "v"', '(property "Value" "10k"', 1), encoding="utf-8")
    bf = BoardFile(board)
    data = board_fix.tidy_silkscreen(bf, size=1.0, min_size=1.0, reach=0.5, values_for=["R*"])
    bf.save()
    t = board.read_text(encoding="utf-8")
    assert data["failed"] == ["R1"] and "fp_text user" not in t
    i = t.index('(property "Reference" "R1"')
    assert '"F.SilkS"' in t[i:i + 120]


def test_silk_tidy_keeps_texts_out_of_other_parts_courtyards(ws):
    """A label under a neighbour's body cannot be read once the board is assembled."""
    b = Board(40, 30)
    b.footprint("R1", 10, 15, [("1", "A", -0.95, 0, 1.0, 1.4), ("2", "B", 0.95, 0, 1.0, 1.4)])
    # a big part to the right with a courtyard and no silkscreen or pads where R1's text would go alone
    b.footprint("U1", 17, 15, [("1", "C", 3, 3, 0.6, 0.6)], court=(-5.0, -4.0, 5.0, 4.0))
    board = project(ws, b)
    bf = BoardFile(board)
    data = board_fix.tidy_silkscreen(bf, refs=["R1"])
    (m,) = [i for i in data["items"] if i["ref"] == "R1"]
    w, h = (2 * 0.92 + 0.2, 1.35) if m["rotation_deg"] == 0 else (1.35, 2 * 0.92 + 0.2)
    assert m["x_mm"] + w / 2 <= 12.0 + 1e-6 or not (abs(m["y_mm"] - 15) < 4 + h / 2), m


def test_silk_tidy_tool_prints_passive_values_by_default(ws):
    """The tool's default: R, C, L and FB show their value; [] keeps references everywhere."""
    from kicad_layer import pcb_tools

    b = Board(40, 30)
    b.footprint("R1", 10, 15, [("1", "A", -0.95, 0, 1.0, 1.4), ("2", "B", 0.95, 0, 1.0, 1.4)])
    board = project(ws, b)
    board.write_text(board.read_text(encoding="utf-8").replace('(property "Value" "v"', '(property "Value" "4.7k"', 1), encoding="utf-8")
    res = pcb_tools.silk_tidy(str(board), dry_run=True)
    assert [i["text"] for i in res.items] == ["4.7k"]
    res = pcb_tools.silk_tidy(str(board), values_for=[], dry_run=True)
    assert [i["text"] for i in res.items] == ["R1"]
