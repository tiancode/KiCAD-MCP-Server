"""Socket auto-detect scans per-instance sockets and prefers the project's
board (fix B), and KICAD_API_SOCKET is an authoritative override (fix C).

A second KiCad instance serves its own ``api-<pid>.sock``; the connect-time
selection must be able to pick the instance whose open board matches the
project the MCP session is on, instead of stopping at the first instance with
ANY board (which pinned IPC to the wrong project in the E2E run).
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from kicad_api.ipc_backend import IPCBackend  # noqa: E402
from kicad_api.ipc_backend import _backend as backend_mod  # noqa: E402
from kicad_api.ipc_backend import _helpers as helpers_mod  # noqa: E402


def _install_fake_kipy(monkeypatch, live_sockets):
    """Fake kipy where only ``live_sockets`` (uri -> [board paths]) answer ping."""

    class FakeClient:
        def __init__(self, socket_path=None):
            self.socket_path = socket_path

        def ping(self):
            if self.socket_path not in live_sockets:
                raise RuntimeError(f"Connection refused: {self.socket_path}")

    fake_kipy = types.ModuleType("kipy")
    fake_kipy.KiCad = FakeClient
    monkeypatch.setitem(sys.modules, "kipy", fake_kipy)
    monkeypatch.setattr(
        backend_mod,
        "open_pcb_document_paths",
        lambda client: live_sockets.get(getattr(client, "socket_path", None), []),
    )
    # Deterministic socket dirs: only /tmp/kicad, with one PID-suffixed sibling.
    monkeypatch.setattr(backend_mod.platform, "system", lambda: "Linux")

    def _fake_glob(pattern):
        if pattern == os.path.join("/tmp/kicad", "api-*.sock"):
            return ["/tmp/kicad/api-222.sock"]
        return []

    monkeypatch.setattr("glob.glob", _fake_glob)
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    return FakeClient


# ---------------------------------------------------------------------------
# Fix B — prefer the instance whose open board matches the project
# ---------------------------------------------------------------------------
def test_connect_prefers_instance_with_matching_board(monkeypatch):
    _install_fake_kipy(
        monkeypatch,
        {
            "ipc:///tmp/kicad/api.sock": ["/proj/a/a.kicad_pcb"],  # first, wrong board
            "ipc:///tmp/kicad/api-222.sock": ["/proj/b/b.kicad_pcb"],  # sibling, right board
        },
    )
    be = IPCBackend()

    assert be.connect(prefer_board_path="/proj/b/b.kicad_pcb") is True
    assert be._kicad.socket_path == "ipc:///tmp/kicad/api-222.sock"


def test_connect_without_preference_takes_first_with_board(monkeypatch):
    _install_fake_kipy(
        monkeypatch,
        {
            "ipc:///tmp/kicad/api.sock": ["/proj/a/a.kicad_pcb"],
            "ipc:///tmp/kicad/api-222.sock": ["/proj/b/b.kicad_pcb"],
        },
    )
    be = IPCBackend()

    assert be.connect() is True
    # Legacy behaviour: first instance with any board open.
    assert be._kicad.socket_path == "ipc:///tmp/kicad/api.sock"


def test_connect_falls_back_when_preferred_board_absent(monkeypatch):
    """Preferred board isn't open anywhere → still connect (first-with-board),
    so the board-identity gate can then refuse rather than the connect failing."""
    _install_fake_kipy(
        monkeypatch,
        {
            "ipc:///tmp/kicad/api.sock": ["/proj/a/a.kicad_pcb"],
            "ipc:///tmp/kicad/api-222.sock": ["/proj/a/a.kicad_pcb"],
        },
    )
    be = IPCBackend()

    assert be.connect(prefer_board_path="/proj/z/z.kicad_pcb") is True
    assert be._kicad.socket_path == "ipc:///tmp/kicad/api.sock"


def test_connect_skips_dead_pid_sockets(monkeypatch):
    """A stale/dead api-<pid>.sock is skipped gracefully; the reachable one wins."""
    _install_fake_kipy(
        monkeypatch,
        # api.sock is dead; only the sibling answers.
        {"ipc:///tmp/kicad/api-222.sock": ["/proj/b/b.kicad_pcb"]},
    )
    be = IPCBackend()

    assert be.connect(prefer_board_path="/proj/b/b.kicad_pcb") is True
    assert be._kicad.socket_path == "ipc:///tmp/kicad/api-222.sock"


# ---------------------------------------------------------------------------
# reselect_preferring_board with a board preference
# ---------------------------------------------------------------------------
def test_reselect_noop_when_already_on_preferred_board(monkeypatch):
    be = IPCBackend()
    be._kicad = object()
    be._connected = True
    monkeypatch.setattr(backend_mod, "open_pcb_document_paths", lambda k: ["/proj/b/b.kicad_pcb"])
    from unittest.mock import MagicMock

    be.connect = MagicMock()  # type: ignore[assignment]

    assert be.reselect_preferring_board(prefer_board_path="/proj/b/b.kicad_pcb") is True
    be.connect.assert_not_called()


def test_reselect_reconnects_when_on_wrong_board(monkeypatch):
    be = IPCBackend()
    be._kicad = object()
    be._connected = True
    # Current instance on the wrong board; after reconnect it's on the right one.
    seq = iter([["/proj/a/a.kicad_pcb"], ["/proj/b/b.kicad_pcb"]])
    monkeypatch.setattr(backend_mod, "open_pcb_document_paths", lambda k: next(seq))
    from unittest.mock import MagicMock

    def _fake_connect(prefer_board_path=None):
        be._kicad = object()
        be._connected = True

    be.connect = MagicMock(side_effect=_fake_connect)  # type: ignore[assignment]

    assert be.reselect_preferring_board(prefer_board_path="/proj/b/b.kicad_pcb") is True
    be.connect.assert_called_once()


def test_reselect_false_when_preferred_board_unreachable(monkeypatch):
    be = IPCBackend()
    be._kicad = object()
    be._connected = True
    monkeypatch.setattr(backend_mod, "open_pcb_document_paths", lambda k: ["/proj/a/a.kicad_pcb"])
    from unittest.mock import MagicMock

    def _fake_connect(prefer_board_path=None):
        be._kicad = object()
        be._connected = True

    be.connect = MagicMock(side_effect=_fake_connect)  # type: ignore[assignment]

    assert be.reselect_preferring_board(prefer_board_path="/proj/b/b.kicad_pcb") is False


# ---------------------------------------------------------------------------
# Fix C — KICAD_API_SOCKET is authoritative
# ---------------------------------------------------------------------------
def test_kicad_api_socket_wins_over_default(monkeypatch):
    _install_fake_kipy(
        monkeypatch,
        {
            "ipc:///tmp/kicad/api.sock": ["/proj/a/a.kicad_pcb"],  # default, has a board
            "ipc:///tmp/kicad/api-999.sock": ["/proj/b/b.kicad_pcb"],  # the override target
        },
    )
    monkeypatch.setenv("KICAD_API_SOCKET", "ipc:///tmp/kicad/api-999.sock")
    be = IPCBackend()

    assert be.connect() is True
    # The env override beat the default api.sock instance (the documented
    # contract; previously the default won and the override was a no-op).
    assert be._kicad.socket_path == "ipc:///tmp/kicad/api-999.sock"


def test_kicad_api_socket_unreachable_raises_naming_the_socket(monkeypatch):
    _install_fake_kipy(
        monkeypatch,
        {"ipc:///tmp/kicad/api.sock": ["/proj/a/a.kicad_pcb"]},  # override NOT live
    )
    monkeypatch.setenv("KICAD_API_SOCKET", "ipc:///tmp/kicad/api-nope.sock")
    be = IPCBackend()

    with pytest.raises(Exception) as exc:
        be.connect()
    # Authoritative failure names the socket instead of silently attaching to
    # the reachable default.
    assert "KICAD_API_SOCKET" in str(exc.value)
    assert be._kicad is None or not be._connected


# ---------------------------------------------------------------------------
# Pure helper unit tests
# ---------------------------------------------------------------------------
def test_doc_board_path_stitches_project_dir_and_filename():
    doc = types.SimpleNamespace(
        board_filename="board.kicad_pcb",
        project=types.SimpleNamespace(path="/home/u/proj"),
    )
    assert helpers_mod._doc_board_path(doc) == "/home/u/proj/board.kicad_pcb"


def test_doc_board_path_absolute_filename_used_as_is():
    doc = types.SimpleNamespace(
        board_filename="/abs/board.kicad_pcb", project=types.SimpleNamespace(path="/ignored")
    )
    assert helpers_mod._doc_board_path(doc) == "/abs/board.kicad_pcb"


def test_doc_board_path_non_pcb_returns_empty():
    doc = types.SimpleNamespace(
        board_filename="sheet.kicad_sch", project=types.SimpleNamespace(path="/p")
    )
    assert helpers_mod._doc_board_path(doc) == ""


def test_open_pcb_document_paths_filters_and_collects():
    docs = [
        types.SimpleNamespace(
            board_filename="a.kicad_pcb", project=types.SimpleNamespace(path="/p")
        ),
        types.SimpleNamespace(
            board_filename="a.kicad_sch", project=types.SimpleNamespace(path="/p")
        ),
    ]
    kicad = types.SimpleNamespace(get_open_documents=lambda dt=None: docs)
    assert helpers_mod.open_pcb_document_paths(kicad) == ["/p/a.kicad_pcb"]


def test_normalize_board_path_handles_relative_and_bad_input():
    assert helpers_mod.normalize_board_path(None) is None
    assert helpers_mod.normalize_board_path("") is None
    assert helpers_mod.normalize_board_path(123) is None
    # Absolute path round-trips (realpath/normcase idempotent for existing dirs).
    assert helpers_mod.normalize_board_path("/tmp/x.kicad_pcb") == os.path.normcase(
        os.path.realpath("/tmp/x.kicad_pcb")
    )
