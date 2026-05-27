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
    """Get information about the current backend (IPC vs. SWIG, version).

    On SWIG the response is *prescriptive*, not descriptive — the agent
    gets a concrete next step (`launch_kicad_ui`) plus the concrete
    capabilities it's giving up by staying on SWIG, so it doesn't
    discover the gap through 8-deep trial-and-error.
    """
    if KiCADProcessManager.is_running():
        iface._try_enable_ipc_backend()
    status = iface._backend_status()
    ipc_backend = getattr(iface, "ipc_backend", None)
    response: Dict[str, Any] = {
        "success": True,
        **status,
        "version": ipc_backend.get_version() if ipc_backend else "N/A",
    }
    if status["backend"] == "ipc":
        response["message"] = "Using IPC backend with real-time UI sync"
    else:
        unavailable_count = len(status.get("unavailable_tools", []))
        response["message"] = (
            "On SWIG backend — call launch_kicad_ui to enable IPC "
            "(unlocks realtime sync, transactions, selection, and "
            f"{unavailable_count} IPC-only tools)"
        )
        response["recommendation"] = (
            "Call launch_kicad_ui to switch to the IPC backend. Without it "
            "you lose: realtime UI sync (your changes won't appear until "
            "KiCAD reloads the file), atomic transactions (no rollback on "
            "error), the selection API, and "
            f"{unavailable_count} IPC-only tools (see unavailable_tools)."
        )
    return response


def handle_run_action(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke a KiCad TOOL_ACTION by name via the IPC backend.

    The action namespace is KiCad-internal and not stable across releases —
    surface kipy's exact response (status + statusName) so callers can
    detect ``RAS_INVALID`` vs ``RAS_FRAME_NOT_OPEN`` and recover.
    Requires the IPC backend; SWIG has no equivalent.
    """
    if not iface.use_ipc or not iface.ipc_backend:
        # Action names can target any frame (project manager / PCB / schematic
        # editor / plugin), so we don't require the PCB editor specifically —
        # kipy will report RAS_FRAME_NOT_OPEN with the action name if needed.
        ok, reason = iface.ensure_ipc(allow_launch=True, require_pcb_editor=False)
        if not ok:
            return {
                "success": False,
                "message": ("run_action requires the IPC backend. " + reason),
            }
    action = params.get("action")
    if not isinstance(action, str) or not action:
        return {"success": False, "message": "'action' parameter is required (string)"}
    try:
        return iface.ipc_backend.run_action(action)
    except Exception as e:
        logger.error(f"Error invoking action {action!r}: {e}")
        return {"success": False, "action": action, "message": str(e)}


def handle_reconcile_backends(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Flush pending changes between the SWIG and IPC backends, if possible.

    ``direction`` (required) is ``ipc_to_swig`` or ``swig_to_ipc``.

    - ``ipc_to_swig``: if IPC has unsaved changes, call ``ipc_save_board``
      first; then re-load the SWIG board from disk so the next SWIG call
      sees the IPC content.  Clears both ``_ipc_writes_pending`` and
      ``_swig_writes_landed`` on success.
    - ``swig_to_ipc``: refused with explicit recovery steps — kipy has no
      "discard pending and reload from disk" API, so the user has to
      reload the .kicad_pcb file inside KiCad (File → Revert from saved,
      or close+reopen the file) before further IPC writes are safe.
    """
    direction = params.get("direction")
    if direction not in ("ipc_to_swig", "swig_to_ipc"):
        return {
            "success": False,
            "message": (
                "reconcile_backends requires direction='ipc_to_swig' or "
                "direction='swig_to_ipc'"
            ),
        }

    if direction == "swig_to_ipc":
        return {
            "success": False,
            "direction": "swig_to_ipc",
            "needs_manual_action": True,
            "message": (
                "SWIG → IPC reconcile is not automatic: kipy has no API to "
                "discard KiCad's in-memory state and reload from disk. In "
                "KiCad, reload the .kicad_pcb file (File → Revert from saved, "
                "or close+reopen the file), then any further IPC writes are "
                "safe. Once that's done, the next IPC write will succeed and "
                "the _swig_writes_landed gate clears itself on the next "
                "open_project / IPC save."
            ),
            "steps": [
                "Switch to the PCB editor in KiCad.",
                "File → Revert from saved (or close the file and reopen it).",
                "Resume the workflow — the SWIG content is now in KiCad memory.",
            ],
        }

    # direction == "ipc_to_swig"
    if not iface._ipc_writes_pending and not iface._swig_writes_landed:
        return {
            "success": True,
            "direction": "ipc_to_swig",
            "noop": True,
            "message": "Backends are already in sync; nothing to reconcile.",
        }

    steps_taken: list = []

    # Step 1: flush IPC to disk if it has pending writes.
    if iface._ipc_writes_pending:
        ok, reason = iface.ensure_ipc(allow_launch=False, require_pcb_editor=True)
        if not ok:
            return {
                "success": False,
                "direction": "ipc_to_swig",
                "message": (
                    "Cannot flush IPC to disk: IPC isn't reachable. " + reason
                ),
            }
        try:
            saved = iface.ipc_board_api.save()
        except Exception as e:
            logger.error(f"reconcile_backends: ipc save raised: {e}")
            return {
                "success": False,
                "direction": "ipc_to_swig",
                "message": f"Cannot flush IPC to disk: {e}",
            }
        if not saved:
            return {
                "success": False,
                "direction": "ipc_to_swig",
                "message": (
                    "Cannot flush IPC to disk: ipc_board_api.save() returned "
                    "False. Try ipc_save_board manually to surface kipy's error."
                ),
            }
        steps_taken.append("ipc_save_board")

    # Step 2: re-load SWIG board from disk so subsequent SWIG ops see the
    # freshly-saved content.  Find the .kicad_pcb path from whichever
    # source is authoritative right now.
    board_path = iface._current_board_path()
    if not board_path:
        return {
            "success": False,
            "direction": "ipc_to_swig",
            "message": (
                "Reloaded IPC to disk, but no board path is known to reload "
                "into the SWIG backend. Call open_project explicitly to point "
                "at the .kicad_pcb file."
            ),
            "stepsTaken": steps_taken,
        }
    reloaded = iface._safe_load_board(board_path)
    if reloaded is None:
        return {
            "success": False,
            "direction": "ipc_to_swig",
            "message": (
                f"Flushed IPC to disk, but reloading the SWIG board from "
                f"{board_path} failed. Call open_project manually to retry."
            ),
            "stepsTaken": steps_taken,
        }
    iface.board = reloaded
    if iface.project_commands is not None:
        iface.project_commands.board = reloaded
    iface._update_command_handlers()
    iface._record_board_signature(board_path)
    iface._swig_writes_landed = False
    # _ipc_writes_pending was cleared by the save callback already; assert
    # the invariant in case the callback didn't fire (degenerate IPC).
    iface._ipc_writes_pending = False
    steps_taken.append("swig_reload")

    return {
        "success": True,
        "direction": "ipc_to_swig",
        "boardPath": board_path,
        "stepsTaken": steps_taken,
        "message": "Flushed IPC to disk and reloaded the SWIG board.",
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
        # Cross-backend sync state: lets callers detect divergence ahead
        # of the dispatch-time gate so they can call reconcile_backends
        # proactively instead of waiting for a needs_reconcile error.
        "ipcWritesPending": bool(getattr(iface, "_ipc_writes_pending", False)),
        "swigWritesLanded": bool(getattr(iface, "_swig_writes_landed", False)),
        "message": (
            f"{status['backend']} backend; "
            f"{'board loaded' if loaded_board else 'no board loaded'}"
        ),
    }
