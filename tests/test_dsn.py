"""Specctra export and session import, and routes as data."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_layer import routes as routes_mod
from kicad_layer.routers import dsn, ses
from kicad_layer.config import load_settings, set_settings
from kicad_layer.pcb_writer import BoardBuilder
from kicad_layer.sexpr import child, children, parse


@pytest.fixture(autouse=True)
def workspace(tmp_path):
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    try:
        yield
    finally:
        set_settings(None)


def _board(tmp_path: Path) -> Path:
    pcb = BoardBuilder(sheetfile="t.kicad_sch", title="t", copper_layers=4)
    pcb.rounded_rect_outline(0, 0, 40, 30, 2)
    pcb.rounded_rect_outline(10, 20, 20, 23, 1.0)  # a slot
    pcb.zone(net="GND", layer="In1.Cu", polygon=[(0.5, 0.5), (39.5, 0.5), (39.5, 29.5), (0.5, 29.5)], name="GND plane")
    pcb.track([(5, 5), (15, 5)], width=0.2, layer="F.Cu", net="SIG_P")
    pcb.via((15, 5), net="SIG_P", size=0.6, drill=0.3)
    path = tmp_path / "t.kicad_pcb"
    pcb.write(str(path))
    return path


def test_outline_loops_and_export(tmp_path):
    board = _board(tmp_path)
    loops = dsn.outline_loops(board)
    assert len(loops) == 2 and len(loops[0]) > 40  # the main outline first, arcs sampled
    xs = [p[0] for p in loops[1]]
    assert min(xs) == pytest.approx(10) and max(xs) == pytest.approx(20)
    text = dsn.export_dsn(board)
    root = parse(text.replace('    (string_quote ")\n', ""))
    st = child(root, "structure")
    layers = [(str(l[1]), str(child(l, "type")[1])) for l in children(st, "layer")]
    assert layers == [("F.Cu", "signal"), ("In1.Cu", "power"), ("In2.Cu", "signal"), ("B.Cu", "signal")]
    assert len(children(st, "keepout")) == 1, "the slot becomes a keepout"
    plane = children(st, "plane")[0]
    assert str(plane[1]) == "GND" and str(child(plane, "polygon")[1]) == "In1.Cu"
    b = child(child(st, "boundary"), "path")
    # micrometres, Y negated: the outline's top-left corner region
    ys = [float(b[i]) for i in range(4, len(b), 2)]
    assert min(ys) == pytest.approx(-30000) and max(ys) == pytest.approx(0)
    wiring = child(root, "wiring")
    wires = children(wiring, "wire")
    vias = children(wiring, "via")
    assert len(wires) == 1 and len(vias) == 1
    assert " ".join(str(a) for a in child(wires[0], "path")) == "path F.Cu 200 5000 -5000 15000 -5000"
    assert str(vias[0][1]) == "Via[0-3]_600:300_um" and str(child(vias[0], "type")[1]) == "protect"
    assert dsn.parse_via_name("Via[0-3]_600:300_um") == (0.6, 0.3)


SES = '''(session "t.ses"
  (base_design "t.dsn")
  (routes
    (resolution um 10)
    (library_out
      (padstack "Via[0-3]_600:300_um" (shape (circle F.Cu 6000 0 0)) (attach off))
    )
    (network_out
      (net "SIG_P"
        (wire (path F.Cu 1500 50000 -50000 150000 -50000 150000 -80000) (type route))
        (via "Via[0-3]_600:300_um" 150000 -80000)
        (wire (path B.Cu 1500 150000 -80000 200000 -80000) (type route))
      )
      (net "/S/OTHER"
        (wire (path F.Cu 2000 10000 -10000 10000 -10000) (type route))
      )
    )
  )
)
'''


def test_session_import_and_routes_roundtrip(tmp_path):
    p = tmp_path / "t.ses"
    p.write_text(SES, encoding="utf-8")
    r = ses.parse_ses(p)
    assert r.nets == {"SIG_P", "/S/OTHER"}
    segs = [s for s in r.segments if s.net == "SIG_P"]
    assert len(segs) == 3 and segs[0].layer == "F.Cu" and segs[0].width == 0.15
    assert (segs[0].x1, segs[0].y1, segs[0].x2, segs[0].y2) == (5.0, 5.0, 15.0, 5.0)
    assert (segs[1].x2, segs[1].y2) == (15.0, 8.0) and segs[2].layer == "B.Cu"
    assert [s for s in r.segments if s.net == "/S/OTHER"] == []  # a zero-length path adds nothing
    assert r.vias[0].net == "SIG_P" and (r.vias[0].x, r.vias[0].y, r.vias[0].size, r.vias[0].drill) == (15.0, 8.0, 0.6, 0.3)
    saved = tmp_path / "routes.json"
    routes_mod.save(r, saved)
    back = routes_mod.load(saved)
    assert len(back.segments) == 3 and len(back.vias) == 1 and back.nets == {"SIG_P"}
    merged = routes_mod.merge(back, ses.Routes(segments=[ses.RouteSegment("SIG_P", "F.Cu", 0.2, 0, 0, 1, 0)], vias=[], nets={"SIG_P"}))
    assert len(merged.segments) == 1, "replacing a net drops its old routes"
    pcb = BoardBuilder(sheetfile="t.kicad_sch", title="t", copper_layers=4)
    pcb.rounded_rect_outline(0, 0, 40, 30, 2)
    assert routes_mod.apply(pcb, back) == (3, 1)
    stale = routes_mod.stale(back, {"SIG_P": [(5.0, 5.0)]})
    assert stale == []
    assert routes_mod.stale(back, {"SIG_P": [(30.0, 30.0)]}) == ["SIG_P"]
