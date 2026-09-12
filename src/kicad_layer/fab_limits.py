"""Manufacturing limits of named fabs, with where each number came from.

Limits are conservative "standard process" figures. A design that passes them at the
default limit orders without a capability upgrade; one that only meets the absolute
minimum is flagged as a warning so the engineer decides.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FabLimits:
    name: str
    source: str
    checked_on: str
    layers: int
    copper_oz: float
    min_track_mm: float
    min_space_mm: float
    min_via_drill_mm: float
    min_via_diameter_mm: float
    min_annular_ring_mm: float            # recommended
    abs_min_annular_ring_mm: float        # absolute minimum
    min_hole_to_hole_mm: float            # pad to pad, plated holes
    min_via_hole_to_hole_mm: float
    min_via_hole_to_track_mm: float
    min_copper_to_edge_mm: float
    min_silk_line_mm: float
    min_silk_text_height_mm: float
    min_mask_bridge_mm: float
    min_smd_pad_to_pad_mm: float
    min_board_mm: float
    thickness_options_mm: tuple[float, ...]


JLCPCB_2L_1OZ = FabLimits(
    name="JLCPCB, 2 layers, 1 oz",
    source="https://jlcpcb.com/capabilities/pcb-capabilities",
    checked_on="2026-09-04",
    layers=2,
    copper_oz=1.0,
    min_track_mm=0.10,
    min_space_mm=0.10,
    min_via_drill_mm=0.15,
    min_via_diameter_mm=0.25,
    min_annular_ring_mm=0.25,
    abs_min_annular_ring_mm=0.18,
    min_hole_to_hole_mm=0.45,
    min_via_hole_to_hole_mm=0.20,
    min_via_hole_to_track_mm=0.20,
    min_copper_to_edge_mm=0.20,
    min_silk_line_mm=0.15,
    min_silk_text_height_mm=1.0,
    min_mask_bridge_mm=0.10,
    min_smd_pad_to_pad_mm=0.15,
    min_board_mm=3.0,
    thickness_options_mm=(0.4, 0.6, 0.8, 1.0, 1.2, 1.6, 2.0),
)

JLCPCB_4L_1OZ = FabLimits(
    name="JLCPCB, 4 layers, 1 oz",
    source="https://jlcpcb.com/capabilities/pcb-capabilities",
    checked_on="2026-09-04",
    layers=4,
    copper_oz=1.0,
    min_track_mm=0.09,
    min_space_mm=0.09,
    min_via_drill_mm=0.15,
    min_via_diameter_mm=0.25,
    min_annular_ring_mm=0.20,
    abs_min_annular_ring_mm=0.15,
    min_hole_to_hole_mm=0.45,
    min_via_hole_to_hole_mm=0.20,
    min_via_hole_to_track_mm=0.20,
    min_copper_to_edge_mm=0.20,
    min_silk_line_mm=0.15,
    min_silk_text_height_mm=1.0,
    min_mask_bridge_mm=0.10,
    min_smd_pad_to_pad_mm=0.15,
    min_board_mm=3.0,
    thickness_options_mm=(0.4, 0.6, 0.8, 1.0, 1.2, 1.6, 2.0),
)

# AISLER (Aachen) 4 layers, 35 um copper, ENIG, 0.8 or 1.6 mm. Numbers from the design-rules page and, where the two
# differ, from the .kicad_pro AISLER ships (min via diameter 0.45 = drill 0.25 + 2 x 0.1 ring; hole clearance 0.25).
# Only the 4-layer product is entered; AISLER's 2-layer rules are on
# community.aisler.net/t/2-layer-1-6mm-35-m-enig-design-rules/3732 when a 2-layer board needs them.
AISLER_4L_35UM = FabLimits(
    name="AISLER, 4 layers, 35 um ENIG",
    source="https://community.aisler.net/t/4-layer-35-m-enig-design-rules/3733 (page of 2025-07-07) and "
    "https://github.com/AislerHQ/aisler-support kicad/aisler-4-layer-hd-drc (rules of May 2024, repo of 2026-01-20)",
    checked_on="2026-09-08",
    layers=4,
    copper_oz=1.0,
    min_track_mm=0.125,
    min_space_mm=0.125,
    min_via_drill_mm=0.25,
    min_via_diameter_mm=0.45,
    min_annular_ring_mm=0.30,          # plated through holes
    abs_min_annular_ring_mm=0.10,      # vias
    min_hole_to_hole_mm=0.30,
    min_via_hole_to_hole_mm=0.30,
    min_via_hole_to_track_mm=0.25,     # any hole to copper of another net
    min_copper_to_edge_mm=0.30,
    min_silk_line_mm=0.15,
    min_silk_text_height_mm=0.8,
    min_mask_bridge_mm=0.10,
    min_smd_pad_to_pad_mm=0.125,
    min_board_mm=10.0,
    thickness_options_mm=(0.8, 1.6),
)

FABS: dict[str, FabLimits] = {
    "jlcpcb": JLCPCB_2L_1OZ,
    "jlcpcb-2l": JLCPCB_2L_1OZ,
    "jlcpcb-4l": JLCPCB_4L_1OZ,
    "aisler": AISLER_4L_35UM,
    "aisler-4l": AISLER_4L_35UM,
}


def limits_for(fab: str, copper_layers: int) -> FabLimits:
    key = fab.lower()
    if key == "jlcpcb":
        return JLCPCB_4L_1OZ if copper_layers >= 4 else JLCPCB_2L_1OZ
    if key not in FABS:
        raise KeyError(f"unknown fab {fab!r}; known: {', '.join(sorted(FABS))}")
    return FABS[key]
