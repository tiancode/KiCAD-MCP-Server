"""Tests for handle_run_action (escape hatch into KiCad TOOL_ACTIONs)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _make_iface(ipc_backend=None, use_ipc=True):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = use_ipc
    iface.ipc_backend = ipc_backend
    iface.ipc_board_api = None
    iface.board = None
    iface.command_routes = {}
    iface._board_disk_signature = None
    iface._current_project_path = None
    iface._last_auto_save_status = None
    return iface


def test_run_action_forwards_to_ipc_backend():
    backend = MagicMock()
    backend.run_action.return_value = {
        "success": True,
        "action": "pcbnew.EditorControl.zoomFitScreen",
        "status": 1,
        "statusName": "RAS_OK",
    }
    iface = _make_iface(backend)
    out = iface._handle_run_action({"action": "pcbnew.EditorControl.zoomFitScreen"})
    assert out["success"] is True
    assert out["statusName"] == "RAS_OK"
    backend.run_action.assert_called_once_with("pcbnew.EditorControl.zoomFitScreen")


def test_run_action_propagates_invalid_status():
    backend = MagicMock()
    backend.run_action.return_value = {
        "success": False,
        "action": "bogus.action",
        "status": 2,
        "statusName": "RAS_INVALID",
    }
    iface = _make_iface(backend)
    out = iface._handle_run_action({"action": "bogus.action"})
    assert out["success"] is False
    assert out["statusName"] == "RAS_INVALID"


def test_run_action_rejects_missing_action_param():
    backend = MagicMock()
    iface = _make_iface(backend)
    out = iface._handle_run_action({})
    assert out["success"] is False
    assert "action" in out["message"].lower()
    backend.run_action.assert_not_called()


def test_run_action_rejects_non_string_action():
    backend = MagicMock()
    iface = _make_iface(backend)
    out = iface._handle_run_action({"action": 42})
    assert out["success"] is False
    backend.run_action.assert_not_called()


def test_run_action_requires_ipc_backend():
    iface = _make_iface(ipc_backend=None, use_ipc=False)
    # Stub ensure_ipc so the auto-recovery path doesn't connect to a real
    # KiCAD instance — we want to verify the gating message here.
    iface.ensure_ipc = lambda **kw: (False, "ipc disabled in test")
    out = iface._handle_run_action({"action": "pcbnew.EditorControl.zoomFitScreen"})
    assert out["success"] is False
    assert "IPC" in out["message"]


def test_run_action_surfaces_backend_exceptions():
    backend = MagicMock()
    backend.run_action.side_effect = RuntimeError("socket closed")
    iface = _make_iface(backend)
    out = iface._handle_run_action({"action": "any"})
    assert out["success"] is False
    assert "socket closed" in out["message"]
