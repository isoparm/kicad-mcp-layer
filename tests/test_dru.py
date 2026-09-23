"""The .kicad_dru questions in the core (kicad_layer.dru) and the copper model's use of them."""
from __future__ import annotations

import json
from pathlib import Path

from kicad_layer import dru
from kicad_layer.design import copper
from kicad_layer.routers import dru as routers_dru

HIZ = dru.Facts(net="/Notch/TT1K_M1", netclass="HiZ", type="Via")
OUT = dru.Facts(net="/Notch/TT1K_OUT", netclass="HiZ", type="Via")
GND = dru.Facts(net="GND", netclass="Power", type="Via")
NOTCH = "A.NetClass == 'HiZ' && A.NetName != '/Notch/TT1K_OUT' && A.NetName != '/Notch/TT40_OUT'"


def test_evaluate_class_name_and_type_terms():
    assert dru.evaluate(NOTCH, HIZ) is True
    assert dru.evaluate(NOTCH, OUT) is False
    assert dru.evaluate(NOTCH, GND) is False
    assert dru.evaluate("A.hasNetclass('HiZ') || A.NetName == 'GND'", GND) is True
    assert dru.evaluate("!(A.NetClass == 'HiZ')", GND) is True
    assert dru.evaluate("A.NetName == '/Notch/*'", HIZ) is True  # == takes wildcards
    assert dru.evaluate("A.Type == 'via' && A.NetClass == 'HiZ'", HIZ) is True
    assert dru.evaluate("", GND) is True


def test_evaluate_says_unknown_for_what_it_cannot_see():
    assert dru.evaluate("A.insideArea('RF')", HIZ) is None
    assert dru.evaluate("A.insideArea('RF') && A.NetClass == 'Power'", HIZ) is False  # False && ? is False
    assert dru.evaluate("A.insideArea('RF') || A.NetClass == 'HiZ'", HIZ) is True
    assert dru.evaluate("A.NetClass == 'HiZ' && B.NetClass != 'HiZ'", HIZ) is None  # no B given


def test_the_routers_see_the_same_reader():
    assert routers_dru.load_rules is dru.load_rules and routers_dru.class_pairs is dru.class_pairs


def _project(tmp_path: Path, rules_text: str) -> Path:
    pro = tmp_path / "p.kicad_pro"
    pro.write_text(json.dumps({"board": {"design_settings": {"rules": {"min_track_width": 0.15}}}, "net_settings": {
        "classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.1, "via_diameter": 0.6, "via_drill": 0.3},
                    {"name": "HiZ", "clearance": 0.3, "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3}],
        "netclass_patterns": [{"netclass": "HiZ", "pattern": "*/TT*"}]}}), encoding="utf-8")
    pro.with_suffix(".kicad_dru").write_text(rules_text, encoding="utf-8")
    return pro


def test_copper_rules_read_the_kicad_dru(tmp_path):
    r = copper.Rules.load(_project(tmp_path, f'''(version 1)
# a comment
(rule "hiz_no_via"
  (constraint disallow via)
  (condition "{NOTCH}"))
(rule "hiz_clearance"
  (constraint clearance (min 0.5mm))
  (condition "A.NetClass == 'HiZ' && B.NetClass != 'HiZ'"))
(rule "gnd_wide"
  (constraint track_width (min 0.4mm))
  (condition "A.NetName == 'GND'"))
(rule "no_track_on_sense"
  (constraint disallow track)
  (condition "A.NetName == 'SENSE'"))
'''))
    assert r.disallowed("via", "/Notch/TT1K_M1") and not r.disallowed("via", "/Notch/TT1K_OUT") and not r.disallowed("via", "GND")
    assert r.disallowed("track", "SENSE") and not r.disallowed("track", "/Notch/TT1K_M1")
    assert r.between("/Notch/TT1K_M1", "GND") == 0.5 and r.between("GND", "/Notch/TT1K_M1") == 0.5  # either order
    assert r.between("/Notch/TT1K_M1", "/Notch/TT40_M1") == 0.3  # two HiZ nets: the class clearance
    assert r.between("GND", "SIG") == 0.2
    assert r.track("GND") == 0.4 and r.track("SIG") == 0.15  # the board minimum lifts Default's 0.1


def test_an_area_disallow_counts_as_applying(tmp_path):
    r = copper.Rules.load(_project(tmp_path, '(version 1)\n(rule "rf"\n  (constraint disallow via)\n  (condition "A.insideArea(\'RF\')"))\n'))
    why = r.disallowed("via", "GND")
    assert why and "not evaluated" in why


def test_clear_via_names_the_rule(tmp_path):
    from kicad_layer.review import BoardModel

    r = copper.Rules.load(_project(tmp_path, '(version 1)\n(rule "no_via"\n  (constraint disallow via)\n  (condition "A.NetName == \'X\'"))\n'))
    bm = BoardModel(path=Path("memory.kicad_pcb"), copper_layers=2, outline=(0, 0, 20, 20), footprints=[], segments=[], vias=[], zones=[], texts=[],
                    design_rules={})
    model = copper.Model(bm, r)
    assert copper.clear_via(model, "X", 10, 10)[1].startswith("NO: rule 'no_via'")
    assert copper.clear_via(model, "Y", 10, 10)[1].startswith("OK")


def test_the_stub_router_leaves_a_net_whose_tracks_a_rule_forbids(tmp_path):
    from kicad_layer.design import stubs
    from kicad_layer.review import BoardModel

    r = copper.Rules.load(_project(tmp_path, '(version 1)\n(rule "hands_off"\n  (constraint disallow track)\n  (condition "A.NetName == \'SENSE\'"))\n'))
    bm = BoardModel(path=Path("memory.kicad_pcb"), copper_layers=2, outline=(0, 0, 40, 40), footprints=[], segments=[], vias=[], zones=[], texts=[],
                    design_rules={})
    res = stubs.route_stubs(bm, r, [stubs.Open("SENSE", (10, 10), (20, 10), "F.Cu", "F.Cu")])
    assert (res.routed, res.skipped, res.routes.segments) == (0, 1, [])
    assert "hands_off" in res.lines[0]
