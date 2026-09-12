"""The pad-level comparison names pads by footprint so two designs compare whatever their references are."""

from __future__ import annotations

from pathlib import Path

from kicad_layer.design import compare as cmp

OURS = """<export version="E"><components>
<comp ref="MOD1"><value>CM5</value><footprint>CM5IO:CM5_GPIO</footprint><libsource lib="x" part="CM5_GPIO"/></comp>
<comp ref="J1"><value>DSI</value><footprint>CM5IO:FH12</footprint><libsource lib="x" part="Conn_22"/></comp>
<comp ref="R1"><value>2k2</value><footprint>Resistor_SMD:R_0603</footprint><libsource lib="x" part="R"/></comp>
<comp ref="U1"><value>SW</value><footprint>Package:SOT-23-5</footprint><libsource lib="x" part="STMPS2151"/></comp>
</components><nets>
<net code="1" name="/SCL"><node ref="MOD1" pin="35" pinfunction="ID_SC"/><node ref="J1" pin="20"/><node ref="R1" pin="1"/></net>
<net code="2" name="+3V3"><node ref="MOD1" pin="84" pinfunction="3V3"/><node ref="R1" pin="2"/></net>
<net code="3" name="/EN"><node ref="MOD1" pin="75" pinfunction="SD_PWR_ON"/><node ref="U1" pin="4" pinfunction="EN"/></net>
<net code="4" name="unconnected-(MOD1-Pad56)"><node ref="MOD1" pin="56" pinfunction="GPIO3"/></net>
<net code="5" name="/D0"><node ref="MOD1" pin="115" pinfunction="D0_N"/><node ref="J1" pin="2"/></net>
<net code="6" name="/X"><node ref="MOD1" pin="50" pinfunction="GPIO17"/><node ref="J1" pin="9"/></net>
</nets></export>"""

REF = """<export version="E"><components>
<comp ref="Module1"><value>CM5</value><footprint>CM5IO:CM5</footprint><libsource lib="x" part="CM5"/></comp>
<comp ref="J16"><value>Conn</value><footprint>CM5IO:FH12</footprint><libsource lib="x" part="Conn_22"/></comp>
<comp ref="J8"><value>HAT</value><footprint>Conn:PinHeader_2x20</footprint><libsource lib="x" part="Conn_02x20"/></comp>
<comp ref="R7"><value>2.2K 1%</value><footprint>Resistor_SMD:R_0402</footprint><libsource lib="x" part="R"/></comp>
<comp ref="U5"><value>RT9742</value><footprint>Package:SOT-23-5</footprint><libsource lib="x" part="RT9742"/></comp>
</components><nets>
<net code="1" name="/SCL1"><node ref="Module1" pin="35" pinfunction="ID_SC"/><node ref="J16" pin="20"/><node ref="R7" pin="2"/></net>
<net code="2" name="/+3.3v"><node ref="Module1" pin="84" pinfunction="3V3"/><node ref="R7" pin="1"/></net>
<net code="3" name="/SD_EN"><node ref="Module1" pin="75" pinfunction="SD_PWR_ON"/><node ref="U5" pin="4" pinfunction="EN"/></net>
<net code="4" name="/GPIO3"><node ref="Module1" pin="56" pinfunction="GPIO3"/><node ref="J8" pin="5"/></net>
<net code="5" name="/DPHY"><node ref="Module1" pin="115" pinfunction="D0_N"/><node ref="J16" pin="3"/></net>
<net code="6" name="unconnected-(Module1-Pad50)"><node ref="Module1" pin="50" pinfunction="GPIO17"/></net>
</nets></export>"""


def _designs(tmp_path: Path):
    (tmp_path / "ours.xml").write_text(OURS, encoding="utf-8")
    (tmp_path / "ref.xml").write_text(REF, encoding="utf-8")
    return cmp.load(tmp_path / "ours.xml"), cmp.load(tmp_path / "ref.xml")


def test_verdicts_per_module_pin(tmp_path):
    ours, ref = _designs(tmp_path)
    report = cmp.compare(ours, ref, ("MOD1",), ("Module1",))
    by_pin = {r.pin: r.verdict for r in report.rows}
    assert by_pin == {"35": "same", "50": "ours only", "56": "reference only", "75": "same", "84": "power", "115": "differ"}
    row35 = next(r for r in report.rows if r.pin == "35")
    assert [n.text() for n in row35.ours] == ["FH12.20", "R 2.2k -> rail 3.3V"]  # 2k2 and 2.2K, +3V3 and +3.3v, are the same
    assert [n.text() for n in row35.ref] == ["FH12.20", "R 2.2k -> rail 3.3V"]


def test_summary_names_only_the_pins_that_are_not_the_same(tmp_path):
    ours, ref = _designs(tmp_path)
    lines = cmp.compare(ours, ref, ("MOD1",), ("Module1",)).summary()
    assert lines[0].startswith("same 2")
    text = "\n".join(lines)
    assert "115 D0_N" in text and "FH12.2" in text and "FH12.3" in text
    assert " 35 " not in text and "differ" in text and "reference only" in text and "ours only" in text


def test_markdown_lists_footprint_and_pad_on_both_sides_and_the_connectors(tmp_path):
    ours, ref = _designs(tmp_path)
    md = cmp.compare(ours, ref, ("MOD1",), ("Module1",)).markdown("ours", "ref", ours, ref)
    assert "| 115 | D0_N | differ | FH12.2 | FH12.3 |" in md
    assert "| 56 | GPIO3 | reference only | open | PinHeader_2x20.5 |" in md
    assert "## Connector pads in ref" in md and "**J8**" in md and "CM5.56[GPIO3]" in md


def test_ignored_references_make_a_pin_open(tmp_path):
    ours, ref = _designs(tmp_path)
    report = cmp.compare(ours, ref, ("MOD1",), ("Module1",), ignore_reference=("J8",))
    assert {r.pin: r.verdict for r in report.rows}["56"] == "nc"


def test_cli_writes_the_report(tmp_path, capsys):
    _designs(tmp_path)
    rc = cmp.main([str(tmp_path / "ours.xml"), str(tmp_path / "ref.xml"), "--module", "MOD1", "--reference-module", "Module1", "--out", str(tmp_path / "r.md")])
    assert rc == 0 and (tmp_path / "r.md").read_text(encoding="utf-8").startswith("# ours against ref")
    assert "differ" in capsys.readouterr().out
    assert cmp.main([]) == 2
