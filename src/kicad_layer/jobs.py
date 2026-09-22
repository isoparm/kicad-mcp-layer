"""Background jobs for tools that outlast the MCP client's request timeout.

An MCP client gives a tool call about a minute. FreeRouting on a real board, DRC or a zone refill on a
few hundred parts, and a high-quality render take longer, and the client gives up while the work goes
on unseen. ``job_start`` runs such a tool in a thread of this server and answers at once with a job id;
``job_status`` reports the state, the time taken so far, the last lines of output and, for FreeRouting,
the pass, unrouted and violation counts; ``job_result`` gives the tool's normal result once it is done.
The call returning does not stop the work, and a subprocess is never killed because a call returned.

State lives in memory and in one small JSON file per job under ``<cache>/jobs``, so a restarted server
still knows a job it started: finished jobs keep their result, a job that was running when the server
went away is reported as ``lost``. An id nobody knows is ``unknown``.

Code that runs inside a job reaches it through :func:`current` (a context variable, so it follows the
work into worker threads) to add log lines and progress, and runs long subprocesses with
:func:`run_process`, which streams their output into the job.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import subprocess
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from kicad_layer.config import settings

log = logging.getLogger(__name__)

LOG_LINES = 200  # kept per job; job_status shows the tail
_SAVE_EVERY_S = 2.0
_TERMINATE_GRACE_S = 15.0

_current: contextvars.ContextVar["Job | None"] = contextvars.ContextVar("kicad_layer_job", default=None)


def _jobs_dir() -> Path:
    d = settings().cache_dir / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Job:
    id: str
    tool: str
    args: dict[str, Any]
    state: str = "queued"  # queued, running, done, failed
    started: float = field(default_factory=time.time)
    finished: float | None = None
    result: Any = None
    error: str | None = None
    error_code: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    lines: deque = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    path: Path | None = None
    _saved: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _done: threading.Event = field(default_factory=threading.Event)

    def log(self, line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        with self._lock:
            self.lines.append(line)
        p = router_progress(line)
        if p:
            self.progress.update(p)
        if time.time() - self._saved > _SAVE_EVERY_S:
            self.save()

    def elapsed(self) -> float:
        return round((self.finished or time.time()) - self.started, 1)

    def to_json(self) -> dict[str, Any]:
        with self._lock:
            lines = list(self.lines)
        return {"id": self.id, "tool": self.tool, "args": self.args, "state": self.state, "started": self.started, "finished": self.finished,
                "result": self.result, "error": self.error, "error_code": self.error_code, "progress": self.progress, "log": lines[-50:]}

    def save(self) -> None:
        self._saved = time.time()
        if self.path is None:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.to_json(), default=str), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:  # the job goes on without its file
            log.warning("job %s: cannot save state: %s", self.id, exc)


def current() -> Job | None:
    """The job the calling code runs in, or None outside a job."""
    return _current.get()


def note(line: str) -> None:
    """A log line for the current job; nothing outside a job."""
    job = _current.get()
    if job is not None:
        job.log(line)


_PASS_RE = re.compile(r"\bpass\s*#?\s*(\d+)", re.IGNORECASE)
_UNROUTED_RE = re.compile(r"(\d+)\s+unrouted", re.IGNORECASE)
_VIOLATIONS_RE = re.compile(r"(\d+)\s+violations?", re.IGNORECASE)


def router_progress(line: str) -> dict[str, Any]:
    """Pass number, unrouted and violation counts from a FreeRouting log line, e.g.
    'Auto-router pass #3 on board 'x' was completed in 12.1 seconds with the score of 812.5 (14 unrouted and 2 violations).'"""
    out: dict[str, Any] = {}
    m = _PASS_RE.search(line)
    if m:
        out["pass"] = int(m.group(1))
        if re.search(r"optimi[sz]", line, re.IGNORECASE):
            out["phase"] = "optimise"
        elif re.search(r"rout", line, re.IGNORECASE):
            out["phase"] = "route"
    m = _UNROUTED_RE.search(line)
    if m:
        out["unrouted"] = int(m.group(1))
    m = _VIOLATIONS_RE.search(line)
    if m:
        out["violations"] = int(m.group(1))
    return out


@dataclass
class ProcessResult:
    returncode: int
    stdout: str  # stdout and stderr, interleaved as the process wrote them
    stderr: str = ""
    timed_out: bool = False
    seconds: float = 0.0


def run_process(cmd: list[str], *, timeout_s: float, cwd: Path | None = None) -> ProcessResult:
    """Run ``cmd``, streaming each output line into the current job; on timeout terminate it (a grace
    period for the program to save what it has), then kill it. Never raises on timeout: ``timed_out`` says so."""
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            cwd=str(cwd) if cwd else None, bufsize=1)
    lines: list[str] = []
    job = _current.get()

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            if job is not None:
                job.log(line)

    reader = threading.Thread(target=pump, name="job-output", daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        note(f"timeout after {timeout_s:.0f} s: asking the process to stop")
        proc.terminate()
        try:
            proc.wait(timeout=_TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    reader.join(timeout=5)
    return ProcessResult(returncode=proc.returncode, stdout="".join(lines), timed_out=timed_out, seconds=round(time.time() - t0, 1))


class JobRunner:
    """Runs callables in background threads and remembers them."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self._dir = state_dir
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def _state_dir(self) -> Path:
        if self._dir is None:
            return _jobs_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    def start(self, tool: str, args: dict[str, Any], fn: Callable[[], Any]) -> Job:
        """Start ``fn()`` as a job for ``tool``. Its return value (JSON-able) becomes the result; an exception
        makes the job failed with the message and, for a LayerError, its code."""
        job = Job(id=uuid.uuid4().hex[:12], tool=tool, args=args)
        job.path = self._state_dir() / f"{job.id}.json"
        with self._lock:
            self._jobs[job.id] = job
        job.save()

        def work() -> None:
            token = _current.set(job)
            job.state = "running"
            job.save()
            try:
                job.result = fn()
                job.state = "done"
            except Exception as exc:  # the job records every failure; nothing escapes the thread
                job.state = "failed"
                job.error = str(exc) or type(exc).__name__
                job.error_code = getattr(exc, "code", None) or _code_in(job.error)
                log.info("job %s (%s) failed: %s", job.id, tool, job.error)
                log.debug("%s", traceback.format_exc())
            finally:
                job.finished = time.time()
                job.save()
                job._done.set()
                _current.reset(token)

        threading.Thread(target=contextvars.copy_context().run, args=(work,), name=f"job-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        """The job in memory, or one read back from its file (a job of an earlier server process)."""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is not None:
            return job
        if not re.fullmatch(r"[0-9a-f]{6,32}", job_id or ""):
            return None
        path = self._state_dir() / f"{job_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        old = Job(id=data.get("id", job_id), tool=data.get("tool", "?"), args=data.get("args") or {}, state=data.get("state", "unknown"),
                  started=data.get("started") or 0.0, finished=data.get("finished"), result=data.get("result"), error=data.get("error"),
                  error_code=data.get("error_code"), progress=data.get("progress") or {})
        old.lines.extend(data.get("log") or [])
        if old.state in ("queued", "running"):
            old.state = "lost"  # the process that ran it is gone
            old.error = "the server restarted while this job ran; its result is lost (a subprocess may have finished on its own)"
        old._done.set()
        return old

    def wait(self, job_id: str, timeout_s: float) -> Job | None:
        job = self.get(job_id)
        if job is not None and timeout_s > 0:
            job._done.wait(timeout_s)
        return job


def _code_in(message: str) -> str | None:
    m = re.match(r"\[([A-Z_]+)\]", message or "")
    return m.group(1) if m else None


_RUNNER: JobRunner | None = None


def runner() -> JobRunner:
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = JobRunner()
    return _RUNNER
