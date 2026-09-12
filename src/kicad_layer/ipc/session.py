"""One session to KiCad per process, with every failure classified.

The rule that keeps file edits safe: a failure is either **Unreachable** (no address, the
connection or the send itself failed) or **Rejected** (the request was delivered and KiCad
said no, including timeouts). Only Unreachable may ever justify touching a board file on
disk, and never for a board this process has already seen live.

KiCad serialises requests anyway, so one lock guards every call. Connecting runs on a
helper thread with a bound, because a dial can hang when a pipe exists but nobody answers.
"""

from __future__ import annotations

import logging
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
                hint="A modal dialog or an interactive tool may be open in KiCad; finish it and retry.",
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
                hint="The PCB Editor window is probably not open. Some requests also need KiCad 11.",
            )
        if code == S.AS_UNIMPLEMENTED:
            return Rejected(IPC_REJECTED, f"KiCad 10 does not implement this request: {exc}")
        return Rejected(IPC_REJECTED, f"KiCad rejected the request: {exc}")
    return Rejected(IPC_REJECTED, f"{type(exc).__name__}: {exc}")


def _canonical(path: os.PathLike[str] | str) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


class Session:
    """Process-wide connection state. Use :func:`get_session`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._kicad: Any = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kicad-ipc-connect")
        self.seen_live: set[str] = set()

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
                log.info("connected to KiCad at %s", resolved_address()[0])
            return self._kicad

    def reset(self) -> None:
        with self._lock:
            self._kicad = None

    # -- guarded calls --------------------------------------------------------------

    def call(self, fn: Callable[[], T], *, retries: int = 2) -> T:
        """Run ``fn`` under the session lock, classifying failures and retrying busy states."""
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

        return self.call(work)

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
