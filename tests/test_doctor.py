"""The doctor names what is missing: kicad-python for the IPC channel, and the formats the writers emit."""

from __future__ import annotations

import sys

from kicad_layer import doctor, formats
from kicad_layer.ipc import probe


def test_missing_kicad_python_is_named_not_unknown(monkeypatch, workspace):
    for name in [m for m in sys.modules if m == "kipy" or m.startswith("kipy.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "kipy", None)  # makes ``import kipy`` raise ImportError
    info = probe.probe_ipc(timeout_ms=100)
    assert info.diagnosis == "not_installed" and not info.reachable
    report = doctor.diagnose()
    assert any("kicad-mcp-layer[ipc]" in a for a in report.advice)


def test_doctor_reports_the_writer_formats(workspace):
    report = doctor.diagnose()
    assert report.writer_formats == formats.WRITER_FORMATS
    assert report.writer_formats["schematic"] == 20260306 and report.writer_formats["board"] == 20260206
