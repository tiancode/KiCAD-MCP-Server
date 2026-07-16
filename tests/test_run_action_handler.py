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
    out = iface._handle_run_action({"action": "common.Control.zoomFitScreen"})
    assert out["success"] is False
    assert "IPC" in out["message"]


# --- C8: run_action must NOT auto-launch the GUI by default ------------------


def test_run_action_does_not_autolaunch_by_default():
    """C8: with IPC down and no allowLaunch, ensure_ipc is called allow_launch=False."""
    iface = _make_iface(ipc_backend=None, use_ipc=False)
    seen = {}

    def fake_ensure_ipc(**kw):
        seen.update(kw)
        return (False, "KiCad isn't running")

    iface.ensure_ipc = fake_ensure_ipc
    out = iface._handle_run_action({"action": "common.Control.zoomFitScreen"})
    assert out["success"] is False
    assert seen.get("allow_launch") is False, "default must not auto-launch the GUI"
    assert seen.get("require_pcb_editor") is False
    assert "allowLaunch:true" in out["message"]


def test_run_action_allowlaunch_true_opts_into_launch():
    """C8: allowLaunch:true forwards allow_launch=True to ensure_ipc."""
    iface = _make_iface(ipc_backend=None, use_ipc=False)
    seen = {}

    def fake_ensure_ipc(**kw):
        seen.update(kw)
        return (False, "launch attempted but failed")

    iface.ensure_ipc = fake_ensure_ipc
    out = iface._handle_run_action({"action": "common.Control.zoomFitScreen", "allowLaunch": True})
    assert seen.get("allow_launch") is True
    # No opt-in hint when the caller already opted in.
    assert "allowLaunch:true" not in out["message"]


def test_run_action_refusal_gets_ipc_required_errorcode():
    """C8: the refusal is classified IPC_REQUIRED (not INTERNAL_ERROR)."""
    from utils.failure import enrich_failure

    iface = _make_iface(ipc_backend=None, use_ipc=False)
    iface.ensure_ipc = lambda **kw: (False, "KiCad isn't running")
    out = iface._handle_run_action({"action": "common.Control.zoomFitScreen"})
    enriched = enrich_failure("run_action", out)
    assert enriched["errorCode"] == "IPC_REQUIRED"


# --- D5: RAS_INVALID → INVALID_ACTION errorCode, statusName preserved --------


def test_run_action_invalid_status_maps_to_invalid_action():
    from utils.failure import enrich_failure

    backend = MagicMock()
    backend.run_action.return_value = {
        "success": False,
        "action": "cvpcb",
        "status": 2,
        "statusName": "RAS_INVALID",
    }
    iface = _make_iface(backend)
    out = iface._handle_run_action({"action": "cvpcb"})
    assert out["errorCode"] == "INVALID_ACTION"
    assert out["statusName"] == "RAS_INVALID"  # retry contract preserved
    # enrich_failure must not clobber the handler-preset code.
    enriched = enrich_failure("run_action", out)
    assert enriched["errorCode"] == "INVALID_ACTION"


def test_run_action_ok_status_not_touched():
    backend = MagicMock()
    backend.run_action.return_value = {
        "success": True,
        "action": "common.Control.zoomFitScreen",
        "status": 1,
        "statusName": "RAS_OK",
    }
    iface = _make_iface(backend)
    out = iface._handle_run_action({"action": "common.Control.zoomFitScreen"})
    assert out["success"] is True
    assert "errorCode" not in out


def test_run_action_frame_not_open_not_mislabeled_invalid_action():
    """RAS_FRAME_NOT_OPEN is a distinct state — must not get INVALID_ACTION."""
    backend = MagicMock()
    backend.run_action.return_value = {
        "success": False,
        "action": "common.Control.zoomFitScreen",
        "status": 3,
        "statusName": "RAS_FRAME_NOT_OPEN",
    }
    iface = _make_iface(backend)
    out = iface._handle_run_action({"action": "common.Control.zoomFitScreen"})
    assert out.get("errorCode") != "INVALID_ACTION"


# --- C8: classify_failure covers the whole IPC-required handler family -------


def test_classify_failure_ipc_required_family():
    from utils.failure import classify_failure

    for msg in (
        "run_action requires the IPC backend. KiCad isn't running",
        "Transaction commands require the IPC backend. Launch KiCAD with ...",
        "Board metadata commands require the IPC backend. Launch KiCAD with ...",
        "Selection commands require the IPC backend. Launch KiCAD with ...",
    ):
        code, hint = classify_failure(msg)
        assert code == "IPC_REQUIRED", f"{msg!r} -> {code}"
        assert hint


def test_run_action_surfaces_backend_exceptions():
    backend = MagicMock()
    backend.run_action.side_effect = RuntimeError("socket closed")
    iface = _make_iface(backend)
    out = iface._handle_run_action({"action": "any"})
    assert out["success"] is False
    assert "socket closed" in out["message"]
