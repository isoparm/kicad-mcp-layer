"""The library index against KiCad's real stock libraries. Self-skips without a KiCad install."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from kicad_layer import libindex, libtables
from kicad_layer.config import load_settings, set_settings
from kicad_layer.errors import LayerError


def _share_available() -> bool:
    try:
        from kicad_layer.kicad_libs import share_dir

        return (share_dir() / "symbols" / "Device.kicad_sym").is_file()
    except Exception:
        return False


pytestmark = [pytest.mark.skipif(not _share_available(), reason="KiCad libraries not installed"),
              pytest.mark.slow]  # the module-scoped fixture indexes every library KiCad ships, about a minute


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    cache = tmp_path_factory.mktemp("libcache")
    set_settings(load_settings({"KICAD_LAYER_CACHE_DIR": str(cache), "KICAD_LAYER_WORKSPACE": str(Path(__file__).parent)}))
    t0 = time.time()
    report = libindex.build()
    report.first_build_seconds = time.time() - t0  # type: ignore[attr-defined]
    yield report
    set_settings(None)


def test_tables_resolve_to_real_files():
    syms = libtables.library_entries("symbol")
    fps = libtables.library_entries("footprint")
    assert len(syms) > 150 and all(e.path.suffix == ".kicad_sym" for e in syms)
    assert len(fps) > 100 and all(e.path.suffix == ".pretty" for e in fps)
    assert any(e.nickname == "Device" for e in syms)
    assert any(e.nickname == "Resistor_SMD" for e in fps)


def test_index_size_and_incremental_rebuild(index):
    assert index.symbols > 20000
    assert index.footprints > 14000
    assert index.libraries > 300
    t0 = time.time()
    again = libindex.build()
    assert again.rebuilt_libraries == 0
    assert time.time() - t0 < 5, "an unchanged index must not re-parse anything"


def test_search_finds_common_parts(index):
    led = libindex.search("LED", "symbol", 10)["symbols"]
    assert led and led[0][0] == "Device:LED"
    mcu = libindex.search("attiny1614", "symbol", 10)["symbols"]
    assert any(s[0] == "MCU_Microchip_ATtiny:ATtiny1614-SS" for s in mcu)
    r = libindex.search("0603 resistor", "footprint", 10)["footprints"]
    assert any(f[0] == "Resistor_SMD:R_0603_1608Metric" for f in r)
    usb = libindex.search("usb c receptacle 16", "symbol", 10)["symbols"]
    assert any("USB_C_Receptacle_USB2.0_16P" in s[0] for s in usb)


def test_derived_symbol_has_parent_pins(index):
    info = libindex.symbol_info("Transistor_FET:2N7002")
    assert info["extends"] == "Q_NMOS_GSD"
    names = {p[1] for p in info["pins"]}
    assert names == {"G", "S", "D"}
    assert info["footprint"] == "Package_TO_SOT_SMD:SOT-23"
    assert "Package_TO_SOT_SMD:SOT-23" in info["matching_footprints"]


def test_footprint_filters_match(index):
    info = libindex.symbol_info("Device:R")
    assert "Resistor_SMD:R_0603_1608Metric" in info["matching_footprints"]
    assert all(":" in f for f in info["matching_footprints"])


def test_footprint_info_pads(index):
    fp = libindex.footprint_info("Package_TO_SOT_SMD:SOT-23")
    assert fp["pad_count"] == 3 and fp["smd_pads"] == 3
    assert fp["width"] and fp["height"]
    numbers = {p["number"] for p in fp["pads"]}
    assert numbers == {"1", "2", "3"}


CM5_DEMO = Path(__file__).parent.parent.parent / "research" / "fixtures" / "cm5_minima"


@pytest.mark.skipif(not (CM5_DEMO / "sym-lib-table").is_file(), reason="CM5 MINIMA demo not available")
def test_project_libraries_are_indexed_and_shadow_globals(index):
    report = libindex.build(project_dir=CM5_DEMO)
    assert any(s.endswith("cm5_minima") for s in report.scopes)
    hits = libindex.search("compute module 5", "symbol", 10, CM5_DEMO)["symbols"]
    assert any(h[0] == "CM5IO:ComputeModule5-CM5_HSS" for h in hits), [h[0] for h in hits]
    fps = libindex.search("M.2 M key socket", "footprint", 10, CM5_DEMO)["footprints"]
    assert any("CM5IO:M.2 M Key socket" in f[0] for f in fps), [f[0] for f in fps]
    cm5 = libindex.symbol_info("CM5IO:ComputeModule5-CM5_HSS", CM5_DEMO)
    assert cm5["pin_count"] > 50
    # without the project, the same query sees only the stock libraries
    assert not any(h[0].startswith("CM5IO:") for h in libindex.search("compute module 5", "symbol", 10)["symbols"])


def test_unknown_ids_give_hints(index):
    with pytest.raises(LayerError) as exc:
        libindex.symbol_info("Device:Resistorr")
    assert "NOT_FOUND_IN_DESIGN" in str(exc.value)
    with pytest.raises(LayerError):
        libindex.footprint_info("Nope:Nothing")
