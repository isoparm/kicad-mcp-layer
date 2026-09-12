"""The fab rule sets a project builds its .kicad_pro on."""
from pathlib import Path

from kicad_layer.design import rules
from kicad_layer.design.project import Rules

TEMPLATE = Path("template.kicad_pro")
ASSIGN = [{"netclass": "Power", "pattern": "+3V3"}]


def test_aisler_rule_set_is_aislers_own_kicad_numbers():
    r = rules.aisler_4l(TEMPLATE, ASSIGN)
    assert isinstance(r, Rules)
    assert r.rules["min_track_width"] == 0.125 and r.rules["min_clearance"] == 0.125
    assert r.rules["min_via_diameter"] == 0.45 and r.rules["min_through_hole_diameter"] == 0.25 and r.rules["min_via_annular_width"] == 0.1
    assert r.rules["min_hole_clearance"] == 0.25 and r.rules["min_hole_to_hole"] == 0.3 and r.rules["min_copper_edge_clearance"] == 0.3
    assert r.rules["solder_mask_to_copper_clearance"] == 0.0
    by_name = {c["name"]: c for c in r.classes}
    assert by_name["100R"]["track_width"] == 0.22 and by_name["100R"]["diff_pair_gap"] == 0.15
    assert by_name["90R"]["track_width"] == 0.26 and by_name["90R"]["diff_pair_gap"] == 0.135
    for c in r.classes:
        assert c["clearance"] >= r.rules["min_clearance"] and c["track_width"] >= r.rules["min_track_width"]
        if c["name"].endswith("R"):  # a pair's halves are different nets: the class clearance must not exceed the gap
            assert c["clearance"] <= c["diff_pair_gap"]
    assert 0.22 in r.track_widths and 0.26 in r.track_widths


def test_aisler_rules_come_first_in_the_dru_and_the_version_line_is_single():
    mine = "(version 1)\n(rule \"mine\"\n  (constraint clearance (min 0.15mm)))\n"
    dru = rules.aisler_4l(TEMPLATE, ASSIGN, design_rules=mine).design_rules
    assert dru.startswith("(version 1)\n") and dru.count("(version") == 1
    assert dru.index("aisler_max_drill_pth") < dru.index('(rule "mine"')
    assert "disallow buried_via" in dru and "disallow micro_via" in dru and "Soldermask_Margin_Override" in dru
    assert rules.aisler_4l(TEMPLATE, ASSIGN).design_rules.count("(rule ") == 4
    assert rules.merge_design_rules("", "  ") == ""


def test_rule_sets_hand_out_copies():
    r = rules.aisler_4l(TEMPLATE, ASSIGN)
    r.rules["min_track_width"] = 1.0
    r.classes[0]["clearance"] = 9.0
    r.track_widths.append(7.0)
    assert rules.AISLER_4L_35UM["min_track_width"] == 0.125 and rules.AISLER_CLASSES[0]["clearance"] == 0.125 and 7.0 not in rules.AISLER_TRACK_WIDTHS
    j = rules.jlcpcb_4l(TEMPLATE, ASSIGN, design_rules="(version 1)\n")
    assert j.rules["min_track_width"] == 0.1 and j.design_rules == "(version 1)\n"
