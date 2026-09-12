"""lib_fetch: EasyEDA component data into KiCad library files through easyeda2kicad."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_layer import easyeda
from kicad_layer.errors import INVALID_ARGUMENT, LIB_FETCH_FAILED, LayerError

SAMPLE_OUTPUT = """[INFO] Created Kicad symbol for ID : C46550395
       Symbol name : JVJ16V1000M10X10
       Library path : C:\\x\\t.kicad_sym
[INFO] Created Kicad footprint for ID: C46550395
       Footprint name: CAP-SMD_BD10.0-L10.3-W10.3-FD
       Footprint path: C:\\x\\t.pretty\\CAP-SMD_BD10.0-L10.3-W10.3-FD.kicad_mod
[INFO] Created 3D model for ID: C46550395
       3D model name: CAP-SMD_BD10.0-L10.3-W10.3-FD
       3D model path (wrl): C:\\x\\t.3dshapes\\CAP-SMD_BD10.0-L10.3-W10.3-FD.wrl
       3D model path (step): C:\\x\\t.3dshapes\\CAP-SMD_BD10.0-L10.3-W10.3-FD.step
-- easyeda2kicad.py v1.0.1 --
"""

SYM_FILE = """(kicad_symbol_lib
\t(version 20231120)
\t(generator "easyeda2kicad")
\t(symbol "WC-PD30B050G"
\t\t(property "Reference" "U" (at 0 0 0))
\t\t(symbol "WC-PD30B050G_0_1"
\t\t\t(rectangle (start -1 1) (end 1 -1))
\t\t)
\t\t(symbol "WC-PD30B050G_1_1"
\t\t\t(pin passive line (at -3 0 0) (length 2) (name "VA1" (effects (font (size 1 1)))) (number "1" (effects (font (size 1 1)))))
\t\t\t(pin passive line (at 3 0 180) (length 2) (name "+" (effects (font (size 1 1)))) (number "9" (effects (font (size 1 1)))))
\t\t)
\t)
\t(symbol "MA16V470M8X10"
\t\t(symbol "MA16V470M8X10_1_1"
\t\t\t(pin passive line (at 0 3 270) (length 2) (name "+" (effects (font (size 1 1)))) (number "1" (effects (font (size 1 1)))))
\t\t)
\t)
)
"""


def _fake_library(base: Path) -> None:
    base.with_suffix(".kicad_sym").write_text(SYM_FILE, encoding="utf-8")
    pretty = base.with_suffix(".pretty")
    pretty.mkdir()
    (pretty / "PWRM-TH_WC-PD30B012-1.kicad_mod").write_text('(footprint "PWRM-TH_WC-PD30B012-1" (pad 1 thru_hole rect (at 0 0) (size 1.9 1.9) (drill 1.1)) (pad 2 thru_hole circle (at 2.54 0) (size 1.9 1.9) (drill 1.1)))', encoding="utf-8")
    shapes = base.with_suffix(".3dshapes")
    shapes.mkdir()
    (shapes / "PWRM-TH_WC-PD30B012-1.step").write_text("ISO-10303-21;", encoding="utf-8")
    (shapes / "PWRM-TH_WC-PD30B012-1.wrl").write_text("#VRML V2.0 utf8", encoding="utf-8")


def test_parse_names_reads_the_three_names():
    assert easyeda.parse_names(SAMPLE_OUTPUT) == {"symbol": "JVJ16V1000M10X10", "footprint": "CAP-SMD_BD10.0-L10.3-W10.3-FD", "model": "CAP-SMD_BD10.0-L10.3-W10.3-FD"}
    assert easyeda.parse_names("nothing here") == {"symbol": None, "footprint": None, "model": None}


def test_entries_list_top_level_symbols_footprints_and_models(tmp_path: Path):
    base = tmp_path / "jlc"
    assert easyeda.entries(base) == {"symbol": set(), "footprint": set(), "model": set()}
    _fake_library(base)
    e = easyeda.entries(base)
    assert e["symbol"] == {"WC-PD30B050G", "MA16V470M8X10"}, "unit sub-symbols are not entries"
    assert e["footprint"] == {"PWRM-TH_WC-PD30B012-1"}
    assert e["model"] == {"PWRM-TH_WC-PD30B012-1.step", "PWRM-TH_WC-PD30B012-1.wrl"}


def test_symbol_pins_counts_inside_one_symbol_only(tmp_path: Path):
    base = tmp_path / "jlc"
    _fake_library(base)
    assert easyeda.symbol_pins(base.with_suffix(".kicad_sym"), "WC-PD30B050G") == 2
    assert easyeda.symbol_pins(base.with_suffix(".kicad_sym"), "MA16V470M8X10") == 1
    assert easyeda.symbol_pins(base.with_suffix(".kicad_sym"), "nope") is None


def test_target_dir_prefers_the_explicit_folder_then_the_project(tmp_path: Path):
    assert easyeda.target_dir(tmp_path / "x", tmp_path) == tmp_path / "x"
    assert easyeda.target_dir(None, tmp_path) == tmp_path / "lib"
    with pytest.raises(LayerError) as ex:
        easyeda.target_dir(None, None)
    assert ex.value.code == INVALID_ARGUMENT


def test_bad_code_and_bad_parts_are_refused_before_any_network(tmp_path: Path):
    with pytest.raises(LayerError) as ex:
        easyeda.fetch("hello", tmp_path)
    assert ex.value.code == INVALID_ARGUMENT
    with pytest.raises(LayerError) as ex:
        easyeda.fetch("C46550395", tmp_path, parts="everything")
    assert ex.value.code == INVALID_ARGUMENT


@pytest.mark.slow
def test_fetch_live_writes_symbol_footprint_and_models(tmp_path: Path):
    """A real fetch of a two-pad capacitor (LCSC C46550395); skipped when EasyEDA is unreachable."""
    try:
        r = easyeda.fetch("C46550395", tmp_path, "jlc")
    except LayerError as ex:
        if ex.code == LIB_FETCH_FAILED and ("reach" in str(ex) or "longer than" in str(ex) or "wrote nothing" in str(ex)):
            pytest.skip(f"EasyEDA not reachable: {ex}")
        raise
    assert r.symbol and r.footprint and r.pads == 2
    assert r.footprint_id == f"jlc:{r.footprint}" and r.symbol_id == f"jlc:{r.symbol}"
    assert (tmp_path / "jlc.kicad_sym").is_file() and (tmp_path / "jlc.pretty" / f"{r.footprint}.kicad_mod").is_file()
    assert r.model_step and Path(r.model_step).is_file()
    assert set(r.files) >= {"jlc.kicad_sym", f"jlc.pretty/{r.footprint}.kicad_mod"}
    assert any("drawn by users" in w for w in r.warnings)
