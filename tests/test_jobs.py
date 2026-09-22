"""The background job runner, with fake slow work instead of KiCad."""

from __future__ import annotations

import json
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
    saved = json.loads((tmp_path / "jobs" / f"{job.id}.json").read_text(encoding="utf-8"))
    assert saved["state"] == "done" and saved["result"] == {"answer": 42} and saved["args"] == {"board_path": "b.kicad_pcb"}


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
async def test_a_bad_argument_fails_the_job_like_a_direct_call(client):
    started = (await client.call_tool("job_start", {"tool": "run_drc", "args": {"board_path": "nope/missing.kicad_pcb"}})).structured_content
    status = (await client.call_tool("job_status", {"job_id": started["id"], "wait_s": 10})).structured_content
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
