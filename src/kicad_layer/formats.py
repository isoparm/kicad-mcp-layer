"""The KiCad file formats this layer writes, in one place.

KiCad stamps every file with a format version (a date) and the release that wrote it. These are
KiCad 10.0's. The doctor reports them next to the installed kicad-cli and warns when KiCad has
moved on; files this layer writes still open in a newer KiCad, which upgrades them on save. The
schematic editor (``cst``, ``sch_edit``) keeps whatever version the file it edits already carries.

The layer is built against KiCad 10.0.6. Upgrade KiCad deliberately: run the full test suite after,
and read the doctor's advice.
"""

KICAD_RELEASE = "10.0"  # what generator_version says
KICAD_MAJOR = 10
SCHEMATIC_FORMAT = 20260306  # .kicad_sch as KiCad 10.0.6 writes it
BOARD_FORMAT = 20260206  # .kicad_pcb
SYMBOL_LIB_FORMAT = 20251024  # .kicad_sym

WRITER_FORMATS = {"schematic": SCHEMATIC_FORMAT, "board": BOARD_FORMAT, "symbol_lib": SYMBOL_LIB_FORMAT}
