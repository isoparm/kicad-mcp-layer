"""Rule sets a project builds its .kicad_pro on: a fab's limits, the standard net classes, the widths and vias.

A project supplies what is its own: the net-class assignments (which nets are pairs, which are
rails) and any custom design rules. Values here were checked against the fab's published limits
on the dates noted; change them here, not in a project.
"""
from __future__ import annotations

from pathlib import Path

from .project import Rules

# JLCPCB 4-layer, 1 oz (kicad_layer.fab_limits.JLCPCB_4L_1OZ, checked 2026-09-04); values chosen with margin
JLCPCB_4L_1OZ = {
    "min_clearance": 0.125, "min_track_width": 0.1, "min_via_diameter": 0.5, "min_through_hole_diameter": 0.25,
    # hole clearance 0.19: KiCad's GCT USB4105 footprint leaves 0.194 mm between its pegs and the shield pads
    "min_via_annular_width": 0.15, "min_hole_to_hole": 0.45, "min_hole_clearance": 0.19, "min_copper_edge_clearance": 0.3,
    # library footprints draw their fab texts at 0.1 mm; a project's own silk keeps to JLCPCB's 0.15 and the review checks it
    "min_silk_clearance": 0.0, "min_text_height": 0.8, "min_text_thickness": 0.1, "min_connection": 0.1, "min_microvia_diameter": 0.2,
    "min_microvia_drill": 0.1, "min_resolved_spokes": 2, "allow_blind_buried_vias": False, "allow_microvias": False, "max_error": 0.005,
    "solder_mask_to_copper_clearance": 0.0, "use_height_for_length_calcs": True, "min_groove_width": 0.0,
}

# Differential-pair geometry: 100 ohm = 0.1722 / 0.15 mm, 90 ohm = 0.2332 / 0.15 mm, from JLCPCB's published
# table for its JLC04161H-7628 stack-up (kicad_layer.routing.JLC04161H_7628, 2026-09-05). On PCBWay's standard
# 4-layer 1.6 mm stack (7628 prepreg 0.1855 mm after lamination, Dk 4.74, 1 oz; preset pcbway-4l-1.6mm,
# 2026-09-06) the same widths compute to about 103 and 90 ohm, within the +-10 % PCBWay guarantees.
STANDARD_CLASSES = [
    # 0.125 mm clearance: JLCPCB's 4-layer minimum is 0.09, and KiCad's DFN footprints leave exactly 0.125 to the exposed pad
    {"name": "Default", "clearance": 0.125, "track_width": 0.15, "via_diameter": 0.6, "via_drill": 0.3, "diff_pair_width": 0.15, "diff_pair_gap": 0.2, "diff_pair_via_gap": 0.25},
    # 0.125 like Default: KiCad checks pads of one footprint against each other with the nets' class clearance, and
    # every DFN exposed GND pad sits 0.125 from its pins
    {"name": "Power", "clearance": 0.125, "track_width": 0.5, "via_diameter": 0.7, "via_drill": 0.3, "diff_pair_width": 0.5, "diff_pair_gap": 0.2, "diff_pair_via_gap": 0.25},
    {"name": "100R", "clearance": 0.15, "track_width": 0.1722, "via_diameter": 0.6, "via_drill": 0.3, "diff_pair_width": 0.1722, "diff_pair_gap": 0.15, "diff_pair_via_gap": 0.25},
    {"name": "90R", "clearance": 0.15, "track_width": 0.2332, "via_diameter": 0.6, "via_drill": 0.3, "diff_pair_width": 0.2332, "diff_pair_gap": 0.15, "diff_pair_via_gap": 0.25},
    # 0.3 mm rails: a module's 3.3 V output (600 mA budget) and an SD card supply; wide enough for the current, narrow
    # enough to reach 0.2 mm module pads and thread a display connector area
    {"name": "Rail3", "clearance": 0.15, "track_width": 0.3, "via_diameter": 0.6, "via_drill": 0.3, "diff_pair_width": 0.3, "diff_pair_gap": 0.2, "diff_pair_via_gap": 0.25},
    # PoE taps and rectified input, 48-57 V. IPC-2221 B2 asks 0.6 mm for an uncoated outer layer at 51-100 V, but a
    # MagJack's tap pins sit 1.47 mm apart (0.05 mm between pads), so a track leaving one tap is 0.46 mm from the
    # next tap's pad whatever the rule says; 0.4 mm is what the part allows and is kept everywhere else too
    {"name": "POE", "clearance": 0.4, "track_width": 0.5, "via_diameter": 0.7, "via_drill": 0.3, "diff_pair_width": 0.5, "diff_pair_gap": 0.2, "diff_pair_via_gap": 0.25},
]

TRACK_WIDTHS = [0.0, 0.15, 0.1722, 0.2332, 0.3, 0.5, 1.0]
VIA_DIMENSIONS = [{"diameter": 0.0, "drill": 0.0}, {"diameter": 0.6, "drill": 0.3}, {"diameter": 0.7, "drill": 0.3}]
DIFF_PAIR_DIMENSIONS = [{"gap": 0.0, "via_gap": 0.0, "width": 0.0}, {"gap": 0.15, "via_gap": 0.25, "width": 0.1722}, {"gap": 0.15, "via_gap": 0.25, "width": 0.2332}]


def jlcpcb_4l(template: Path, assignments: list[dict], *, design_rules: str = "", classes: list[dict] | None = None) -> Rules:
    """The JLCPCB four-layer rule set with a project's net-class assignments and custom design rules."""
    return Rules(template=template, rules=dict(JLCPCB_4L_1OZ), classes=[dict(c) for c in (classes if classes is not None else STANDARD_CLASSES)],
                 assignments=[dict(a) for a in assignments], track_widths=list(TRACK_WIDTHS), via_dimensions=[dict(v) for v in VIA_DIMENSIONS],
                 diff_pair_dimensions=[dict(d) for d in DIFF_PAIR_DIMENSIONS], design_rules=design_rules)


# AISLER 4-layer 1.6 mm, 35 um ENIG (kicad_layer.fab_limits.AISLER_4L_35UM, checked 2026-09-08). These are the values AISLER
# ships in its own KiCad template (aisler-support/kicad/aisler-4-layer-hd-drc.kicad_pro), so KiCad's DRC is AISLER's DRC:
# 0.125 mm track and clearance, via drill 0.25 with a 0.1 ring, any hole 0.25 from other copper, 0.3 hole to hole and
# copper to edge, silk 0.15 wide and 0.8 high. AISLER pulls the solder mask back itself, so the mask clearance stays 0.
AISLER_4L_35UM = {
    "min_clearance": 0.125, "min_track_width": 0.125, "min_via_diameter": 0.45, "min_through_hole_diameter": 0.25,
    "min_via_annular_width": 0.1, "min_hole_to_hole": 0.3, "min_hole_clearance": 0.25, "min_copper_edge_clearance": 0.3,
    "min_silk_clearance": 0.0, "min_text_height": 0.8, "min_text_thickness": 0.15, "min_connection": 0.125, "min_microvia_diameter": 0.0,
    "min_microvia_drill": 0.0, "min_resolved_spokes": 2, "allow_blind_buried_vias": False, "allow_microvias": False, "max_error": 0.005,
    "solder_mask_to_copper_clearance": 0.0, "use_height_for_length_calcs": True, "min_groove_width": 0.0,
}

# AISLER's 4-layer 1.6 mm stack-up (kicad_layer.routing.AISLER_4L_1P6): 100 ohm = 0.22 / 0.15 mm, 90 ohm = 0.26 / 0.135 mm,
# 50 ohm single-ended = 0.295 mm, as published on its stack-up page (2025-12-01). A pair's class clearance equals its gap,
# because KiCad checks the two halves of a pair against each other with the class clearance.
AISLER_CLASSES = [
    {"name": "Default", "clearance": 0.125, "track_width": 0.15, "via_diameter": 0.6, "via_drill": 0.3, "diff_pair_width": 0.15, "diff_pair_gap": 0.2, "diff_pair_via_gap": 0.25},
    {"name": "Power", "clearance": 0.125, "track_width": 0.5, "via_diameter": 0.7, "via_drill": 0.3, "diff_pair_width": 0.5, "diff_pair_gap": 0.2, "diff_pair_via_gap": 0.25},
    {"name": "100R", "clearance": 0.15, "track_width": 0.22, "via_diameter": 0.6, "via_drill": 0.3, "diff_pair_width": 0.22, "diff_pair_gap": 0.15, "diff_pair_via_gap": 0.25},
    {"name": "90R", "clearance": 0.135, "track_width": 0.26, "via_diameter": 0.6, "via_drill": 0.3, "diff_pair_width": 0.26, "diff_pair_gap": 0.135, "diff_pair_via_gap": 0.25},
    {"name": "Rail3", "clearance": 0.15, "track_width": 0.3, "via_diameter": 0.6, "via_drill": 0.3, "diff_pair_width": 0.3, "diff_pair_gap": 0.2, "diff_pair_via_gap": 0.25},
    {"name": "POE", "clearance": 0.4, "track_width": 0.5, "via_diameter": 0.7, "via_drill": 0.3, "diff_pair_width": 0.5, "diff_pair_gap": 0.2, "diff_pair_via_gap": 0.25},
]
AISLER_TRACK_WIDTHS = [0.0, 0.15, 0.22, 0.26, 0.3, 0.5, 1.0]
AISLER_DIFF_PAIR_DIMENSIONS = [{"gap": 0.0, "via_gap": 0.0, "width": 0.0}, {"gap": 0.15, "via_gap": 0.25, "width": 0.22}, {"gap": 0.135, "via_gap": 0.25, "width": 0.26}]

# The custom rules AISLER ships in its .kicad_dru (May 2024): the largest drill its tooling has, no buried or micro vias,
# and no per-pad solder-mask margin because AISLER sets the mask pull-back itself.
AISLER_DESIGN_RULES = """# AISLER's own rules (aisler-support/kicad/aisler-4-layer-hd-drc.kicad_dru, May 2024)
(rule "aisler_max_drill_pth"
  (constraint hole_size (max 5.6mm))
  (condition "A.Pad_Type == 'Through-hole'"))
(rule "aisler_no_buried_via"
  (constraint disallow buried_via))
(rule "aisler_no_micro_via"
  (constraint disallow micro_via))
(rule "aisler_no_mask_margin_override"
  (constraint assertion "A.Soldermask_Margin_Override == 0mm || A.Soldermask_Margin_Override == null")
  (condition "A.Type == 'Pad'"))
"""


def merge_design_rules(*texts: str) -> str:
    """One .kicad_dru text from several: a single version line first, then each text's rules in order."""
    bodies = []
    for text in texts:
        lines = text.strip().splitlines()
        if lines and lines[0].startswith("(version"):
            lines = lines[1:]
        body = "\n".join(lines).strip()
        if body:
            bodies.append(body)
    return "" if not bodies else "(version 1)\n" + "\n\n".join(bodies) + "\n"


def aisler_4l(template: Path, assignments: list[dict], *, design_rules: str = "", classes: list[dict] | None = None) -> Rules:
    """The AISLER four-layer rule set with a project's net-class assignments; AISLER's own custom rules come first in the .kicad_dru."""
    return Rules(template=template, rules=dict(AISLER_4L_35UM), classes=[dict(c) for c in (classes if classes is not None else AISLER_CLASSES)],
                 assignments=[dict(a) for a in assignments], track_widths=list(AISLER_TRACK_WIDTHS), via_dimensions=[dict(v) for v in VIA_DIMENSIONS],
                 diff_pair_dimensions=[dict(d) for d in AISLER_DIFF_PAIR_DIMENSIONS], design_rules=merge_design_rules(AISLER_DESIGN_RULES, design_rules))
