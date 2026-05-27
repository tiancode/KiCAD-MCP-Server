"""Regression tests for the get_backend_info / check_kicad_ui diagnostic branch.

User report: "SWIG starts by default, missing 26 tools" — the previous
get_backend_info ALWAYS recommended ``launch_kicad_ui`` even when the
user had KiCad already running with the IPC API server disabled.  That
sent users on the wrong fix.  The handler now:

  1. Force-attaches IPC on every call (so a user who launches KiCad
     AFTER the MCP server starts sees IPC on the next poll without
     anything else triggering the attach).
  2. Branches the SWIG recommendation on whether KiCad is actually
     running — "start KiCad" vs. "enable the IPC API server".
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _iface():
    from kicad_interface import KiCADInterface

    obj = KiCADInterface.__new__(KiCADInterface)
    obj.use_ipc = False
    obj.ipc_backend = None
    obj.ipc_board_api = None
    obj.board = None
    obj.command_routes = {}
    obj._board_disk_signature = None
    obj._current_project_path = None
    obj._last_auto_save_status = None
    obj._ipc_writes_pending = False
    obj._swig_writes_landed = False
    obj._ipc_change_callback_registered = False
    return obj


# ---------------------------------------------------------------------------
# get_backend_info: SWIG-mode diagnostic branches
# ---------------------------------------------------------------------------
def test_get_backend_info_swig_when_kicad_not_running_points_at_launch(monkeypatch):
    from handlers.ui import handle_get_backend_info
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr(KiCADProcessManager, "is_running", lambda: False)

    iface = _iface()
    iface._try_enable_ipc_backend = lambda force=False: False
    iface._backend_status = lambda: {
        "backend": "swig",
        "realtime_sync": False,
        "ipc_connected": False,
        "capabilities": {},
        "unavailable_tools": ["add_segment", "begin_transaction"],
    }

    out = handle_get_backend_info(iface, {})

    assert out["backend"] == "swig"
    assert out["kicad_running"] is False
    assert "launch_kicad_ui" in out["message"]
    assert "KiCad isn't running" in out["message"]
    assert "launch_kicad_ui" in out["recommendation"]


def test_get_backend_info_swig_when_kicad_running_points_at_preferences(monkeypatch):
    """The user's wedge case: KiCad is up but IPC API server isn't
    enabled.  Recommendation must point at Preferences → Plugins, NOT at
    launch_kicad_ui (which won't help)."""
    from handlers.ui import handle_get_backend_info
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr(KiCADProcessManager, "is_running", lambda: True)

    iface = _iface()
    iface._try_enable_ipc_backend = lambda force=False: False
    iface._backend_status = lambda: {
        "backend": "swig",
        "realtime_sync": False,
        "ipc_connected": False,
        "capabilities": {},
        "unavailable_tools": ["add_segment"],
    }

    out = handle_get_backend_info(iface, {})

    assert out["backend"] == "swig"
    assert out["kicad_running"] is True
    assert "Preferences" in out["message"]
    assert "IPC API Server" in out["message"]
    assert "Preferences" in out["recommendation"]
    # "launch_kicad_ui" must NOT be suggested here — KiCad is already up.
    assert "launch_kicad_ui" not in out["recommendation"]


def test_get_backend_info_ipc_branch_has_no_recommendation(monkeypatch):
    from handlers.ui import handle_get_backend_info

    iface = _iface()
    iface._try_enable_ipc_backend = lambda force=False: True
    iface._backend_status = lambda: {
        "backend": "ipc",
        "realtime_sync": True,
        "ipc_connected": True,
        "capabilities": {},
    }
    iface.ipc_backend = MagicMock()
    iface.ipc_backend.get_version = lambda: "10.0.3"

    out = handle_get_backend_info(iface, {})

    assert out["backend"] == "ipc"
    assert "real-time UI sync" in out["message"]
    assert "recommendation" not in out


def test_get_backend_info_force_attaches_ipc_unconditionally(monkeypatch):
    """The handler calls _try_enable_ipc_backend(force=True) regardless
    of is_running so a user who launched KiCad after the MCP started
    gets the attach on the next poll — the old code only retried when
    /proc detection already showed KiCad."""
    from handlers.ui import handle_get_backend_info
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr(KiCADProcessManager, "is_running", lambda: False)

    iface = _iface()
    seen: dict = {}

    def _spy(self_iface=None, force=False):
        seen["force"] = force
        seen["called"] = True
        return False

    iface._try_enable_ipc_backend = _spy
    iface._backend_status = lambda: {
        "backend": "swig",
        "realtime_sync": False,
        "ipc_connected": False,
        "capabilities": {},
        "unavailable_tools": [],
    }

    handle_get_backend_info(iface, {})

    assert seen["called"] is True
    assert seen["force"] is True


# ---------------------------------------------------------------------------
# check_kicad_ui: same force-attach behaviour
# ---------------------------------------------------------------------------
def test_check_kicad_ui_force_attaches_ipc_unconditionally(monkeypatch):
    from handlers.ui import handle_check_kicad_ui
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr(KiCADProcessManager, "is_running", lambda: False)
    monkeypatch.setattr(KiCADProcessManager, "get_process_info", lambda self: [])

    iface = _iface()
    seen: dict = {}
    iface._try_enable_ipc_backend = lambda force=False: (seen.update(force=force), False)[1]
    iface._backend_status = lambda: {
        "backend": "swig",
        "realtime_sync": False,
        "ipc_connected": False,
        "capabilities": {},
        "unavailable_tools": [],
    }

    out = handle_check_kicad_ui(iface, {})

    # Even though is_running=False, the force-attach was attempted.
    assert seen.get("force") is True
    assert out["running"] is False
