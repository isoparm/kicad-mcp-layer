"""Failure classification for the IPC channel, without KiCad."""

import pytest

from kicad_layer.errors import (
    IPC_BUSY,
    IPC_REJECTED,
    KICAD_API_DISABLED,
    KICAD_NOT_RUNNING,
)
from kicad_layer.ipc import session as ipc_session
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
