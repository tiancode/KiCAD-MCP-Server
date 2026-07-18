"""Truthful quit (E2E P3, fix E): run_action('*.quit') returns RAS_OK even when
KiCad never quits (verified no-op on 10.0.4).  handle_run_action must verify the
process really exited before reporting success, and the file-open-forward spawn
must record its PID so manage_kicad_ui(action=quit) can terminate the second
instance the server opened.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _make_iface(backend):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    iface.ipc_backend = backend
    iface.ipc_board_api = None
    iface.board = None
    iface.command_routes = {}
    iface._board_disk_signature = None
    iface._current_project_path = None
    iface._last_auto_save_status = None
    return iface


def _quit_backend():
    backend = MagicMock()
    backend.run_action.return_value = {
        "success": True,
        "action": "common.Control.quit",
        "status": 1,
        "statusName": "RAS_OK",
    }
    return backend


# ---------------------------------------------------------------------------
# handle_run_action quit verification
# ---------------------------------------------------------------------------
def test_quit_ras_ok_but_process_alive_reports_failure(monkeypatch):
    from handlers import ui

    monkeypatch.setattr(ui, "_ipc_still_alive_after", lambda iface, timeout_s=3.0: True)
    iface = _make_iface(_quit_backend())

    out = ui.handle_run_action(iface, {"action": "common.Control.quit"})

    assert out["success"] is False
    assert out["quitVerified"] is False
    assert out["errorCode"] == "QUIT_NOOP"
    assert "no-op" in out["message"].lower()
    assert "manage_kicad_ui(action=quit)" in out["message"]


def test_quit_ras_ok_and_process_gone_reports_success(monkeypatch):
    from handlers import ui

    monkeypatch.setattr(ui, "_ipc_still_alive_after", lambda iface, timeout_s=3.0: False)
    iface = _make_iface(_quit_backend())

    out = ui.handle_run_action(iface, {"action": "common.Control.quit"})

    assert out["success"] is True
    assert out["quitVerified"] is True
    assert "errorCode" not in out


def test_non_quit_action_not_verified(monkeypatch):
    """A normal action (zoomFit) must not trigger the quit-verification wait."""
    from handlers import ui

    called = {"n": 0}
    monkeypatch.setattr(
        ui, "_ipc_still_alive_after", lambda *a, **k: called.__setitem__("n", called["n"] + 1)
    )
    backend = MagicMock()
    backend.run_action.return_value = {
        "success": True,
        "action": "common.Control.zoomFitScreen",
        "status": 1,
        "statusName": "RAS_OK",
    }
    iface = _make_iface(backend)

    out = ui.handle_run_action(iface, {"action": "common.Control.zoomFitScreen"})

    assert out["success"] is True
    assert "quitVerified" not in out
    assert called["n"] == 0


def test_quit_noop_errorcode_survives_enrich_failure(monkeypatch):
    from handlers import ui
    from utils.failure import enrich_failure

    monkeypatch.setattr(ui, "_ipc_still_alive_after", lambda iface, timeout_s=3.0: True)
    iface = _make_iface(_quit_backend())

    out = enrich_failure(
        "run_action", ui.handle_run_action(iface, {"action": "common.Control.quit"})
    )

    assert out["errorCode"] == "QUIT_NOOP"


# ---------------------------------------------------------------------------
# _ipc_still_alive_after
# ---------------------------------------------------------------------------
def test_still_alive_returns_false_when_socket_dies_fast():
    from handlers.ui import _ipc_still_alive_after

    backend = MagicMock()
    backend.is_connected.return_value = False
    iface = _make_iface(backend)

    assert _ipc_still_alive_after(iface, timeout_s=1.0) is False


def test_still_alive_returns_true_when_connection_persists():
    from handlers.ui import _ipc_still_alive_after

    backend = MagicMock()
    backend.is_connected.return_value = True
    iface = _make_iface(backend)

    assert _ipc_still_alive_after(iface, timeout_s=0.3) is True


def test_still_alive_true_when_no_backend_to_probe():
    """Unverifiable → assume still alive so a quit is never falsely 'confirmed'."""
    from handlers.ui import _ipc_still_alive_after

    iface = _make_iface(None)
    assert _ipc_still_alive_after(iface, timeout_s=0.1) is True


# ---------------------------------------------------------------------------
# file-open-forward spawn records the second instance's PID for quit
# ---------------------------------------------------------------------------
def test_spawned_board_editor_pid_is_recorded(monkeypatch, tmp_path):
    from handlers import ui
    from utils.kicad_process import KiCADProcessManager

    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)\n", encoding="utf-8")

    iface = _make_iface(None)
    # No IPC, no already-open short-circuit, no lockfile → spawn path.
    monkeypatch.setattr(ui, "_path_already_open", lambda i, p: False)
    monkeypatch.setattr(ui, "_board_lock_present", lambda p: False)
    monkeypatch.setattr(
        KiCADProcessManager, "get_pcb_editor_command", staticmethod(lambda p: ["pcbnew", str(p)])
    )

    class _FakeProc:
        pid = 34567

    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: _FakeProc())
    KiCADProcessManager._launched_pids = set()

    out = ui._forward_file_open_to_running_kicad(iface, board)

    assert out["fileOpenMethod"] == "spawn"
    # The second-instance PID is now tracked so quit can target it.
    assert 34567 in KiCADProcessManager._launched_pids
