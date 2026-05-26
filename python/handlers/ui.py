"""
UI / process / backend-status handlers.

These all read the running KiCAD process state and the current MCP
backend (IPC vs. SWIG).  They never mutate the board, so they don't
need the auto-save guard, and they only depend on private helpers
that stay on `KiCADInterface` (`_try_enable_ipc_backend`,
`_backend_status`, `_current_board_path`, `_dirty_state`, …).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

from utils.kicad_process import KiCADProcessManager, check_and_launch_kicad

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


def handle_check_kicad_ui(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Check if KiCAD UI is running.

    `processes` is the single source of truth — `running` is derived from
    its length so the two fields cannot disagree.  See issue #173 for the
    history.  If KiCAD is up, opportunistically (re)connect the IPC
    backend so a session that started before KiCAD launched can fall up
    from SWIG to IPC (#140).
    """
    logger.info("Checking if KiCAD UI is running")
    try:
        manager = KiCADProcessManager()
        processes = manager.get_process_info()
        is_running = len(processes) > 0
        if is_running:
            iface._try_enable_ipc_backend()

        return {
            "success": True,
            "running": is_running,
            "processes": processes,
            "message": "KiCAD is running" if is_running else "KiCAD is not running",
            **iface._backend_status(),
        }
    except Exception as e:
        logger.error(f"Error checking KiCAD UI status: {str(e)}")
        return {"success": False, "message": str(e)}


def handle_launch_kicad_ui(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Launch KiCAD UI (no-op if already running)."""
    logger.info("Launching KiCAD UI")
    try:
        # Read AUTO_LAUNCH_KICAD lazily to avoid a hard import cycle with
        # kicad_interface (which imports this module).
        from kicad_interface import AUTO_LAUNCH_KICAD

        project_path = params.get("projectPath")
        auto_launch = params.get("autoLaunch", AUTO_LAUNCH_KICAD)
        path_obj = Path(project_path) if project_path else None

        result = check_and_launch_kicad(path_obj, auto_launch)
        if result.get("running"):
            iface._try_enable_ipc_backend(force=True)

        return {"success": True, **result, **iface._backend_status()}
    except Exception as e:
        logger.error(f"Error launching KiCAD UI: {str(e)}")
        return {"success": False, "message": str(e)}


def handle_get_backend_info(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Get information about the current backend (IPC vs. SWIG, version)."""
    if KiCADProcessManager.is_running():
        iface._try_enable_ipc_backend()
    status = iface._backend_status()
    ipc_backend = getattr(iface, "ipc_backend", None)
    return {
        "success": True,
        **status,
        "version": ipc_backend.get_version() if ipc_backend else "N/A",
        "message": (
            "Using IPC backend with real-time UI sync"
            if status["backend"] == "ipc"
            else "Using SWIG backend (requires manual reload)"
        ),
    }


def handle_get_backend_state(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Return the MCP/KiCad backend state and currently loaded file state."""
    if KiCADProcessManager.is_running():
        iface._try_enable_ipc_backend()

    status = iface._backend_status()
    board_path = iface._current_board_path()
    project_path = iface._current_project_file_path(board_path)
    dirty_state = iface._dirty_state(board_path)
    loaded_board = board_path is not None
    loaded_project = project_path is not None

    return {
        "success": True,
        "backend": status["backend"],
        "realtime": status["realtime_sync"],
        "realtime_sync": status["realtime_sync"],
        "ipcConnected": status["ipc_connected"],
        "ipc_connected": status["ipc_connected"],
        "loadedProject": loaded_project,
        "loadedBoard": loaded_board,
        "projectPath": project_path,
        "boardPath": board_path,
        "dirty": dirty_state["dirty"],
        "dirtyReason": dirty_state["dirtyReason"],
        "diskChangedExternally": dirty_state["diskChangedExternally"],
        "message": (
            f"{status['backend']} backend; "
            f"{'board loaded' if loaded_board else 'no board loaded'}"
        ),
    }
