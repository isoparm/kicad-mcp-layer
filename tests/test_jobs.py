"""The background job runner, with fake slow work instead of KiCad."""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

from kicad_layer import jobs
from kicad_layer.errors import LayerError


@pytest.fixture
def runner(tmp_path):
    return jobs.JobRunner(tmp_path / "jobs")


def test_start_returns_at_once_and_the_result_comes_later(runner, tmp_path):
    gate = threading.Event()

    def slow():
        jobs.note("working")
        gate.wait(5)
        return {"answer": 42}

    t0 = time.monotonic()
    job = runner.start("run_drc", {"board_path": "b.kicad_pcb"}, slow)
    assert time.monotonic() - t0 < 0.5
    time.sleep(0.05)
    assert runner.get(job.id).state == "running" and "working" in list(job.lines)
    gate.set()
    done = runner.wait(job.id, 5)
    assert done.state == "done" and done.result == {"answer": 42} and done.elapsed() >= 0
    saved = json.loads((tmp_path / "jobs" / job.id / "status.json").read_text(encoding="utf-8"))
    assert saved["state"] == "done" and saved["args"] == {"board_path": "b.kicad_pcb"} and saved["mode"] == "thread"
    assert json.loads((tmp_path / "jobs" / job.id / "result.json").read_text(encoding="utf-8")) == {"answer": 42}
    assert "working" in (tmp_path / "jobs" / job.id / "log.txt").read_text(encoding="utf-8")


def test_a_failure_keeps_its_code(runner):
    def boom():
        raise LayerError("KICAD_CLI_TIMEOUT", "too slow", hint="wait")

    job = runner.wait(runner.start("run_drc", {}, boom).id, 5)
    assert job.state == "failed" and job.error_code == "KICAD_CLI_TIMEOUT" and "too slow" in job.error


def test_a_restarted_server_reports_lost_done_and_unknown(runner, tmp_path):
    gate = threading.Event()
    running = runner.start("autoroute", {}, lambda: gate.wait(5))
    finished = runner.wait(runner.start("render_board", {}, lambda: {"text": ["ok"]}).id, 5)
    time.sleep(0.05)
    fresh = jobs.JobRunner(tmp_path / "jobs")  # a new server process: nothing in memory
    assert fresh.get(running.id).state == "lost"
    again = fresh.get(finished.id)
    assert again.state == "done" and again.result == {"text": ["ok"]}
    assert fresh.get("0123456789ab") is None and fresh.get("../etc/passwd") is None
    gate.set()


def test_router_progress_reads_freerouting_lines():
    p = jobs.router_progress("Auto-router pass #3 on board 'x' was completed in 12.1 seconds with the score of 812.5 (14 unrouted and 2 violations).")
    assert p == {"pass": 3, "phase": "route", "unrouted": 14, "violations": 2}
    assert jobs.router_progress("Optimizer pass #7 improved the score by 1.2%")["phase"] == "optimise"
    assert jobs.router_progress("Loading design") == {}


def test_run_process_streams_into_the_job_and_stops_at_the_timeout(runner, monkeypatch):
    monkeypatch.setattr(jobs, "_TERMINATE_GRACE_S", 2.0)
    script = ("import sys, time\n"
              "print('Auto-router pass #1 was completed (9 unrouted and 0 violations)', flush=True)\n"
              "print('Auto-router pass #2 was completed (4 unrouted and 1 violations)', flush=True)\n"
              "time.sleep(30)\n")
    job = runner.start("autoroute", {}, lambda: jobs.run_process([sys.executable, "-c", script], timeout_s=1.5).__dict__)
    done = runner.wait(job.id, 20)
    assert done.state == "done"
    assert done.result["timed_out"] is True and "pass #2" in done.result["stdout"]
    assert done.progress == {"pass": 2, "phase": "route", "unrouted": 4, "violations": 1}
    assert any("timeout" in line for line in done.lines)


def test_run_process_outside_a_job_is_plain():
    r = jobs.run_process([sys.executable, "-c", "print('hi')"], timeout_s=20)
    assert r.returncode == 0 and r.stdout.strip() == "hi" and not r.timed_out


@pytest.mark.anyio
async def test_job_tools_run_a_tool_through_the_server(client, monkeypatch):
    from kicad_layer.cli import reports
    from kicad_layer.models import VerdictReport

    monkeypatch.setenv("KICAD_LAYER_JOBS", "thread")  # the fake below lives in this process: the in-server fallback
    monkeypatch.setattr(jobs, "_RUNNER", None)

    def fake_run_drc(board, **kw):
        jobs.note("fake kicad-cli running")
        time.sleep(0.3)
        return VerdictReport(verdict="PASS", kind="drc", source=str(board), counts={"total": 0}, warnings=["w"])

    monkeypatch.setattr(reports, "run_drc", fake_run_drc)
    started = await client.call_tool("job_start", {"tool": "run_drc", "args": {"board_path": "pic_programmer/pic_programmer.kicad_pcb", "summary": True}})
    st = started.structured_content
    assert st["state"] in ("queued", "running") and st["tool"] == "run_drc"
    status = (await client.call_tool("job_status", {"job_id": st["id"], "wait_s": 10})).structured_content
    assert status["state"] == "done" and "fake kicad-cli running" in status["log_tail"]
    res = (await client.call_tool("job_result", {"job_id": st["id"]})).structured_content
    assert res["state"] == "done" and res["result"]["verdict"] == "PASS" and res["result"]["warnings"] == ["w"]
    unknown = (await client.call_tool("job_status", {"job_id": "ffffffffffff"})).structured_content
    assert unknown["state"] == "unknown"


@pytest.mark.anyio
async def test_a_bad_argument_fails_the_job_like_a_direct_call(client, monkeypatch):
    """Through a real worker process, read back by a restarted server."""
    monkeypatch.delenv("KICAD_LAYER_JOBS", raising=False)
    monkeypatch.setattr(jobs, "_RUNNER", None)
    started = (await client.call_tool("job_start", {"tool": "run_drc", "args": {"board_path": "nope/missing.kicad_pcb"}})).structured_content
    monkeypatch.setattr(jobs, "_RUNNER", None)  # a new server process: nothing in memory
    status = (await client.call_tool("job_status", {"job_id": started["id"], "wait_s": 30})).structured_content
    assert status["state"] == "failed" and "FILE_NOT_FOUND" in status["error"]
    res = await client.call_tool("job_result", {"job_id": started["id"]})
    assert res.is_error and "FILE_NOT_FOUND" in res.content[0].text


@pytest.mark.anyio
async def test_core_tier_refuses_a_full_tier_tool(workspace):
    from mcp import Client

    from kicad_layer.server import build_server

    async with Client(build_server(tier="core"), raise_exceptions=True) as c:
        res = await c.call_tool("job_start", {"tool": "autoroute", "args": {"board_path": "x.kicad_pcb"}})
    assert res.is_error and "INVALID_ARGUMENT" in res.content[0].text


# ------------------------------------------------------------------ detached workers (docs/field-findings.md #19)


def fake_slow_tool(gate: str, crash: bool = False):
    """A job target for the worker process: logs, waits for ``gate`` to exist, returns a pydantic model."""
    from pathlib import Path

    from kicad_layer.models import VerdictReport

    jobs.note("Auto-router pass #2 was completed (3 unrouted and 0 violations)")
    jobs.note(f"working in pid {os.getpid()}")
    deadline = time.time() + 60
    while not Path(gate).exists() and time.time() < deadline:
        time.sleep(0.05)
    if crash:
        os._exit(3)  # the worker dies without a word
    if Path(gate).read_text() == "fail":
        raise LayerError("KICAD_CLI_TIMEOUT", "too slow", hint="wait")
    return VerdictReport(verdict="PASS", kind="drc", source="b.kicad_pcb", counts={"total": 0}, warnings=["from the worker"])


def _until(fn, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError("timed out")


def _start_worker(tmp_path, gate, **target_args):
    r = jobs.JobRunner(tmp_path / "jobs", detached=True)
    job = r.start("run_drc", {"board_path": "b.kicad_pcb"}, lambda: pytest.fail("ran in the server"), target=f"{__name__}:fake_slow_tool",
                  target_args={"gate": str(gate), **target_args})
    return r, job


@pytest.fixture
def cache(tmp_path):
    from kicad_layer.config import load_settings, set_settings

    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache")}))
    yield tmp_path
    set_settings(None)


def test_a_worker_job_survives_a_server_restart(cache):
    from kicad_layer.models import VerdictReport

    gate = cache / "gate"
    first, job = _start_worker(cache, gate)
    assert job.mode == "worker"
    running = _until(lambda: (j := first.get(job.id)) and any("working in pid" in ln for ln in j.lines) and j)
    assert running.state == "running" and running.pid != os.getpid()
    del first  # the server goes away; the worker does not
    fresh = jobs.JobRunner(cache / "jobs")
    again = fresh.get(job.id)
    assert again.state == "running" and again.progress == {"pass": 2, "phase": "route", "unrouted": 3, "violations": 0}
    assert fresh.wait(job.id, 0.3).state == "running"
    gate.write_text("ok")
    done = fresh.wait(job.id, 30)
    assert done.state == "done", (done.state, done.error)
    report = VerdictReport.model_validate(done.result)
    assert report.verdict == "PASS" and report.warnings == ["from the worker"]
    assert jobs.JobRunner(cache / "jobs").get(job.id).result == done.result


def test_a_worker_failure_keeps_its_code(cache):
    gate = cache / "gate"
    gate.write_text("fail")
    r, job = _start_worker(cache, gate)
    done = jobs.JobRunner(cache / "jobs").wait(job.id, 30)
    assert done.state == "failed" and done.error_code == "KICAD_CLI_TIMEOUT" and "too slow" in done.error


def test_a_worker_that_dies_is_lost(cache, monkeypatch):
    monkeypatch.setattr(jobs, "_HEARTBEAT_S", 0.1)  # the reader trusts a heartbeat this recent without asking the OS
    gate = cache / "gate"
    r, job = _start_worker(cache, gate, crash=True)
    _until(lambda: r.get(job.id).state == "running")
    gate.write_text("ok")
    lost = _until(lambda: (j := r.get(job.id)).state != "running" and j)
    assert lost.state == "lost" and "exited with 3" in lost.error
    later = _until(lambda: (j := jobs.JobRunner(cache / "jobs").get(job.id)).state != "running" and j)  # the pid is gone
    assert later.state == "lost" and "gone without a result" in later.error


def test_a_worker_that_cannot_start_falls_back_to_a_thread(cache, monkeypatch):
    def refuse(job_dir):
        raise OSError("no processes here")

    monkeypatch.setattr(jobs, "spawn_worker", refuse)
    r = jobs.JobRunner(cache / "jobs", detached=True)
    job = r.start("run_drc", {}, lambda: {"answer": 1}, target=f"{__name__}:fake_slow_tool", target_args={"gate": "x"})
    done = r.wait(job.id, 5)
    assert done.mode == "thread" and done.state == "done" and done.result == {"answer": 1}


def _summary_report(board: str):
    from kicad_layer.models import FindingsSummary, UnconnectedPair, VerdictReport, WorstFinding

    worst = WorstFinding(id="abc123", type="clearance", severity="error", rule="netclass:HiZ", description="Clearance violation (netclass 'HiZ' clearance 0.5000 mm; actual 0.3000 mm)",
                         required_mm=0.5, actual_mm=0.3, deficit_mm=0.2, items=["Track [/Notch/TT1K_M1] on F.Cu", "Pad 1 [GND] of C5 on F.Cu"], x_mm=12.5, y_mm=30.0)
    summary = FindingsSummary(by_type={"clearance": 1, "unconnected_items": 2}, by_rule={"netclass:HiZ": 1}, by_severity={"error": 3}, worst=[worst],
                              unconnected=2, unconnected_pairs=[UnconnectedPair(a="Pad 1 [/S] of R1", b="Pad 2 [/S] of J1", x_mm=1.0, y_mm=2.0),
                                                                UnconnectedPair(a="Pad 3 [GND] of U1")])
    return VerdictReport(verdict="FAIL", kind="drc", source=board, report_path="drc.json", counts={"errors": 1, "unconnected": 2, "total": 3},
                         summary=summary, notes=["summary=true: the full list is in report_path"], exit_code=5, duration_s=1.25)


def drc_summary_through_the_server(board_path: str):
    """A job target for the worker process: the run_drc tool through a fresh server (``tools.run_job_tool``), kicad-cli faked."""
    from kicad_layer import tools
    from kicad_layer.cli import reports

    def fake_run_drc(board, **kw):
        assert kw["summary"] is True, kw
        return _summary_report(str(board))

    reports.run_drc = fake_run_drc  # this is the worker's own process
    return tools.run_job_tool("run_drc", {"board_path": board_path, "summary": True}, "core")


def test_a_worker_run_drc_summary_result_round_trips_through_result_json(cache):
    """run_drc with summary=true in a detached worker: the VerdictReport and its FindingsSummary come back whole from result.json."""
    from kicad_layer.models import VerdictReport

    board = cache / "b.kicad_pcb"
    board.write_text("(kicad_pcb)\n", encoding="utf-8")
    r = jobs.JobRunner(cache / "jobs", detached=True)
    job = r.start("run_drc", {"board_path": "b.kicad_pcb", "summary": True}, lambda: pytest.fail("ran in the server"),
                  target=f"{__name__}:drc_summary_through_the_server", target_args={"board_path": str(board)})
    assert job.mode == "worker"
    done = jobs.JobRunner(cache / "jobs").wait(job.id, 60)  # read back by another runner, as a restarted server would
    assert done.state == "done", (done.state, done.error)
    on_disk = json.loads((cache / "jobs" / job.id / "result.json").read_text(encoding="utf-8"))
    assert on_disk == done.result
    report = VerdictReport.model_validate(on_disk)
    assert report.model_dump(exclude={"source"}) == _summary_report("").model_dump(exclude={"source"}) and report.source.endswith("b.kicad_pcb")
    assert report.summary.worst[0].deficit_mm == 0.2 and report.summary.unconnected_pairs[1].b is None and report.findings == []
