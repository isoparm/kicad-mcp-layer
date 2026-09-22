"""kicad-cli finds a board's rules by the board's name; the tools say so when they are missing."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kicad_layer.config import load_settings, set_settings
from kicad_layer.errors import LayerError
from kicad_layer.project import NO_PROJECT_RULES, board_rule_warnings

DATA = Path(__file__).parent / "data"
BOARD_TEXT = '(kicad_pcb (version 20241229) (generator "pcbnew"))\n'


@pytest.fixture
def ws(tmp_path):
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_MODE": "write", "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    yield tmp_path
    set_settings(None)


def _board(folder: Path, name: str = "amp") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    b = folder / f"{name}.kicad_pcb"
    b.write_text(BOARD_TEXT, encoding="utf-8")
    return b


def test_board_with_its_project_and_rules_has_no_warning(tmp_path):
    b = _board(tmp_path)
    b.with_suffix(".kicad_pro").write_text("{}", encoding="utf-8")
    b.with_suffix(".kicad_dru").write_text("(version 1)", encoding="utf-8")
    assert board_rule_warnings(b) == []


def test_missing_project_is_named_with_the_one_that_is_ignored(tmp_path):
    b = _board(tmp_path, "amp-copy")
    (tmp_path / "amp.kicad_pro").write_text("{}", encoding="utf-8")
    w = board_rule_warnings(b)
    assert len(w) == 1 and w[0].startswith(NO_PROJECT_RULES)
    assert "amp-copy.kicad_pro" in w[0] and "amp.kicad_pro is ignored" in w[0]


def test_rules_file_under_another_name_is_the_rename_mistake(tmp_path):
    b = _board(tmp_path, "amp-v2")
    b.with_suffix(".kicad_pro").write_text("{}", encoding="utf-8")
    (tmp_path / "amp.kicad_dru").write_text("(version 1)", encoding="utf-8")
    w = board_rule_warnings(b)
    assert len(w) == 1 and "amp.kicad_dru not applied" in w[0] and "amp-v2.kicad_dru" in w[0]


def test_no_rules_file_at_all_is_fine(tmp_path):
    b = _board(tmp_path)
    b.with_suffix(".kicad_pro").write_text("{}", encoding="utf-8")
    assert board_rule_warnings(b) == []


def _fake_cli(monkeypatch, module, calls: list):
    from kicad_layer.cli import runner

    class Cli:
        path = "kicad-cli"
        major = 10

    def run(cmd, timeout_s=None, cwd=None):
        calls.append([str(c) for c in cmd])
        out = Path(cmd[cmd.index("-o") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DATA / "drc-complex_hierarchy.json", out)
        return runner.CliResult(command=[str(c) for c in cmd], returncode=0, stdout="", stderr="", duration_s=0.1)

    monkeypatch.setattr(runner, "run", run)
    monkeypatch.setattr(module, "find_kicad_cli", lambda: Cli())


def test_run_drc_warns_without_the_project(ws, monkeypatch):
    from kicad_layer.cli import reports

    calls: list = []
    _fake_cli(monkeypatch, reports, calls)
    b = _board(ws / "p")
    r = reports.run_drc(b)
    assert r.verdict == "PASS" and r.warnings and r.warnings[0].startswith(NO_PROJECT_RULES)
    b.with_suffix(".kicad_pro").write_text("{}", encoding="utf-8")
    assert reports.run_drc(b).warnings == []


def test_refill_refuses_default_rules_unless_allowed(ws, monkeypatch):
    from kicad_layer import pcb_tools
    from kicad_layer.cli import discovery

    calls: list = []
    _fake_cli(monkeypatch, discovery, calls)
    b = _board(ws / "p")
    monkeypatch.setattr(pcb_tools, "choose_channel", lambda path, ch: ("file", path))
    with pytest.raises(LayerError) as e:
        pcb_tools.refill_zones(str(b))
    assert e.value.code == "PROJECT_NOT_FOUND" and "allow_default_rules" in str(e.value)
    assert calls == [], "nothing may run before the refusal"
    res = pcb_tools.refill_zones(str(b), allow_default_rules=True)
    assert calls and "--refill-zones" in calls[0]
    assert any(w.startswith(NO_PROJECT_RULES) for w in res.warnings)
    b.with_suffix(".kicad_pro").write_text("{}", encoding="utf-8")
    assert not any(w.startswith(NO_PROJECT_RULES) for w in pcb_tools.refill_zones(str(b)).warnings)


def test_autoroute_warns_when_the_project_default_is_missing(tmp_path):
    from kicad_layer.routers.routing_tools import _rule_warnings

    b = _board(tmp_path)
    assert _rule_warnings(b, None)[0].startswith(NO_PROJECT_RULES)
    assert "does not exist" in _rule_warnings(b, tmp_path / "other.kicad_pro")[0]
    pro = b.with_suffix(".kicad_pro")
    pro.write_text(json.dumps({}), encoding="utf-8")
    assert _rule_warnings(b, None) == [] and _rule_warnings(b, pro) == []
