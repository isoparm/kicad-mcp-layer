"""One session to KiCad per process, with every failure classified.

The rule that keeps file edits safe: a failure is either **Unreachable** (no address, the
connection or the send itself failed) or **Rejected** (the request was delivered and KiCad
said no, including timeouts). Only Unreachable may ever justify touching a board file on
disk, and never for a board this process has already seen live.

KiCad serialises requests anyway, so one lock guards every call. Connecting runs on a
helper thread with a bound, because a dial can hang when a pipe exists but nobody answers.

With ``KICAD_LAYER_IPC_LOG=1`` every call is logged (label, item count, duration, outcome) to a
rotating file ``<cache>/logs/ipc.log``. Whatever the setting, a transport failure records the KiCad
process ids before and after the call (in the log and in the error's data), and a request that
timed out while KiCad's process disappeared is reported as KiCad having exited, not as busy.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, TypeVar

from kicad_layer.config import settings
from kicad_layer.errors import (
    BOARD_NOT_OPEN,
    IPC_BUSY,
    IPC_REJECTED,
    KICAD_API_DISABLED,
    KICAD_NOT_RUNNING,
    LayerError,
)
from kicad_layer.ipc.probe import kicad_processes, resolved_address

log = logging.getLogger(__name__)
T = TypeVar("T")

_ipc_log: logging.Logger | None = None
_ipc_log_lock = threading.Lock()


def ipc_log() -> logging.Logger | None:
    """The request logger when KICAD_LAYER_IPC_LOG=1, else None."""
    global _ipc_log
    if os.environ.get("KICAD_LAYER_IPC_LOG", "").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    with _ipc_log_lock:
        target = settings().cache_dir / "logs" / "ipc.log"
        if _ipc_log is None or getattr(_ipc_log, "_target", None) != target:
            target.parent.mkdir(parents=True, exist_ok=True)
            lg = logging.getLogger("kicad_layer.ipc.requests")
            lg.propagate = False
            lg.setLevel(logging.INFO)
            for h in list(lg.handlers):
                lg.removeHandler(h)
                h.close()
            h = logging.handlers.RotatingFileHandler(target, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            lg.addHandler(h)
            lg._target = target  # type: ignore[attr-defined]
            _ipc_log = lg
        return _ipc_log


def _kicad_pids() -> list[int]:
    from kicad_layer.locks import kicad_pids

    try:
        return kicad_pids()
    except Exception:  # never let diagnostics break a call
        return []


def _count(result: Any) -> int | None:
    if isinstance(result, (list, tuple, set, dict)):
        return len(result)
    try:
        return len(result)  # protobuf repeated fields and similar
    except TypeError:
        return None


def _is_transport(exc: BaseException) -> bool:
    """A failure of the pipe itself rather than an answer from KiCad."""
    mod = type(exc).__module__ or ""
    return mod.startswith("pynng") or isinstance(exc, (ConnectionError, BrokenPipeError, EOFError))


class Unreachable(LayerError):
    """No address, or the connection or send failed. KiCad never saw the request."""


class Rejected(LayerError):
    """The request was delivered and KiCad answered with an error, or did not answer in time."""


def classify(exc: BaseException) -> LayerError:
    """Map a kicad-python exception to an Unreachable or Rejected LayerError."""
    from kipy.errors import ApiError
    from kipy.errors import ConnectionError as KipyConnectionError
    from kipy.proto.common.envelope_pb2 import ApiStatusCode as S

    if isinstance(exc, LayerError):
        return exc
    if isinstance(exc, KipyConnectionError):
        text = str(exc)
        if "timed out" in text.lower():
            return Rejected(
                IPC_BUSY,
                "KiCad did not answer in time.",
                hint="A modal dialog or an interactive tool may be open in KiCad; finish it and retry. If KiCad closed, it crashed: kicad_doctor says which.",
                retryable=True,
            )
        if kicad_processes():
            return Unreachable(
                KICAD_API_DISABLED,
                "KiCad is running but its API endpoint refused the connection.",
                hint="In KiCad open Preferences > Plugins and tick 'Enable KiCad API'; it takes effect immediately.",
            )
        return Unreachable(
            KICAD_NOT_RUNNING,
            "KiCad is not running.",
            hint="Open the project in KiCad and open the PCB Editor, then retry.",
        )
    if isinstance(exc, ApiError):
        code = exc.code
        if code == S.AS_BUSY:
            return Rejected(IPC_BUSY, "KiCad is busy with an interactive tool or dialog.", hint="Finish it in the GUI and retry.", retryable=True)
        if code == S.AS_NOT_READY:
            return Rejected(IPC_BUSY, "KiCad is still starting or has no project loaded.", retryable=True)
        if code == S.AS_TOKEN_MISMATCH:
            return Rejected(IPC_REJECTED, "KiCad restarted since this session connected.", hint="Retry; the session reconnects.", retryable=True)
        if code == S.AS_UNHANDLED:
            return Rejected(
                IPC_REJECTED,
                f"KiCad has no handler for this request: {exc}",
                hint="The PCB Editor window is not open, is busy (a dialog or an interactive tool), or KiCad crashed; run kicad_doctor, open the PCB Editor and retry.",
            )
        if code == S.AS_UNIMPLEMENTED:
            return Rejected(IPC_REJECTED, f"KiCad 10 does not implement this request: {exc}")
        return Rejected(IPC_REJECTED, f"KiCad rejected the request: {exc}")
    if _is_transport(exc):
        running = bool(kicad_processes())
        return Unreachable(
            KICAD_API_DISABLED if running else KICAD_NOT_RUNNING,
            f"The connection to KiCad failed ({type(exc).__name__}: {exc})" + ("." if running else "; KiCad is no longer running."),
            hint="KiCad may have crashed or closed its API endpoint; run kicad_doctor, reopen the board and retry.",
        )
    return Rejected(IPC_REJECTED, f"{type(exc).__name__}: {exc}", hint="Unexpected answer from KiCad; run kicad_doctor. A busy or crashed editor looks like this too.")


def _canonical(path: os.PathLike[str] | str) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


class Session:
    """Process-wide connection state. Use :func:`get_session`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._kicad: Any = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kicad-ipc-connect")
        self.seen_live: set[str] = set()
        self.known_pids: list[int] | None = None  # KiCad's process ids when the session connected

    # -- connection -----------------------------------------------------------------

    def _connect(self, timeout_ms: int) -> Any:
        from kipy import KiCad

        address, _ = resolved_address()
        override = settings().ipc_address_override

        def make() -> Any:
            k = KiCad(
                socket_path=address if override else None,
                client_name=f"kicad-mcp-layer-{os.getpid()}",
                timeout_ms=timeout_ms,
            )
            k.ping()  # forces the dial; raises kipy ConnectionError when nothing listens
            return k

        future = self._pool.submit(make)
        try:
            return future.result(timeout=max(5.0, timeout_ms / 1000 + 3))
        except FutureTimeout as exc:
            future.cancel()
            raise Unreachable(
                KICAD_NOT_RUNNING,
                "Connecting to KiCad's API endpoint hung.",
                hint="The endpoint may be stale; restart KiCad or check KICAD_API_SOCKET.",
                retryable=True,
            ) from exc

    def kicad(self) -> Any:
        """The connected KiCad client, connecting on first use."""
        with self._lock:
            if self._kicad is None:
                try:
                    self._kicad = self._connect(settings().ipc_call_timeout_ms)
                except LayerError:
                    raise
                except Exception as exc:
                    raise classify(exc) from exc
                self.known_pids = _kicad_pids()
                log.info("connected to KiCad at %s (KiCad pids %s)", resolved_address()[0], self.known_pids)
            return self._kicad

    def reset(self) -> None:
        with self._lock:
            self._kicad = None

    # -- guarded calls --------------------------------------------------------------

    def call(self, fn: Callable[[], T], *, retries: int = 2, label: str | None = None) -> T:
        """Run ``fn`` under the session lock, classifying failures and retrying busy states."""
        name = label or getattr(fn, "__qualname__", None) or repr(fn)
        rec = ipc_log()
        before = _kicad_pids() if rec is not None else self.known_pids
        started = time.monotonic()
        try:
            result = self._call(fn, retries=retries)
        except LayerError as err:
            transport = isinstance(err, Unreachable) or (err.code == IPC_BUSY and "did not answer in time" in str(err))
            if transport:
                after = _kicad_pids()
                err.data.setdefault("kicad_pids_before", before)
                err.data["kicad_pids_after"] = after
                gone = sorted(set(before or []) - set(after)) if before is not None else []
                log.warning("IPC transport failure in %s after %.2f s: %s; KiCad pids before %s, after %s", name, time.monotonic() - started, err.code, before, after)
                if err.code == IPC_BUSY and (gone or (before is None and not after)):
                    self.reset()
                    crashed = Unreachable(
                        KICAD_NOT_RUNNING,
                        "KiCad exited while the request was pending" + (f" (pid {', '.join(map(str, gone))} is gone)" if gone else "") + ": it probably crashed.",
                        hint="Reopen the board in KiCad (its autosave may hold recent edits) and retry; KICAD_LAYER_IPC_LOG=1 logs every request for a report.",
                        data=dict(err.data),
                    )
                    if rec is not None:
                        rec.info("%s FAILED %s %.3fs pids %s -> %s", name, crashed.code, time.monotonic() - started, before, after)
                    raise crashed from err
            if rec is not None:
                rec.info("%s FAILED %s %.3fs pids %s -> %s", name, err.code, time.monotonic() - started, before, err.data.get("kicad_pids_after"))
            raise
        if rec is not None:
            rec.info("%s ok items=%s %.3fs", name, _count(result), time.monotonic() - started)
        return result

    def _call(self, fn: Callable[[], T], *, retries: int = 2) -> T:
        from kipy.errors import ApiError
        from kipy.proto.common.envelope_pb2 import ApiStatusCode as S

        attempt = 0
        while True:
            with self._lock:
                try:
                    return fn()
                except ApiError as exc:
                    if exc.code == S.AS_TOKEN_MISMATCH and attempt == 0:
                        log.info("token mismatch: KiCad restarted, reconnecting")
                        self.reset()
                        attempt += 1
                        continue
                    if exc.code in (S.AS_BUSY, S.AS_NOT_READY) and attempt < retries:
                        attempt += 1
                        time.sleep(0.4 * attempt)
                        continue
                    raise classify(exc) from exc
                except LayerError:
                    raise
                except Exception as exc:
                    err = classify(exc)
                    if isinstance(err, Unreachable):
                        self.reset()
                    raise err from exc

    # -- documents ------------------------------------------------------------------

    def open_boards(self) -> list[tuple[Any, Path]]:
        """Every board open in a PCB Editor window, as (DocumentSpecifier, path)."""
        from kipy.proto.common.types.base_types_pb2 import DOCTYPE_PCB

        def work() -> list[tuple[Any, Path]]:
            docs = list(self.kicad().get_open_documents(DOCTYPE_PCB))
            out = []
            for d in docs:
                project_dir = getattr(getattr(d, "project", None), "path", "") or ""
                name = getattr(d, "board_filename", "") or ""
                out.append((d, Path(project_dir) / name if project_dir else Path(name)))
            return out

        return self.call(work, label="get_open_documents")

    def board(self, board_path: Path | None = None) -> tuple[Any, Path]:
        """Bind a kipy Board to the requested open board (or the only open one)."""
        from kipy.board import Board

        try:
            docs = self.open_boards()
        except Rejected as exc:
            if exc.code == IPC_REJECTED and "no handler" in str(exc):
                raise Rejected(
                    BOARD_NOT_OPEN,
                    "KiCad's API answers, but no PCB Editor window is open.",
                    hint="Open the PCB Editor from the project window, then retry.",
                ) from exc
            raise
        if not docs:
            raise Rejected(
                BOARD_NOT_OPEN,
                "No board is open in KiCad's PCB Editor.",
                hint="Open the PCB Editor from the project window, then retry.",
            )
        if board_path is None:
            if len(docs) > 1:
                raise Rejected(
                    BOARD_NOT_OPEN,
                    f"Several boards are open: {', '.join(str(p) for _, p in docs)}.",
                    hint="Pass board_path to choose one.",
                )
            doc, path = docs[0]
        else:
            want = _canonical(board_path)
            match = [(d, p) for d, p in docs if _canonical(p) == want]
            if not match:
                raise Rejected(
                    BOARD_NOT_OPEN,
                    f"{board_path.name} is not open in the PCB Editor. Open boards: "
                    f"{', '.join(str(p) for _, p in docs) or 'none'}.",
                    hint="Open that board in KiCad, or omit board_path to use the open one.",
                )
            doc, path = match[0]
        self.seen_live.add(_canonical(path))
        kicad = self.kicad()
        client = getattr(kicad, "_client", None) or getattr(kicad, "client", None)
        if client is None:  # pragma: no cover - kipy internals changed
            return kicad.get_board(), path
        return Board(client, doc), path

    def has_seen_live(self, path: os.PathLike[str] | str) -> bool:
        return _canonical(path) in self.seen_live


_session: Session | None = None
_session_lock = threading.Lock()


def get_session() -> Session:
    global _session
    with _session_lock:
        if _session is None:
            _session = Session()
        return _session


def reset_session() -> None:
    global _session
    with _session_lock:
        _session = None
