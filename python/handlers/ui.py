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
import os
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
        # Force-attach unconditionally — covers the "user launched KiCad
        # after the MCP started" case where /proc detection lags behind
        # reality, AND the "KiCad has been up the whole time but never
        # attached" case after a startup-time IPC connect blip.
        iface._try_enable_ipc_backend(force=True)

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
    """Launch KiCAD UI (or forward a file-open to the running instance).

    Previously a no-op when KiCad was already running: the caller passed
    ``projectPath`` expecting the running KiCad to open it, and the
    handler returned ``alreadyRunning: true, launched: false`` without
    doing anything with the path.  The handler now forwards the
    file-open via two best-effort paths:

      1. IPC ``run_action`` — KiCad action names aren't stable across
         releases; we try a small set ("open file" / "open project"
         family) and stop on the first ``RAS_OK`` whose effect we can
         verify via ``get_open_documents()``.
      2. Spawn ``kicad <path>`` — KiCad's single-instance protocol
         (wxSingleInstanceChecker) makes the new process hand the
         file-open off to the running instance, then exit.

    The response surfaces ``fileOpenForwarded: bool`` and
    ``fileOpenMethod: "ipc_action" | "spawn" | "none"`` so the agent
    can see which path landed.  When neither works the response carries
    a ``warning`` instructing the user to drag the file into KiCad
    manually (or close KiCad and retry).
    """
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

        # If KiCad was already running AND the caller asked for a
        # specific file, forward the file-open.  Skip when there's no
        # path (caller just wanted to bring KiCad up) or when launch()
        # itself spawned the process (it already had the path on argv).
        if path_obj is not None and result.get("alreadyRunning") and not result.get("launched"):
            forwarded = _forward_file_open_to_running_kicad(iface, path_obj)
            result.update(forwarded)

        return {"success": True, **result, **iface._backend_status()}
    except Exception as e:
        logger.error(f"Error launching KiCAD UI: {str(e)}")
        return {"success": False, "message": str(e)}


def _path_already_open(iface: "KiCADInterface", target: Path) -> bool:
    """Return True iff ``get_open_documents`` lists ``target`` (or its
    .kicad_pcb sibling when target is .kicad_pro).  Used to verify that
    a forward attempt actually landed."""
    ipc_backend = getattr(iface, "ipc_backend", None)
    if ipc_backend is None or not getattr(ipc_backend, "is_connected", lambda: False)():
        return False
    kicad = getattr(ipc_backend, "_kicad", None)
    if kicad is None:
        return False
    # kipy 10's get_open_documents(doc_type) requires the arg — query all
    # known types via the compat helper instead of the broken no-arg call.
    from kicad_api.ipc_backend import get_open_documents_compat

    try:
        docs = get_open_documents_compat(kicad)
    except Exception:
        return False
    try:
        target_resolved = target.resolve()
    except Exception:
        target_resolved = target
    candidate_strs = {str(target), str(target_resolved)}
    # When the caller passes .kicad_pro, accept the .kicad_pcb sibling as
    # "open" — and vice versa — since KiCad opens the board frame in
    # response to either path.
    sibling_pcb = target_resolved.with_suffix(".kicad_pcb")
    candidate_strs.add(str(sibling_pcb))
    for doc in docs:
        # Docs carry board_filename (relative) + project.path (dir); also
        # accept a bare ``path`` attr if a future kipy exposes one.
        fname = getattr(doc, "board_filename", None) or getattr(doc, "path", None)
        proj = getattr(doc, "project", None)
        proj_dir = getattr(proj, "path", "") if proj is not None else ""
        full = os.path.join(proj_dir, str(fname)) if (proj_dir and fname) else (fname or "")
        for cand in (str(fname or ""), str(full)):
            if cand and any(cand.endswith(c) for c in candidate_strs):
                return True
    return False


def _forward_file_open_to_running_kicad(iface: "KiCADInterface", path: Path) -> Dict[str, Any]:
    """Best-effort: ask the running KiCad to open ``path``.

    Tries IPC ``run_action`` first (action names are KiCad-internal and
    unstable, so we walk a candidate list).  If verification via
    ``get_open_documents()`` still doesn't show the path, falls back to
    spawning ``kicad <path>`` and relying on KiCad's single-instance
    protocol to hand the open request to the existing process.

    Never raises; the return dict is merged into the
    ``launch_kicad_ui`` response.
    """
    import platform
    import subprocess

    out: Dict[str, Any] = {
        "fileOpenForwarded": False,
        "fileOpenMethod": "none",
        "fileOpenAttempts": [],
    }

    # Already-open short-circuit — saves the user from a redundant action.
    if _path_already_open(iface, path):
        out["fileOpenForwarded"] = True
        out["fileOpenMethod"] = "already_open"
        return out

    # Path 1: IPC run_action.  Most KiCad versions don't expose a
    # "openFile" action, but we try a small set anyway — gracefully
    # degrades when none land.
    ipc_backend = getattr(iface, "ipc_backend", None)
    if ipc_backend is not None and getattr(ipc_backend, "is_connected", lambda: False)():
        for action in (
            "common.Control.openFile",
            "kicadManager.Control.openProject",
            "kicadManager.Control.openFile",
        ):
            try:
                resp = ipc_backend.run_action(action)
            except Exception as exc:  # pragma: no cover - best-effort
                logger.debug(f"run_action({action!r}) raised: {exc}")
                continue
            out["fileOpenAttempts"].append(
                {"method": "ipc_action", "action": action, "status": resp.get("statusName")}
            )
            if resp.get("success") and _path_already_open(iface, path):
                out["fileOpenForwarded"] = True
                out["fileOpenMethod"] = "ipc_action"
                out["fileOpenAction"] = action
                return out

    # Path 2: spawn ``kicad <path>`` so KiCad's wxSingleInstanceChecker
    # forwards the open request to the running process.  The new
    # process exits after handing off; no second window appears on
    # builds that have single-instance enabled.
    exe = KiCADProcessManager.get_executable_path()
    if exe is None:
        out["fileOpenAttempts"].append({"method": "spawn", "error": "kicad executable not found"})
        out["warning"] = (
            "KiCad is running but the MCP couldn't open the file: "
            "neither IPC's run_action nor the kicad CLI is reachable.  "
            "Open the file manually in KiCad (File → Open, or drag-drop "
            "the path) or close KiCad and call launch_kicad_ui again."
        )
        return out

    try:
        # Detach the child so it doesn't tie its lifetime to the MCP
        # server.  KiCad's single-instance handshake exits the child
        # quickly; we don't wait on it.
        creationflags = 0
        kwargs: Dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if platform.system() == "Windows":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            kwargs["creationflags"] = creationflags
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([str(exe), str(path)], **kwargs)
        out["fileOpenAttempts"].append({"method": "spawn", "argv": [str(exe), str(path)]})
        out["fileOpenForwarded"] = True
        out["fileOpenMethod"] = "spawn"
    except Exception as exc:
        logger.warning(f"spawn fallback for file-open failed: {exc}")
        out["fileOpenAttempts"].append({"method": "spawn", "error": str(exc)})
        out["warning"] = (
            "KiCad is running but the MCP couldn't forward the file-open: "
            f"{exc}.  Open the file manually in KiCad or close KiCad and "
            "call launch_kicad_ui again."
        )
    return out


def handle_get_backend_info(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Get information about the current backend (IPC vs. SWIG, version).

    Actively tries to attach IPC (``_try_enable_ipc_backend(force=True)``)
    before reporting — otherwise a user who launched KiCad after the MCP
    server started would still see SWIG on every poll until something
    else triggered the attach. On SWIG the response is *prescriptive*:
    the recommendation text branches on whether KiCad is running so the
    agent gets the right next step (start KiCad vs. enable the IPC API
    server in Preferences) instead of a generic "call launch_kicad_ui".
    """
    # Force-attach: covers the "user launched KiCad after MCP" case where
    # is_running() lags behind reality, and the "KiCad has been up the
    # whole time but never attached" case after a prior reconnect blip.
    iface._try_enable_ipc_backend(force=True)
    status = iface._backend_status()
    ipc_backend = getattr(iface, "ipc_backend", None)
    response: Dict[str, Any] = {
        "success": True,
        **status,
        "version": ipc_backend.get_version() if ipc_backend else "N/A",
    }
    if status["backend"] == "ipc":
        response["message"] = "Using IPC backend with real-time UI sync"
        return response

    # SWIG branch — diagnose the *specific* reason IPC isn't attached.
    # This is the capability-enumeration tool, so it returns the full
    # unavailable_tools list; the routine status tools (_backend_status) carry
    # only unavailable_tool_count to keep their responses small.
    response["unavailable_tools"] = list(iface.IPC_REQUIRED_COMMANDS)
    unavailable_count = len(response["unavailable_tools"])
    kicad_running = KiCADProcessManager.is_running()
    if not kicad_running:
        response["kicad_running"] = False
        response["message"] = (
            "On SWIG backend — KiCad isn't running. "
            f"Start KiCad (or call launch_kicad_ui) to enable IPC and unlock "
            f"{unavailable_count} IPC-only tools."
        )
        response["recommendation"] = (
            "Call ``launch_kicad_ui`` to start KiCad with IPC attached.  "
            "Alternatively start KiCad manually (any platform) — the next "
            "``get_backend_info`` call will retry the attach automatically.  "
            "Without IPC you lose: realtime UI sync (changes won't appear "
            "until KiCAD reloads the file), atomic transactions, the "
            f"selection API, and {unavailable_count} IPC-only tools "
            "(see unavailable_tools)."
        )
    else:
        response["kicad_running"] = True
        response["message"] = (
            "On SWIG backend — KiCad is running but its IPC API server "
            "isn't reachable.  Enable it in KiCAD: Preferences → Plugins → "
            "Enable IPC API Server, then re-call get_backend_info."
        )
        response["recommendation"] = (
            "KiCAD is running but the MCP can't reach its IPC API server.  "
            "Open KiCAD → Preferences → Plugins → check 'Enable IPC API "
            "Server' (KiCAD 9+).  No restart needed — the next "
            "get_backend_info call will retry the attach.  Without IPC you "
            "lose realtime sync, transactions, selection, and "
            f"{unavailable_count} IPC-only tools."
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


def handle_reconcile_backends(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Flush pending changes between the SWIG and IPC backends, if possible.

    ``direction`` (required) is ``ipc_to_swig`` or ``swig_to_ipc``.

    - ``ipc_to_swig``: if IPC has unsaved changes, call ``ipc_save_board``
      first; then re-load the SWIG board from disk so the next SWIG call
      sees the IPC content.  Clears both ``_ipc_writes_pending`` and
      ``_swig_writes_landed`` on success.
    - ``swig_to_ipc``: if SWIG landed content on disk that KiCad memory
      doesn't include, call ``ipc_board_api.revert()`` (kipy
      ``Board.revert()`` → ``RevertDocument``) to reload KiCad from disk.
      Refused only when IPC *also* has unsaved changes — reverting would
      discard them, a genuine two-sided conflict the user must resolve.
      Clears both gate flags on success.
    """
    direction = params.get("direction")
    if direction not in ("ipc_to_swig", "swig_to_ipc"):
        return {
            "success": False,
            "message": (
                "reconcile_backends requires direction='ipc_to_swig' or " "direction='swig_to_ipc'"
            ),
        }

    if direction == "swig_to_ipc":
        # Nothing landed on the SWIG side → nothing to push into KiCad.
        if not iface._swig_writes_landed:
            if iface._ipc_writes_pending:
                # Wrong direction: SWIG has nothing to push, but IPC has
                # unsaved changes — that's the ipc_to_swig case.
                return {
                    "success": False,
                    "direction": "swig_to_ipc",
                    "message": (
                        "Nothing landed on the SWIG side to push into KiCad, "
                        "but IPC has unsaved changes. You probably want "
                        "reconcile_backends(direction=ipc_to_swig)."
                    ),
                }
            return {
                "success": True,
                "direction": "swig_to_ipc",
                "noop": True,
                "message": "Backends are already in sync; nothing to reconcile.",
            }
        # Both sides diverged: reverting KiCad to disk would discard the
        # unsaved IPC changes.  Only the user can decide which to keep.
        if iface._ipc_writes_pending:
            return {
                "success": False,
                "direction": "swig_to_ipc",
                "needs_manual_action": True,
                "message": (
                    "Both sides diverged: SWIG wrote new content to disk AND "
                    "KiCad has unsaved IPC changes. Reverting KiCad to disk "
                    "would discard the IPC changes, so this can't be resolved "
                    "automatically. Decide which to keep: save the IPC changes "
                    "first (overwriting the SWIG disk content), or accept the "
                    "disk content via File → Revert in KiCad."
                ),
                "steps": [
                    "In KiCad, either save the open board (keeps IPC changes, "
                    "discards SWIG disk content) ...",
                    "... or File → Revert from saved (keeps SWIG disk content, "
                    "discards unsaved IPC changes).",
                    "Then resume the workflow.",
                ],
            }
        # IPC is clean → safe to reload KiCad's in-memory board from disk.
        ok, reason = iface.ensure_ipc(allow_launch=False, require_pcb_editor=True)
        if not ok:
            return {
                "success": False,
                "direction": "swig_to_ipc",
                "message": ("Cannot reload KiCad from disk: IPC isn't reachable. " + reason),
            }
        try:
            reverted = iface.ipc_board_api.revert()
        except Exception as e:
            logger.error(f"reconcile_backends: ipc revert raised: {e}")
            return {
                "success": False,
                "direction": "swig_to_ipc",
                "message": f"Cannot reload KiCad from disk: {e}",
            }
        if not reverted:
            return {
                "success": False,
                "direction": "swig_to_ipc",
                "needs_manual_action": True,
                "message": (
                    "board.revert() did not succeed. Reload the .kicad_pcb "
                    "manually in KiCad (File → Revert from saved, or "
                    "close+reopen the file)."
                ),
                "steps": [
                    "Switch to the PCB editor in KiCad.",
                    "File → Revert from saved (or close the file and reopen it).",
                    "Resume the workflow — the SWIG content is now in KiCad memory.",
                ],
            }
        iface._swig_writes_landed = False
        iface._ipc_writes_pending = False
        return {
            "success": True,
            "direction": "swig_to_ipc",
            "stepsTaken": ["ipc_revert"],
            "message": (
                "Reloaded KiCad's in-memory board from disk via revert; KiCad "
                "now reflects the SWIG-written content."
            ),
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
                "message": ("Cannot flush IPC to disk: IPC isn't reachable. " + reason),
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
