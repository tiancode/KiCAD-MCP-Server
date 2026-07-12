"""Regression tests for launch_kicad_ui's file-open forwarding.

User report: with KiCad already running, calling
``launch_kicad_ui(projectPath=<.kicad_pcb>)`` returned
``alreadyRunning: true, launched: false`` but the file wasn't opened.
The handler now forwards the file-open via two best-effort paths
(IPC run_action → spawn ``kicad <path>``) and reports which one
landed.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _iface_with_running_kicad():
    """Bare KiCADInterface stand-in with a connected IPC backend."""
    from kicad_interface import KiCADInterface

    obj = KiCADInterface.__new__(KiCADInterface)
    obj.use_ipc = True
    obj.board = None
    obj.command_routes = {}
    obj.ipc_board_api = MagicMock()
    obj.ipc_backend = MagicMock()
    obj.ipc_backend.is_connected = lambda: True
    obj.ipc_backend._kicad = MagicMock()
    obj.ipc_backend._kicad.get_open_documents = MagicMock(return_value=[])
    obj.ipc_backend.run_action = MagicMock(
        return_value={"success": False, "statusName": "RAS_INVALID"}
    )
    obj._try_enable_ipc_backend = lambda force=False: True
    obj._backend_status = lambda: {
        "backend": "ipc",
        "realtime_sync": True,
        "ipc_connected": True,
        "capabilities": {},
    }
    obj._ipc_writes_pending = False
    obj._swig_writes_landed = False
    obj._ipc_change_callback_registered = False
    return obj


def _running_check_and_launch(path, auto_launch=True):
    """Simulate KiCAD already running — what check_and_launch_kicad
    returns when is_running() is True."""
    return {
        "running": True,
        "launched": False,
        "alreadyRunning": True,
        "processes": [],
        "message": "KiCAD is already running",
    }


# ---------------------------------------------------------------------------
# Already-open short-circuit
# ---------------------------------------------------------------------------
def test_already_open_path_is_a_noop(monkeypatch, tmp_path):
    """When KiCad already has the requested file loaded, the handler
    should report ``fileOpenMethod: already_open`` without spawning
    anything or calling run_action."""
    from handlers import ui as ui_handler

    sch = tmp_path / "demo.kicad_pcb"
    sch.write_text("(kicad_pcb)\n", encoding="utf-8")

    iface = _iface_with_running_kicad()

    class _Doc:
        def __init__(self, p):
            self.path = p

    iface.ipc_backend._kicad.get_open_documents = MagicMock(return_value=[_Doc(str(sch))])

    monkeypatch.setattr(ui_handler, "check_and_launch_kicad", _running_check_and_launch)
    monkeypatch.setattr(
        "subprocess.Popen",
        MagicMock(side_effect=AssertionError("spawn must not happen")),
    )

    out = ui_handler.handle_launch_kicad_ui(iface, {"projectPath": str(sch)})

    assert out["success"] is True
    assert out["alreadyRunning"] is True
    assert out["fileOpenForwarded"] is True
    assert out["fileOpenMethod"] == "already_open"
    iface.ipc_backend.run_action.assert_not_called()


# ---------------------------------------------------------------------------
# IPC run_action path: succeeds when get_open_documents shows the path
# ---------------------------------------------------------------------------
def test_run_action_opens_file_and_verifies(monkeypatch, tmp_path):
    from handlers import ui as ui_handler

    sch = tmp_path / "demo.kicad_pcb"
    sch.write_text("(kicad_pcb)\n", encoding="utf-8")

    iface = _iface_with_running_kicad()

    class _Doc:
        def __init__(self, p):
            self.path = p

    # First poll: empty; after run_action: doc appears.
    call_state = {"action_invoked": False}

    def _docs():
        return [_Doc(str(sch))] if call_state["action_invoked"] else []

    iface.ipc_backend._kicad.get_open_documents = lambda: _docs()

    def _run_action(action):
        call_state["action_invoked"] = True
        return {"success": True, "statusName": "RAS_OK", "action": action}

    iface.ipc_backend.run_action = _run_action

    monkeypatch.setattr(ui_handler, "check_and_launch_kicad", _running_check_and_launch)
    spawn_spy = MagicMock(side_effect=AssertionError("spawn must NOT be used"))
    monkeypatch.setattr("subprocess.Popen", spawn_spy)

    out = ui_handler.handle_launch_kicad_ui(iface, {"projectPath": str(sch)})

    assert out["fileOpenForwarded"] is True
    assert out["fileOpenMethod"] == "ipc_action"
    assert "fileOpenAction" in out
    spawn_spy.assert_not_called()


# ---------------------------------------------------------------------------
# Spawn fallback: run_action doesn't land, pcbnew <board> takes over.
# For a .kicad_pcb the spawn must be the STANDALONE PCB EDITOR — a bare
# `kicad <board>` only raises the project manager with no board document open
# over IPC (verified on KiCad 10.x), so it would never lift the PCB-editor
# gate.  `pcbnew <board>` surfaces the board over IPC even alongside a
# running project manager.
# ---------------------------------------------------------------------------
def test_spawn_fallback_when_run_action_fails(monkeypatch, tmp_path):
    from handlers import ui as ui_handler
    from utils.kicad_process import KiCADProcessManager

    sch = tmp_path / "demo.kicad_pcb"
    sch.write_text("(kicad_pcb)\n", encoding="utf-8")

    iface = _iface_with_running_kicad()
    # run_action always returns INVALID — every candidate is rejected.
    iface.ipc_backend.run_action = MagicMock(
        return_value={"success": False, "statusName": "RAS_INVALID"}
    )

    monkeypatch.setattr(ui_handler, "check_and_launch_kicad", _running_check_and_launch)
    # For a board, the spawn goes through get_pcb_editor_command (standalone
    # PCB editor), NOT get_executable_path (project manager).
    monkeypatch.setattr(
        KiCADProcessManager,
        "get_pcb_editor_command",
        staticmethod(lambda board_path=None: ["/usr/bin/pcbnew", str(board_path)]),
    )
    monkeypatch.setattr(
        KiCADProcessManager,
        "get_executable_path",
        staticmethod(lambda: (_ for _ in ()).throw(AssertionError("must spawn pcbnew, not kicad"))),
    )
    spawned = {}

    def _fake_popen(argv, **kwargs):
        spawned["argv"] = argv
        spawned["kwargs"] = kwargs
        return MagicMock()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)

    out = ui_handler.handle_launch_kicad_ui(iface, {"projectPath": str(sch)})

    assert out["fileOpenForwarded"] is True
    assert out["fileOpenMethod"] == "spawn"
    assert spawned["argv"] == ["/usr/bin/pcbnew", str(sch)]
    # Multiple ipc_action attempts were logged before the fallback.
    actions_tried = [a for a in out["fileOpenAttempts"] if a.get("method") == "ipc_action"]
    assert len(actions_tried) >= 2


# ---------------------------------------------------------------------------
# Spawn fallback skips when the board is already locked by another instance:
# opening a second, read-locked editor is worse than the gate.
# ---------------------------------------------------------------------------
def test_spawn_fallback_skips_when_board_lock_present(monkeypatch, tmp_path):
    from handlers import ui as ui_handler
    from utils.kicad_process import KiCADProcessManager

    board = tmp_path / "demo.kicad_pcb"
    board.write_text("(kicad_pcb)\n", encoding="utf-8")
    # KiCad's per-file lock: ~<name>.<ext>.lck next to the board.
    (tmp_path / "~demo.kicad_pcb.lck").write_text(
        '{"hostname":"h","username":"u"}', encoding="utf-8"
    )

    iface = _iface_with_running_kicad()
    iface.ipc_backend.run_action = MagicMock(
        return_value={"success": False, "statusName": "RAS_INVALID"}
    )
    monkeypatch.setattr(ui_handler, "check_and_launch_kicad", _running_check_and_launch)
    # A spawn must NOT happen while the lock is present.
    monkeypatch.setattr(
        KiCADProcessManager,
        "get_pcb_editor_command",
        staticmethod(lambda board_path=None: ["/usr/bin/pcbnew", str(board_path)]),
    )
    spawn_spy = MagicMock(side_effect=AssertionError("must not spawn on a locked board"))
    monkeypatch.setattr("subprocess.Popen", spawn_spy)

    out = ui_handler.handle_launch_kicad_ui(iface, {"projectPath": str(board)})

    assert out["fileOpenForwarded"] is False
    assert "warning" in out
    assert "locked" in out["warning"].lower()
    spawn_spy.assert_not_called()
    skipped = [a for a in out["fileOpenAttempts"] if a.get("skipped")]
    assert skipped and "lockfile" in skipped[0]["skipped"]


# ---------------------------------------------------------------------------
# Spawn failure: surface a clear warning, don't crash
# ---------------------------------------------------------------------------
def test_spawn_failure_surfaces_warning(monkeypatch, tmp_path):
    from handlers import ui as ui_handler
    from utils.kicad_process import KiCADProcessManager

    sch = tmp_path / "demo.kicad_pcb"
    sch.write_text("(kicad_pcb)\n", encoding="utf-8")

    iface = _iface_with_running_kicad()
    iface.ipc_backend.run_action = MagicMock(
        return_value={"success": False, "statusName": "RAS_INVALID"}
    )

    monkeypatch.setattr(ui_handler, "check_and_launch_kicad", _running_check_and_launch)
    monkeypatch.setattr(
        KiCADProcessManager,
        "get_pcb_editor_command",
        staticmethod(lambda board_path=None: ["/usr/bin/pcbnew", str(board_path)]),
    )
    monkeypatch.setattr(
        "subprocess.Popen",
        MagicMock(side_effect=PermissionError("no exec perm")),
    )

    out = ui_handler.handle_launch_kicad_ui(iface, {"projectPath": str(sch)})

    # Operation overall still "succeeded" (KiCad is running, just couldn't
    # forward the file-open).  The warning tells the user what's wrong.
    assert out["success"] is True
    assert out["fileOpenForwarded"] is False
    assert "warning" in out
    assert "no exec perm" in out["warning"]


# ---------------------------------------------------------------------------
# No-projectPath: don't try to forward anything
# ---------------------------------------------------------------------------
def test_no_project_path_skips_forwarding(monkeypatch):
    from handlers import ui as ui_handler

    iface = _iface_with_running_kicad()
    iface.ipc_backend.run_action = MagicMock(
        side_effect=AssertionError("run_action must not be called without a path"),
    )
    spawn_spy = MagicMock(side_effect=AssertionError("spawn must not be called"))
    monkeypatch.setattr("subprocess.Popen", spawn_spy)
    monkeypatch.setattr(ui_handler, "check_and_launch_kicad", _running_check_and_launch)

    out = ui_handler.handle_launch_kicad_ui(iface, {})

    assert out["success"] is True
    assert "fileOpenMethod" not in out
    spawn_spy.assert_not_called()


# ---------------------------------------------------------------------------
# KiCad NOT running: existing launch path still drives, no forwarding fires
# ---------------------------------------------------------------------------
def test_not_running_falls_through_to_normal_launch(monkeypatch, tmp_path):
    """When KiCad isn't running, check_and_launch_kicad's own launch
    branch fires; forwarding doesn't kick in (the launched process
    receives the path directly on argv)."""
    from handlers import ui as ui_handler

    sch = tmp_path / "demo.kicad_pcb"
    sch.write_text("(kicad_pcb)\n", encoding="utf-8")

    def _fresh_launch(path, auto_launch=True):
        return {
            "running": True,
            "launched": True,
            "alreadyRunning": False,
            "processes": [],
            "message": "KiCAD launched",
        }

    iface = _iface_with_running_kicad()
    monkeypatch.setattr(ui_handler, "check_and_launch_kicad", _fresh_launch)
    monkeypatch.setattr(
        "subprocess.Popen",
        MagicMock(side_effect=AssertionError("spawn must not be called on cold launch")),
    )

    out = ui_handler.handle_launch_kicad_ui(iface, {"projectPath": str(sch)})

    assert out["launched"] is True
    assert out["alreadyRunning"] is False
    # No forwarding fields set — fresh launch carries the path itself.
    assert "fileOpenMethod" not in out
