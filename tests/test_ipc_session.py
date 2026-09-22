"""Failure classification for the IPC channel, without KiCad."""

import pytest

from kicad_layer.errors import (
    IPC_BUSY,
    IPC_REJECTED,
    KICAD_API_DISABLED,
    KICAD_NOT_RUNNING,
)
from kicad_layer.ipc import session as ipc_session
from kicad_layer.errors import LayerError
from kicad_layer.ipc.session import Rejected, Unreachable, classify


@pytest.fixture
def no_kicad(monkeypatch):
    monkeypatch.setattr(ipc_session, "kicad_processes", lambda: [])


@pytest.fixture
def kicad_running(monkeypatch):
    from kicad_layer.models import ProcessInfo

    monkeypatch.setattr(ipc_session, "kicad_processes", lambda: [ProcessInfo(pid=1, name="kicad.exe")])


def test_connection_refused_without_process_is_not_running(no_kicad):
    from kipy.errors import ConnectionError as KipyConnectionError

    err = classify(KipyConnectionError("Failed to connect to KiCad: Connection refused"))
    assert isinstance(err, Unreachable)
    assert err.code == KICAD_NOT_RUNNING


def test_connection_refused_with_process_is_api_disabled(kicad_running):
    from kipy.errors import ConnectionError as KipyConnectionError

    err = classify(KipyConnectionError("Failed to connect to KiCad: Connection refused"))
    assert isinstance(err, Unreachable)
    assert err.code == KICAD_API_DISABLED
    assert "Enable KiCad API" in str(err)


def test_transport_timeout_is_rejected_and_retryable(kicad_running):
    from kipy.errors import ConnectionError as KipyConnectionError

    err = classify(KipyConnectionError("Error receiving reply from KiCad: Timed out"))
    assert isinstance(err, Rejected)
    assert err.code == IPC_BUSY
    assert err.retryable


@pytest.mark.parametrize(
    "status, code, retryable",
    [
        ("AS_BUSY", IPC_BUSY, True),
        ("AS_NOT_READY", IPC_BUSY, True),
        ("AS_TOKEN_MISMATCH", IPC_REJECTED, True),
        ("AS_UNHANDLED", IPC_REJECTED, False),
        ("AS_UNIMPLEMENTED", IPC_REJECTED, False),
        ("AS_BAD_REQUEST", IPC_REJECTED, False),
    ],
)
def test_api_errors_are_rejected(status, code, retryable):
    from kipy.errors import ApiError
    from kipy.proto.common.envelope_pb2 import ApiStatusCode as S

    err = classify(ApiError("KiCad returned error: boom", code=getattr(S, status)))
    assert isinstance(err, Rejected)
    assert err.code == code
    assert err.retryable is retryable


def test_layer_errors_pass_through():
    from kicad_layer.errors import LayerError

    original = LayerError("X", "already classified")
    assert classify(original) is original


# --------------------------------------------------------------------------------------
# crash instrumentation, stale locks, fallback to the file
# --------------------------------------------------------------------------------------

import json
import socket
import subprocess
import sys

from kicad_layer import locks
from kicad_layer.config import load_settings, set_settings


@pytest.fixture
def cache(tmp_path, monkeypatch):
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache"), "KICAD_LAYER_MODE": "write"}))
    ipc_session.reset_session()
    yield tmp_path
    set_settings(None)
    ipc_session.reset_session()


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_unhandled_hint_names_a_busy_or_crashed_editor_not_kicad_11():
    from kipy.errors import ApiError
    from kipy.proto.common.envelope_pb2 import ApiStatusCode as S

    err = classify(ApiError("no handler", code=S.AS_UNHANDLED))
    assert "KiCad 11" not in str(err) and "crashed" in err.hint and "busy" in err.hint


def test_pipe_errors_are_transport_failures(no_kicad):
    err = classify(ConnectionResetError("pipe closed"))
    assert isinstance(err, Unreachable) and err.code == KICAD_NOT_RUNNING


def test_requests_are_logged_when_asked(cache, monkeypatch):
    monkeypatch.setenv("KICAD_LAYER_IPC_LOG", "1")
    monkeypatch.setattr(locks, "kicad_pids", lambda: [4242])
    s = ipc_session.Session()
    assert s.call(lambda: [1, 2, 3], label="get_items") == [1, 2, 3]
    with pytest.raises(Unreachable):
        s.call(lambda: (_ for _ in ()).throw(Unreachable(KICAD_NOT_RUNNING, "gone")), label="create_items")
    for h in ipc_session.ipc_log().handlers:
        h.flush()
    text = (cache / "cache" / "logs" / "ipc.log").read_text(encoding="utf-8")
    assert "get_items ok items=3" in text and "create_items FAILED KICAD_NOT_RUNNING" in text and "[4242]" in text


def test_nothing_is_logged_by_default(cache, monkeypatch):
    monkeypatch.delenv("KICAD_LAYER_IPC_LOG", raising=False)
    assert ipc_session.ipc_log() is None
    assert ipc_session.Session().call(lambda: 5) == 5
    assert not (cache / "cache" / "logs" / "ipc.log").exists()


def test_timeout_while_kicad_vanishes_is_a_crash(cache, monkeypatch):
    from kipy.errors import ConnectionError as KipyConnectionError

    monkeypatch.setattr(ipc_session, "kicad_processes", lambda: [])
    pids = iter([[1234], []])  # before the call, after it
    monkeypatch.setenv("KICAD_LAYER_IPC_LOG", "1")
    monkeypatch.setattr(locks, "kicad_pids", lambda: next(pids))

    def hang():
        raise KipyConnectionError("Error receiving reply from KiCad: Timed out")

    with pytest.raises(Unreachable) as exc:
        ipc_session.Session().call(hang, label="commit: move")
    assert exc.value.code == KICAD_NOT_RUNNING and "1234" in str(exc.value)
    assert exc.value.data["kicad_pids_before"] == [1234] and exc.value.data["kicad_pids_after"] == []


def test_timeout_with_kicad_alive_stays_busy(cache, monkeypatch):
    from kipy.errors import ConnectionError as KipyConnectionError

    monkeypatch.setattr(ipc_session, "kicad_processes", lambda: [])
    monkeypatch.setattr(locks, "kicad_pids", lambda: [1234])
    s = ipc_session.Session()
    s.known_pids = [1234]
    with pytest.raises(Rejected) as exc:
        s.call(lambda: (_ for _ in ()).throw(KipyConnectionError("Timed out")))
    assert exc.value.code == IPC_BUSY and exc.value.data["kicad_pids_after"] == [1234]


def test_lock_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(locks, "kicad_pids", lambda: [])
    lock = tmp_path / "~b.kicad_pcb.lck"
    lock.write_text(json.dumps({"hostname": socket.gethostname(), "username": "u", "pid": _dead_pid()}))
    assert not locks.owner_alive(lock)
    lock.write_text(json.dumps({"hostname": socket.gethostname(), "username": "u", "pid": __import__("os").getpid()}))
    assert locks.owner_alive(lock)
    lock.write_text(json.dumps({"hostname": "some-other-host", "username": "u"}))
    assert locks.owner_alive(lock), "another host's lock is never called orphaned"
    lock.write_text(json.dumps({"hostname": socket.gethostname(), "username": "u"}))
    assert not locks.owner_alive(lock)
    monkeypatch.setattr(locks, "kicad_pids", lambda: [99])
    assert locks.owner_alive(lock), "no pid in the lock and KiCad runs here: assume it holds the file"


def test_orphan_locks_are_removed_at_start(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(locks, "kicad_pids", lambda: [])
    proj = tmp_path / "boards" / "p"
    proj.mkdir(parents=True)
    dead = proj / "~p.kicad_pcb.lck"
    dead.write_text(json.dumps({"hostname": socket.gethostname(), "username": "u", "pid": _dead_pid()}))
    alive = proj / "~p.kicad_sch.lck"
    alive.write_text(json.dumps({"hostname": socket.gethostname(), "username": "u", "pid": __import__("os").getpid()}))
    remote = proj / "~q.kicad_pcb.lck"
    remote.write_text(json.dumps({"hostname": "elsewhere", "username": "u"}))
    other = proj / "~notes.txt.lck"
    other.write_text("{}")
    with caplog.at_level("WARNING", logger="kicad_layer.locks"):
        removed = locks.clean_orphan_locks(tmp_path)
    assert removed == [dead] and not dead.exists() and alive.exists() and remote.exists() and other.exists()
    assert "removed orphan KiCad lock" in caplog.text


@pytest.fixture
def board_copy(cache):
    from tests.conftest import copy_project

    return copy_project("pic_programmer", cache / "pic") / "pic_programmer.kicad_pcb"


def _no_kicad(monkeypatch, open_paths=None):
    """The API unreachable (KiCad gone) or, with open_paths, answering with those boards."""
    monkeypatch.setattr(locks, "kicad_pids", lambda: [])
    monkeypatch.setattr(ipc_session, "kicad_processes", lambda: [])

    def open_boards(self):
        if open_paths is None:
            raise Unreachable(KICAD_NOT_RUNNING, "KiCad is not running.")
        return [(None, p) for p in open_paths]

    monkeypatch.setattr(ipc_session.Session, "open_boards", open_boards)


def test_force_bypasses_a_dead_owners_lock_only(board_copy, monkeypatch):
    from kicad_layer import pcb_tools
    from kicad_layer.errors import EDIT_CONFLICT

    _no_kicad(monkeypatch)
    lock = locks.lock_path(board_copy)
    lock.write_text(json.dumps({"hostname": socket.gethostname(), "username": "u", "pid": _dead_pid()}))
    with pytest.raises(LayerError) as exc:
        pcb_tools.add_via(str(board_copy), 100, 60, net="GND", channel="file")
    assert exc.value.code == EDIT_CONFLICT and "force" in exc.value.hint
    res = pcb_tools.add_via(str(board_copy), 100, 60, net="GND", channel="file", force=True)
    assert res.changed and res.channel == "file" and any("stale" in w for w in res.warnings)
    lock.write_text(json.dumps({"hostname": socket.gethostname(), "username": "u", "pid": __import__("os").getpid()}))
    with pytest.raises(LayerError) as exc:
        pcb_tools.add_via(str(board_copy), 101, 60, net="GND", channel="file", force=True)
    assert exc.value.code == EDIT_CONFLICT, "force never overrides a lock a running KiCad holds"


def test_auto_falls_back_to_the_file_when_kicad_crashed(board_copy, monkeypatch):
    from kicad_layer import pcb_tools
    from kicad_layer.errors import EDIT_CONFLICT

    ipc_session.get_session().seen_live.add(ipc_session._canonical(board_copy))
    _no_kicad(monkeypatch)
    locks.lock_path(board_copy).write_text(json.dumps({"hostname": socket.gethostname(), "username": "u"}))  # left by the crash
    with pytest.raises(LayerError) as exc:
        pcb_tools.add_via(str(board_copy), 100, 60, net="GND", channel="file")
    assert exc.value.code == EDIT_CONFLICT, "an explicit file request on a board seen live still refuses without force"
    res = pcb_tools.add_via(str(board_copy), 100, 60, net="GND", channel="auto")
    assert res.channel == "file" and res.changed and any("crashed" in w for w in res.warnings)
    # while KiCad runs and holds the lock, auto never touches the file
    monkeypatch.setattr(locks, "kicad_pids", lambda: [77])
    with pytest.raises(LayerError) as exc:
        pcb_tools.add_via(str(board_copy), 102, 60, net="GND", channel="auto")
    assert exc.value.code == EDIT_CONFLICT


def test_live_edit_that_dies_mid_call_falls_back(board_copy, monkeypatch):
    from kicad_layer import pcb_tools
    from kicad_layer.ipc import board_write

    _no_kicad(monkeypatch, open_paths=[board_copy])

    def crash(*a, **k):
        ipc_session.get_session().seen_live.add(ipc_session._canonical(board_copy))
        raise Unreachable(KICAD_NOT_RUNNING, "KiCad exited while the request was pending: it probably crashed.")

    monkeypatch.setattr(board_write, "add_via", crash)
    res = pcb_tools.add_via(str(board_copy), 100, 60, net="GND", channel="auto")
    assert res.channel == "file" and res.warnings[0].startswith("The live edit failed (KICAD_NOT_RUNNING)")
    monkeypatch.setattr(locks, "kicad_pids", lambda: [5])  # KiCad back up (or never gone): no fallback
    with pytest.raises(Unreachable):
        pcb_tools.add_via(str(board_copy), 101, 60, net="GND", channel="auto")
