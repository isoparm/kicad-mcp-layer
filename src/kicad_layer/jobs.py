"""Background jobs for tools that outlast the MCP client's request timeout.

An MCP client gives a tool call about a minute. FreeRouting on a real board, DRC or a zone refill on a
few hundred parts, and a high-quality render take longer, and the client gives up while the work goes
on unseen. ``job_start`` runs such a tool in the background and answers at once with a job id;
``job_status`` reports the state, the time taken so far, the last lines of output and, for FreeRouting,
the pass, unrouted and violation counts; ``job_result`` gives the tool's normal result once it is done.
The call returning does not stop the work, and a subprocess is never killed because a call returned.

The work runs in a **detached worker process** (``python -m kicad_layer.jobs worker <job dir>``), not in
the server: an MCP host that reconnects restarts the server, and the job goes on. Everything about a job
lives in its directory ``<cache>/jobs/<id>/``:

* ``spec.json``: what to run (an importable ``module:function`` and its keyword arguments) and the
  settings of the server that started it;
* ``status.json``: state, times, the worker's pid and a heartbeat, progress, error;
* ``log.txt``: the output, one line each;
* ``result.json``: the final result, JSON (a pydantic model is dumped with ``model_dump``).

Any server process reads those files, so a job started by an earlier one still reports and returns its
result. A job whose worker is gone without a final state is ``lost``; an id nobody knows is ``unknown``.
When the worker cannot be spawned (or ``KICAD_LAYER_JOBS=thread``), the job runs in a thread of the
server as before and writes the same files; such a job is lost when its server goes away.

Code that runs inside a job reaches it through :func:`current` (a context variable, so it follows the
work into worker threads) to add log lines and progress, and runs long subprocesses with
:func:`run_process`, which streams their output into the job.
"""

from __future__ import annotations

import contextvars
import dataclasses
import importlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from kicad_layer.config import Settings, set_settings, settings

log = logging.getLogger(__name__)

LOG_LINES = 200  # kept per job in memory; job_status shows the tail
_SAVE_EVERY_S = 2.0
_TERMINATE_GRACE_S = 15.0
_HEARTBEAT_S = 5.0  # the worker rewrites status.json this often
_STALE_S = 120.0  # a running job whose heartbeat is older than this, and whose pid is gone or reused, is lost
_LOG_TAIL_BYTES = 64 * 1024
TERMINAL = ("done", "failed", "lost")

# Windows process creation flags (subprocess has them only on Windows)
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000  # a hidden console: the worker's own console children (java, kicad-cli) open no window either
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000  # leave the host's job object, which may kill its tree when the server goes

_current: contextvars.ContextVar["Job | None"] = contextvars.ContextVar("kicad_layer_job", default=None)


def _jobs_dir() -> Path:
    d = settings().cache_dir / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_json(path: Path, data: Any) -> None:
    """Atomic: a reader sees the old file or the new one, never half of it."""
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, default=_jsonable), encoding="utf-8")
    for attempt in range(5):
        try:
            tmp.replace(path)
            return
        except PermissionError:  # Windows: a reader has the target open this instant
            if attempt == 4:
                raise
            time.sleep(0.05)


def _read_json(path: Path) -> Any:
    for attempt in range(3):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):  # Windows sharing violation mid-replace
            if attempt == 2:
                return None
            time.sleep(0.05)
    return None


def _jsonable(obj: Any) -> Any:
    """``json.dumps`` default: pydantic models and dataclasses as their fields, paths and the rest as text."""
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    return str(obj)


def _tail(path: Path, n: int = LOG_LINES) -> list[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _LOG_TAIL_BYTES))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > _LOG_TAIL_BYTES and lines:
        lines = lines[1:]  # the first one is cut
    return lines[-n:]


@dataclass
class Job:
    id: str
    tool: str
    args: dict[str, Any]
    state: str = "queued"  # queued, running, done, failed; lost when read back and its runner is gone
    started: float = field(default_factory=time.time)
    finished: float | None = None
    result: Any = None
    error: str | None = None
    error_code: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    lines: deque = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    dir: Path | None = None  # <cache>/jobs/<id>
    mode: str = "thread"  # thread (in the server) or worker (a detached process)
    pid: int | None = None  # the process that runs the work
    _saved: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _save_lock: threading.Lock = field(default_factory=threading.Lock)  # one writer at a time, each with the state as it is then
    _done: threading.Event = field(default_factory=threading.Event)

    def log(self, line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        with self._lock:
            self.lines.append(line)
            if self.dir is not None:
                try:
                    with (self.dir / "log.txt").open("a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except OSError as exc:
                    log.debug("job %s: cannot append to its log: %s", self.id, exc)
        p = router_progress(line)
        if p:
            self.progress.update(p)
        if p or time.time() - self._saved > _SAVE_EVERY_S:  # progress at once: another process reads it from the file
            self.save()

    def elapsed(self) -> float:
        return round((self.finished or time.time()) - self.started, 1)

    def status_json(self) -> dict[str, Any]:
        return {"id": self.id, "tool": self.tool, "args": self.args, "state": self.state, "started": self.started, "finished": self.finished,
                "error": self.error, "error_code": self.error_code, "progress": self.progress, "mode": self.mode, "pid": self.pid,
                "heartbeat": time.time()}

    def to_json(self) -> dict[str, Any]:
        """Status, result and the last log lines in one dict."""
        with self._lock:
            lines = list(self.lines)
        return {**self.status_json(), "result": self.result, "log": lines[-50:]}

    def save(self) -> None:
        """Write status.json (and nothing else: the log is appended as it comes, the result written once)."""
        self._saved = time.time()
        if self.dir is None:
            return
        with self._save_lock:
            try:
                _write_json(self.dir / "status.json", self.status_json())
            except OSError as exc:  # the job goes on without its file
                log.warning("job %s: cannot save state: %s", self.id, exc)

    def finish(self, result: Any = None, exc: BaseException | None = None) -> None:
        """Record the outcome: the result file first, then the final state, so a done job always has its result."""
        if exc is None:
            try:
                if self.dir is not None:
                    _write_json(self.dir / "result.json", result)
                self.result = result if self.dir is None else _read_json(self.dir / "result.json")
                self.state = "done"
            except (OSError, TypeError, ValueError) as err:  # not serialisable, or the disk refused it
                exc = err
        if exc is not None:
            self.state = "failed"
            self.error = str(exc) or type(exc).__name__
            self.error_code = getattr(exc, "code", None) or _code_in(self.error)
            log.info("job %s (%s) failed: %s", self.id, self.tool, self.error)
            log.debug("%s", "".join(traceback.format_exception(exc)))
        self.finished = time.time()
        self.save()
        self._done.set()


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
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                            errors="replace", cwd=str(cwd) if cwd else None, bufsize=1)
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


# ------------------------------------------------------------------ the worker process


def _settings_json(s: Settings) -> dict[str, Any]:
    values = {f.name: getattr(s, f.name) for f in dataclasses.fields(s)}
    return {k: (str(v) if isinstance(v, Path) else v) for k, v in values.items()}


def _settings_from(data: dict[str, Any]) -> Settings:
    known = {f.name for f in dataclasses.fields(Settings)}
    kw = {k: v for k, v in data.items() if k in known}
    for k in ("workspace_root", "cache_dir", "docs_dir"):
        if kw.get(k) is not None:
            kw[k] = Path(kw[k])
    return Settings(**kw)


def _import_target(target: str) -> Callable[..., Any]:
    module, _, name = target.partition(":")
    fn = importlib.import_module(module)
    for part in name.split("."):
        fn = getattr(fn, part)
    return fn  # type: ignore[return-value]


def _user_paths() -> list[str]:
    """The import path entries this process added to the interpreter's own (a source checkout, a test root):
    the worker gets them through PYTHONPATH so it imports the same code."""
    prefixes = {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}
    out = []
    for p in sys.path:
        if not p or not os.path.isdir(p):
            continue
        real = os.path.realpath(p)
        if any(real == pre or real.startswith(pre + os.sep) for pre in prefixes):
            continue
        out.append(real)
    return out


def _worker_env() -> dict[str, str]:
    env = dict(os.environ)
    paths = _user_paths() + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if paths:
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(paths))
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONSTARTUP", None)
    return env


def spawn_worker(job_dir: Path) -> subprocess.Popen:
    """Start ``python -m kicad_layer.jobs worker <job_dir>`` detached from this process: its own session on
    POSIX, a new process group with a hidden console (and out of the host's job object when allowed) on
    Windows. Its stdout never reaches the server's stdio; stderr goes to ``worker.err`` in the job directory."""
    cmd = [sys.executable, "-m", "kicad_layer.jobs", "worker", str(job_dir)]
    kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "cwd": str(job_dir), "env": _worker_env(), "close_fds": True}
    with (job_dir / "worker.err").open("ab") as err:
        kw["stderr"] = err
        if sys.platform == "win32":
            base = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
            try:
                return subprocess.Popen(cmd, creationflags=base | _CREATE_BREAKAWAY_FROM_JOB, **kw)
            except OSError:  # the host's job object does not allow breakaway
                return subprocess.Popen(cmd, creationflags=base, **kw)
        return subprocess.Popen(cmd, start_new_session=True, **kw)


def worker_main(argv: list[str]) -> int:
    """The worker: run the job described by ``<job_dir>/spec.json`` and record everything in that directory."""
    if len(argv) != 2 or argv[0] != "worker":
        sys.stderr.write("usage: python -m kicad_layer.jobs worker <job directory>\n")
        return 2
    job_dir = Path(argv[1])
    logging.basicConfig(stream=sys.stderr, level=getattr(logging, os.environ.get("KICAD_LAYER_LOG", "INFO").upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    spec = _read_json(job_dir / "spec.json")
    if not isinstance(spec, dict):
        sys.stderr.write(f"no job spec in {job_dir}\n")
        return 2
    status = _read_json(job_dir / "status.json") or {}
    job = Job(id=spec["id"], tool=spec.get("tool", "?"), args=spec.get("args") or {}, started=status.get("started") or time.time(),
              dir=job_dir, mode="worker", pid=os.getpid())
    try:
        if spec.get("settings"):
            set_settings(_settings_from(spec["settings"]))
        fn = _import_target(spec["target"])
    except Exception as exc:  # the job records every failure
        job.finish(exc=exc)
        return 1
    job.state = "running"
    job.save()
    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(_HEARTBEAT_S):
            if job.state == "running":
                job.save()

    threading.Thread(target=heartbeat, name="job-heartbeat", daemon=True).start()
    token = _current.set(job)
    try:
        result = fn(**(spec.get("target_args") or {}))
    except BaseException as exc:  # noqa: BLE001 - a SystemExit in the tool must still be recorded
        stop.set()
        job.finish(exc=exc)
        return 1
    finally:
        _current.reset(token)
    stop.set()
    job.finish(result)
    return 0 if job.state == "done" else 1


# ------------------------------------------------------------------ the runner the server uses


def _detached_default() -> bool:
    return os.environ.get("KICAD_LAYER_JOBS", "worker").strip().lower() not in ("thread", "threads", "inprocess")


class JobRunner:
    """Starts jobs (a detached worker, or a thread as the fallback) and reads them back from their files."""

    def __init__(self, state_dir: Path | None = None, *, detached: bool | None = None) -> None:
        self._dir = state_dir
        self._detached = detached
        self._jobs: dict[str, Job] = {}  # thread jobs of this process: live objects
        self._procs: dict[str, subprocess.Popen] = {}  # workers this process spawned (polled, so none is left a zombie)
        self._lock = threading.Lock()

    def _state_dir(self) -> Path:
        if self._dir is None:
            return _jobs_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    def start(self, tool: str, args: dict[str, Any], fn: Callable[[], Any], *, target: str | None = None,
              target_args: dict[str, Any] | None = None) -> Job:
        """Start a job for ``tool``. With ``target`` (an importable ``module:function`` called with ``target_args``,
        all JSON) the work runs in a detached worker; ``fn()`` is the in-process fallback when the worker cannot
        be spawned or detached jobs are off. The return value (JSON-able, or a pydantic model) becomes the result;
        an exception makes the job failed with the message and, for a LayerError, its code."""
        job = Job(id=uuid.uuid4().hex[:12], tool=tool, args=args)
        job.dir = self._state_dir() / job.id
        job.dir.mkdir(parents=True, exist_ok=True)
        detached = self._detached if self._detached is not None else _detached_default()
        if target is not None and detached:
            spec = {"id": job.id, "tool": tool, "args": args, "target": target, "target_args": target_args or {},
                    "settings": _settings_json(settings())}
            try:
                _write_json(job.dir / "spec.json", spec)
                job.mode = "worker"
                job.save()
                proc = spawn_worker(job.dir)
            except (OSError, TypeError, ValueError) as exc:
                log.warning("job %s: cannot start a worker process (%s); running it in the server", job.id, exc)
                job.mode = "thread"
            else:
                job.pid = proc.pid
                with self._lock:
                    self._procs[job.id] = proc
                try:  # status.json is the worker's from now on: the pid goes to a file of its own
                    _write_json(job.dir / "worker.pid", proc.pid)
                except OSError as exc:
                    log.warning("job %s: cannot record the worker's pid: %s", job.id, exc)
                return self.get(job.id) or job
        return self._start_thread(job, fn)

    def _start_thread(self, job: Job, fn: Callable[[], Any]) -> Job:
        job.mode = "thread"
        job.pid = os.getpid()
        with self._lock:
            self._jobs[job.id] = job
        job.save()

        def work() -> None:
            token = _current.set(job)
            job.state = "running"
            job.save()
            try:
                result = fn()
            except Exception as exc:  # the job records every failure; nothing escapes the thread
                job.finish(exc=exc)
            else:
                job.finish(result)
            finally:
                _current.reset(token)

        threading.Thread(target=contextvars.copy_context().run, args=(work,), name=f"job-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        """The job in memory, or one read back from its files (a worker's, or a job of an earlier server process)."""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is not None:
            return job
        if not re.fullmatch(r"[0-9a-f]{6,32}", job_id or ""):
            return None
        return self._load(self._state_dir() / job_id)

    def _load(self, d: Path) -> Job | None:
        data = _read_json(d / "status.json")
        if not isinstance(data, dict):
            return None
        job = Job(id=data.get("id", d.name), tool=data.get("tool", "?"), args=data.get("args") or {}, state=data.get("state", "unknown"),
                  started=data.get("started") or 0.0, finished=data.get("finished"), error=data.get("error"), error_code=data.get("error_code"),
                  progress=data.get("progress") or {}, dir=d, mode=data.get("mode", "thread"), pid=data.get("pid"))
        job.lines.extend(_tail(d / "log.txt"))
        if job.state not in TERMINAL:
            reason = self._gone(job, data)
            if reason:
                again = _read_json(d / "status.json")  # it may have finished between the two reads
                if isinstance(again, dict) and again.get("state") in TERMINAL:
                    return self._load(d)
                job.state = "lost"
                job.error = reason
        if job.state == "done":
            job.result = _read_json(d / "result.json")
        if job.state in TERMINAL:
            job._done.set()
        return job

    def _gone(self, job: Job, data: dict[str, Any]) -> str | None:
        """Why a job that is not finished is not running any more, or None while it runs."""
        if job.mode != "worker":
            return "the server restarted while this job ran in it; its result is lost (a subprocess may have finished on its own)"
        with self._lock:
            proc = self._procs.get(job.id)
        if proc is not None:
            code = proc.poll()
            if code is None:
                return None
            return f"the worker process (pid {proc.pid}) exited with {code} without a result{self._err_tail(job)}"
        from kicad_layer.locks import pid_running

        pid = job.pid or (_read_json(job.dir / "worker.pid") if job.dir is not None else None)
        beat = float(data.get("heartbeat") or 0.0)
        if not pid:  # spawned this instant, or the spawner died before it could say so
            return None if time.time() - beat < _STALE_S else f"the worker process never started{self._err_tail(job)}"
        if time.time() - beat < 3 * _HEARTBEAT_S:  # a fresh heartbeat: no need to ask the OS (tasklist on Windows)
            return None
        if pid_running(int(pid)) and time.time() - beat < _STALE_S:
            return None
        return f"the worker process (pid {pid}) is gone without a result{self._err_tail(job)}"

    @staticmethod
    def _err_tail(job: Job) -> str:
        if job.dir is None:
            return ""
        tail = _tail(job.dir / "worker.err", 5)
        return (": " + " | ".join(tail)) if tail else ""

    def wait(self, job_id: str, timeout_s: float) -> Job | None:
        """The job, after waiting up to ``timeout_s`` for it to finish."""
        job = self.get(job_id)
        if job is None or timeout_s <= 0 or job.state in TERMINAL:
            return job
        with self._lock:
            live = job_id in self._jobs
        if live:
            job._done.wait(timeout_s)
            return job
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            job = self.get(job_id)
            if job is None or job.state in TERMINAL:
                break
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


if __name__ == "__main__":
    # run through the imported module, not this __main__ copy: code in the job calls kicad_layer.jobs.note()
    from kicad_layer.jobs import worker_main as _worker_main

    sys.exit(_worker_main(sys.argv[1:]))
