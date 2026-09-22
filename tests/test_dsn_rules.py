"""Design rules the Specctra DSN cannot carry: exclusions, keep-outs, class-to-class clearances.

The board is synthetic (built with the repo's own writer from a fixture library footprint) and so is
its .kicad_dru, one rule of every kind the export has to sort."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from kicad_layer.config import load_settings, set_settings
from kicad_layer.kicad_libs import load_footprint, register_footprint_lib
from kicad_layer.pcb_writer import BoardBuilder
from kicad_layer.routers import dru, dsn
from kicad_layer.sexpr import S, Sym, child, children, parse
from tests.conftest import FIXTURES

DRU = """(version 1)
# Hall sensor primary against everything else
(rule "hall creepage"
  (condition "A.NetClass == 'HALL_PRI' && B.NetClass != 'HALL_PRI'")
  (constraint creepage (min 3.0mm))
  (constraint clearance (min 7.25mm)))
(rule "sw to fb"
  (layer outer)
  (condition "A.NetClass == 'SW' && B.NetClass == 'FB'")
  (constraint clearance (min 2mm)))
(rule "no vias on hv"
  (condition "A.NetName == 'HV_INPUT'")
  (constraint disallow via))
(rule "current"
  (condition "A.NetName == '+BAT'")
  (constraint track_width (min 6mm)))
(rule "near the sensor"
  (condition "A.enclosedByArea('HVAREA') && A.NetName == '+3V3'")
  (constraint clearance (min 1mm)))
(rule "no vias in the hv area"
  (condition "A.insideArea('HVAREA')")
  (constraint disallow via))
(rule "odd"
  (condition "A.NetClass == 'SW' || B.NetClass == 'FB'")
  (constraint clearance (min 1mm)))
"""

PRO = {
    "net_settings": {
        "classes": [
            {"name": "Default", "track_width": 0.2, "clearance": 0.2, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "HALL_PRI", "track_width": 1.0, "clearance": 0.5},
            {"name": "POWER", "track_width": 6.0, "clearance": 0.3},
            {"name": "SW", "track_width": 0.5, "clearance": 0.2},
            {"name": "FB", "track_width": 0.2, "clearance": 0.2},
        ],
        "netclass_patterns": [
            {"pattern": "HALL_*", "netclass": "HALL_PRI"},
            {"pattern": "+BAT", "netclass": "POWER"},
            {"pattern": "SW", "netclass": "SW"},
            {"pattern": "FB", "netclass": "FB"},
        ],
    }
}


def rule_area(name: str, layers: list[str], pts, *, tracks="allowed", vias="allowed", footprints="allowed") -> list:
    """A rule area as KiCad 10 writes one (see the placement areas of the multichannel fixture)."""
    return S("zone", S("layers", *layers), S("uuid", f"00000000-0000-0000-0000-{abs(hash(name)) % 10**12:012d}"), S("name", name),
             S("hatch", Sym("edge"), 0.5), S("connect_pads", S("clearance", 0)), S("min_thickness", 0.25), S("filled_areas_thickness", Sym("no")),
             S("keepout", S("tracks", Sym(tracks)), S("vias", Sym(vias)), S("pads", Sym("allowed")), S("copperpour", Sym("allowed")), S("footprints", Sym(footprints))),
             S("placement", S("enabled", Sym("no")), S("sheetname", "")), S("fill", S("thermal_gap", 0.5), S("thermal_bridge_width", 0.5)),
             S("polygon", S("pts", *[S("xy", x, y) for x, y in pts])))


@pytest.fixture
def board(tmp_path) -> Path:
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    register_footprint_lib("mm", FIXTURES / "multichannel" / "multichannel_mixer.pretty")
    fp = load_footprint("mm", "RESC3216X65N")
    pcb = BoardBuilder(sheetfile="t.kicad_sch", title="t", copper_layers=2)
    pcb.rounded_rect_outline(0, 0, 60, 40, 1)
    parts = [("R1", (10, 10), {"1": "HALL_IP", "2": "HALL_IN"}), ("R2", (10, 30), {"1": "HALL_IP", "2": "HALL_IN"}),
             ("R3", (30, 10), {"1": "+BAT", "2": "MT_SW"}), ("R4", (30, 30), {"1": "+BAT", "2": "MT_SW"}),
             ("R5", (45, 10), {"1": "HV_INPUT", "2": "GND"}), ("R6", (45, 30), {"1": "HV_INPUT", "2": "GND"}),
             ("R7", (20, 20), {"1": "SW", "2": "FB"}), ("R8", (40, 20), {"1": "SW", "2": "FB"}),
             ("R9", (52, 10), {"1": "+3V3", "2": "GND"}), ("R10", (52, 30), {"1": "+3V3", "2": "GND"})]
    for ref, at, nets in parts:
        pcb.footprint(fp, ref, "1k", at, 0, pad_nets=nets)
    pcb.zone(net="+BAT", layer="F.Cu", polygon=[(26, 6), (34, 6), (34, 34), (26, 34)], name="bat pour")
    pcb.items.append(rule_area("KO", ["F.Cu"], [(2, 2), (6, 2), (6, 6), (2, 6)], tracks="not_allowed", vias="not_allowed"))
    pcb.items.append(rule_area("NOVIA", ["*.Cu"], [(2, 34), (6, 34), (6, 38), (2, 38)], vias="not_allowed", footprints="not_allowed"))
    pcb.items.append(rule_area("HVAREA", ["F.Cu", "B.Cu"], [(40, 2), (58, 2), (58, 15), (40, 15)]))
    path = tmp_path / "t.kicad_pcb"
    pcb.write(str(path))
    path.with_suffix(".kicad_pro").write_text(json.dumps(PRO), encoding="utf-8")
    path.with_suffix(".kicad_dru").write_text(DRU, encoding="utf-8")
    yield path
    set_settings(None)


def _root(text: str):
    return parse(text.replace('    (string_quote ")\n', ""))


def test_rules_parse_with_units_and_conditions():
    assert dru.length_mm("7.25mm") == 7.25 and dru.length_mm("10mil") == pytest.approx(0.254) and dru.length_mm("0.2") == 0.2
    r = dru.Rule("x", "A.NetClass == 'HV' && B.NetClass != 'HV'", constraints=[dru.Constraint("clearance", min=2.0)])
    assert dru.class_pairs(r, ["Default", "HV", "LV"]) == ([("HV", "Default"), ("HV", "LV")], None)
    assert dru.class_pairs(dru.Rule("y", "A.hasNetclass('HV') && B.NetClass == 'LV'"), ["HV", "LV"])[0] == [("HV", "LV")]
    assert dru.class_pairs(dru.Rule("z", "A.NetClass == 'HV'"), ["HV", "LV"])[0] == [("HV", "HV"), ("HV", "LV")]
    pairs, why = dru.class_pairs(dru.Rule("w", "A.NetClass == 'HV' && A.Type == 'Via'"), ["HV"])
    assert pairs == [] and "not a plain net-class test" in why
    assert dru.class_pairs(dru.Rule("v", "A.NetClass == 'HV' || B.NetClass == 'HV'"), ["HV"])[1] == "the condition has an OR"
    area = dru.Rule("a", "A.enclosedByArea('HV') && A.NetName == '/X*'")
    assert area.area_functions() == [("enclosedByArea", "HV")] and area.named_nets() == {"/X*"}
    assert dru.nets_of_rule(area, ["/X1", "/Y"], {}) == {"/X1"}


def test_export_sorts_every_rule_kind(board):
    exp = dsn.build_dsn(board, options=dsn.DsnOptions(auto_exclude_ruled_nets=True, exclude_nets=("MT_SW",)))
    # a. and b. excluded nets, with the reason
    assert set(exp.excluded) == {"HALL_IP", "HALL_IN", "HV_INPUT", "+BAT", "+3V3", "MT_SW"}
    assert "creepage" in exp.excluded["HALL_IP"] and "disallow" in exp.excluded["HV_INPUT"] and "enclosedByArea" in exp.excluded["+3V3"]
    assert "pour" in exp.excluded["+BAT"] and exp.excluded["MT_SW"] == "excluded by the caller"
    root = _root(exp.text)
    net = child(root, "network")
    cls = {str(c[1]): [str(a) for a in c[2:] if not isinstance(a, list)] for c in children(net, "class")}
    assert cls["HALL_PRI_excluded"] == ["HALL_IP", "HALL_IN"] and cls["POWER_excluded"] == ["+BAT"]
    assert sorted(cls["kicad_default_excluded"]) == ["+3V3", "HV_INPUT", "MT_SW"] and cls["kicad_default"] == ["GND"]
    assert sorted(exp.ignore_classes) == ["HALL_PRI_excluded", "POWER_excluded", "kicad_default_excluded"]
    # excluded nets keep their pins in the network, so their pads keep their nets for the router
    assert {str(n[1]) for n in children(net, "net")} >= {"HALL_IP", "+BAT", "MT_SW"}
    # e. class_class rules: HALL_PRI against every other class at 7.25 mm, SW to FB at 2 mm; the OR rule skipped
    cc = {frozenset((str(child(c, "classes")[1]), str(child(c, "classes")[2]))): float(child(child(c, "rule"), "clearance")[1]) for c in children(net, "class_class")}
    assert cc[frozenset(("HALL_PRI_excluded", "kicad_default"))] == 7250 and cc[frozenset(("HALL_PRI_excluded", "SW"))] == 7250
    assert cc[frozenset(("SW", "FB"))] == 2000
    assert frozenset(("HALL_PRI_excluded",)) not in cc, "a != rule does not keep a class away from itself"
    assert any("'odd'" in w and "OR" in w for w in exp.warnings)
    # c. rule areas as per-layer keep-outs; the dru's area-only disallow as a via keep-out
    st = child(root, "structure")
    ko = [(str(k[1]), str(child(k, "polygon")[1])) for k in children(st, "keepout")]
    vko = sorted((str(k[1]), str(child(k, "polygon")[1])) for k in children(st, "via_keepout"))
    assert ("KO", "F.Cu") in ko
    assert vko == [("HVAREA", "B.Cu"), ("HVAREA", "F.Cu"), ("NOVIA", "B.Cu"), ("NOVIA", "F.Cu")]
    assert [str(k[1]) for k in children(st, "place_keepout")] == ["NOVIA"]
    poly = child(children(st, "keepout")[0], "polygon")
    assert [float(a) for a in poly[3:5]] == [2000, -2000], "micrometres, Y up"
    # d. the unfilled pour of +BAT goes out as a plane on its layer, with a warning to refill
    assert any(str(p[1]) == "+BAT" and str(child(p, "polygon")[1]) == "F.Cu" for p in children(st, "plane"))
    assert any("unfilled" in w and "+BAT" in w for w in exp.warnings)
    # existing behaviour: nothing auto-excluded unless asked, but every decision is reported
    plain = dsn.build_dsn(board)
    assert plain.excluded == {} and plain.ignore_classes == []
    assert any("HALL_IP should not be autorouted" in w for w in plain.warnings)
    forced = dsn.build_dsn(board, options=dsn.DsnOptions(auto_exclude_ruled_nets=True, force_nets=("+3V3",)))
    assert "+3V3" not in forced.excluded and any("+3V3 is routed on request" in w for w in forced.warnings)


def test_filled_pour_of_an_excluded_net_is_an_obstacle(board):
    text = board.read_text(encoding="utf-8")
    fill = '\t\t(filled_polygon\n\t\t\t(layer "F.Cu")\n\t\t\t(pts\n\t\t\t\t(xy 26.5 6.5) (xy 33.5 6.5) (xy 33.5 33.5) (xy 26.5 33.5)\n\t\t\t)\n\t\t)\n'
    j = text.index("\t\t(polygon", text.index('(name "bat pour")'))
    board.write_text(text[:j] + fill + text[j:], encoding="utf-8")
    exp = dsn.build_dsn(board, options=dsn.DsnOptions(auto_exclude_ruled_nets=True))
    st = child(_root(exp.text), "structure")
    pours = [k for k in children(st, "keepout") if str(k[1]) == "pour +BAT"]
    assert len(pours) == 1 and str(child(pours[0], "polygon")[1]) == "F.Cu"
    assert not any(str(p[1]) == "+BAT" for p in children(st, "plane"))


def test_footprint_keepout_zone_is_exported(board):
    """A keep-out inside a footprint is stored in board coordinates and must reach the router too."""
    text = board.read_text(encoding="utf-8")
    area = rule_area("ANT", ["F.Cu"], [(8, 8), (12, 8), (12, 12), (8, 12)], tracks="not_allowed")
    from kicad_layer.sexpr import dumps

    k = text.index("\t(footprint")
    end = text.index("\n\t)\n", k)
    board.write_text(text[:end] + "\n" + dumps(area, indent=2).rstrip("\n") + text[end:], encoding="utf-8")
    exp = dsn.build_dsn(board)
    st = child(_root(exp.text), "structure")
    assert ("ANT", "F.Cu") in [(str(k[1]), str(child(k, "polygon")[1])) for k in children(st, "keepout")]


def _jar() -> Path | None:
    env = os.environ.get("KICAD_LAYER_FREEROUTING")
    if env and Path(env).is_file() and shutil.which("java"):
        return Path(env)
    return None


@pytest.mark.slow
@pytest.mark.skipif(_jar() is None, reason="set KICAD_LAYER_FREEROUTING to a freerouting jar to run the router round trip")
def test_freerouting_leaves_excluded_nets_alone(board, tmp_path):
    """A real router run: the excluded nets end with no new copper, whatever the FreeRouting version does with -inc."""
    from kicad_layer import routes as routes_mod
    from kicad_layer.routers import routing_tools

    os.environ.setdefault("KICAD_LAYER_JAVA", shutil.which("java") or "")
    rep = routing_tools.autoroute(board, None, routes_in=None, routes_out=tmp_path / "r.json", plane_layers=None, routable_layers=None,
                                  passes=3, timeout_s=300, exclude_nets=["MT_SW"])
    excluded = {e.net for e in rep.excluded_nets}
    assert excluded == {"HALL_IP", "HALL_IN", "HV_INPUT", "+BAT", "+3V3", "MT_SW"}
    assert rep.ignored_classes and rep.class_clearances and rep.keepouts
    routed = routes_mod.load(tmp_path / "r.json")
    assert routed.nets & {"SW", "FB", "GND"}, rep.log_tail
    assert not (routed.nets & excluded)
