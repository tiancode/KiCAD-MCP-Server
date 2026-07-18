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


def _board_lock_present(path: Path) -> bool:
    """True when KiCad's per-file lock exists next to ``path``.

    KiCad writes ``~<name>.<ext>.lck`` (e.g. ``~board.kicad_pcb.lck``) in the
    board's directory while it holds the file open.  Its presence means some
    KiCad instance already has the board — opening a second editor on it would
    only get a read-locked duplicate (worse than the gate), so the spawn
    fallback skips it.
    """
    try:
        lock = path.with_name("~" + path.name + ".lck")
        return lock.exists()
    except Exception:
        return False


def _targets_current_board(iface: "KiCADInterface", path_obj: Path) -> bool:
    """Whether an explicit-launch ``projectPath`` refers to the loaded SWIG board.

    The fresh-open clear of ``_swig_writes_landed`` is only meaningful when
    the board KiCad just opened is the SAME file the landed SWIG write went
    to — launching a different project must not drop the flag.  A
    ``.kicad_pro`` path matches through its ``.kicad_pcb`` sibling.
    """
    try:
        board_path = iface.board.GetFileName() if getattr(iface, "board", None) else None
    except Exception:
        board_path = None
    if not board_path:
        return False
    try:
        target = path_obj if path_obj.suffix == ".kicad_pcb" else path_obj.with_suffix(".kicad_pcb")
        return Path(board_path).resolve() == Path(target).resolve()
    except Exception:
        return False


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
      2. Spawn an editor for ``path`` — a ``.kicad_pcb`` opens the
         standalone PCB editor (``pcbnew <board>``) so the board is
         served over IPC; other paths go to ``kicad <path>``, whose
         single-instance protocol hands the open off to the running
         instance, then exits.

    The response surfaces ``fileOpenForwarded: bool`` and
    ``fileOpenMethod: "ipc_action" | "spawn" | "none"`` so the agent
    can see which path landed.  When neither works the response carries
    a ``warning`` instructing the user to drag the file into KiCad
    manually (or close KiCad and retry).
    """
    logger.info("Launching KiCAD UI")
    try:
        project_path = params.get("projectPath")
        path_obj = Path(project_path) if project_path else None

        # F7: an explicit ``launch`` request means launching IS the intent, so
        # default autoLaunch ON — unlike the passive IPC-required auto-open
        # (AUTO_LAUNCH_KICAD, which is opt-in via KICAD_AUTO_LAUNCH=true).
        # Precedence:
        #   * env KICAD_AUTO_LAUNCH=false is a HARD opt-out — never launch.
        #   * an explicit ``autoLaunch`` param wins over the default.
        #   * otherwise default ON.
        env_hard_optout = os.environ.get("KICAD_AUTO_LAUNCH", "").strip().lower() == "false"
        explicit_auto_launch = params.get("autoLaunch")
        if env_hard_optout:
            auto_launch = False
        elif explicit_auto_launch is None:
            auto_launch = True
        else:
            auto_launch = bool(explicit_auto_launch)

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

        # B3 parity for the EXPLICIT launch path: a cold launch (board on
        # argv) or a file-open forward (spawn / verified ipc_action) opens
        # the board fresh from disk — exactly the state where a recorded
        # landed SWIG write needn't gate IPC any more.  The auto-open
        # self-heal already runs _clear_swig_landed_if_disk_matches; without
        # this, manage_kicad_ui(action=launch) left the flag set, so the
        # next IPC reads carried a false staleVsDisk and the first mutation
        # ran a needless auto-reconcile.  "already_open" is deliberately
        # NOT a fresh open (KiCad may hold memory that predates the landed
        # write), and a launch of a DIFFERENT board never clears the flag.
        fresh_open = bool(result.get("launched")) or result.get("fileOpenMethod") in (
            "spawn",
            "ipc_action",
        )
        if (
            fresh_open
            and path_obj is not None
            and getattr(iface, "_swig_writes_landed", False)
            and _targets_current_board(iface, path_obj)
        ):
            if iface._ipc_has_open_board_document():
                # Attach + document already verified — clear synchronously
                # (the helper re-checks the disk signature).
                iface._clear_swig_landed_if_disk_matches()
            else:
                # KiCad still booting / editor still loading: defer the
                # clear to the first point a board document is confirmed
                # open over IPC (signature captured now for safety).
                iface._arm_pending_fresh_open_clear()

        # F7: an explicit launch that did nothing must surface as
        # success:false — not the old success:true masking a silent no-op.
        # Two failure modes:
        #   * auto-launch suppressed (env/param opt-out) and KiCad isn't up →
        #     tell the caller exactly why and how to override.
        #   * auto-launch attempted but the process didn't come up → keep
        #     check_and_launch_kicad's "Failed to launch KiCAD" message.
        success = True
        if not result.get("running") and not result.get("launched"):
            success = False
            if not auto_launch:
                if env_hard_optout:
                    reason = "KICAD_AUTO_LAUNCH=false disables auto-launch"
                elif explicit_auto_launch is False:
                    reason = "autoLaunch:false was passed"
                else:
                    reason = "auto-launch is disabled"
                result["message"] = (
                    f"KiCAD is not running and was not launched ({reason}). "
                    "Start KiCAD manually, or retry with autoLaunch:true "
                    "(and without KICAD_AUTO_LAUNCH=false in the environment)."
                )

        return {"success": success, **result, **iface._backend_status()}
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
    spawning an editor: a ``.kicad_pcb`` is opened with the standalone PCB
    editor (``pcbnew <board>``) — which surfaces the board over IPC even when
    a project manager is already running — while other paths go to the
    project manager, whose single-instance protocol hands the open off to the
    existing process.

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

    # Path 2: spawn the editor that will actually open ``path`` as a document
    # KiCad serves over IPC.  A .kicad_pcb needs the *standalone PCB editor*
    # (pcbnew): ``kicad <board>`` only raises the project manager with no board
    # document open over IPC (verified on KiCad 10.x), so it never lifts the
    # PCB-editor gate — even alongside a running project manager, a standalone
    # ``pcbnew <board>`` makes get_open_documents report the board.  A project
    # (or other) file still goes to the project manager, whose single-instance
    # handshake hands the open off to the running process.
    is_board = path.suffix == ".kicad_pcb"
    if is_board:
        # Don't open a second editor on a board another KiCad instance already
        # holds — that yields a read-locked duplicate, worse than the gate.
        if _board_lock_present(path):
            out["fileOpenAttempts"].append({"method": "spawn", "skipped": "board lockfile present"})
            out["warning"] = (
                f"KiCad is running and '{path.name}' is locked by another "
                "instance (lock file present); not opening a second, "
                "read-locked editor.  Bring that KiCad window forward, or "
                "close it and call manage_kicad_ui(action=launch) again."
            )
            return out
        argv = KiCADProcessManager.get_pcb_editor_command(path)
    else:
        exe = KiCADProcessManager.get_executable_path()
        argv = [str(exe), str(path)] if exe is not None else None

    if not argv:
        out["fileOpenAttempts"].append(
            {"method": "spawn", "error": "KiCad PCB editor / project manager executable not found"}
        )
        out["warning"] = (
            "KiCad is running but the MCP couldn't open the file: "
            "neither IPC's run_action nor a pcbnew/kicad executable is "
            "reachable.  Open the file manually in KiCad (File → Open, or "
            "drag-drop the path) or close KiCad and call "
            "manage_kicad_ui(action=launch) again."
        )
        return out

    try:
        # Detach the child so it doesn't tie its lifetime to the MCP server.
        # For a board this is a genuine standalone PCB-editor process that
        # keeps running; for the project manager the single-instance handshake
        # exits it quickly.  Either way we don't wait on it.
        kwargs: Dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if platform.system() == "Windows":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)
        # E: a .kicad_pcb spawn is a standalone pcbnew (its own api-<pid>.sock).
        # Record its PID so manage_kicad_ui(action=quit) can terminate this
        # second instance the server spawned — otherwise the server has no way
        # to release the extra board it opened.
        if is_board:
            KiCADProcessManager._record_launched_pid(getattr(proc, "pid", None))
        out["fileOpenAttempts"].append({"method": "spawn", "argv": list(argv)})
        out["fileOpenForwarded"] = True
        out["fileOpenMethod"] = "spawn"
    except Exception as exc:
        logger.warning(f"spawn fallback for file-open failed: {exc}")
        out["fileOpenAttempts"].append({"method": "spawn", "error": str(exc)})
        out["warning"] = (
            "KiCad is running but the MCP couldn't forward the file-open: "
            f"{exc}.  Open the file manually in KiCad or close KiCad and "
            "call manage_kicad_ui(action=launch) again."
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
        # Cross-backend sync flags — documented as always present on
        # get_backend_info so callers can pre-empt the needs_reconcile gate
        # (finding F10).  Set before the ipc/swig branch split so BOTH return
        # paths carry them.
        "ipcWritesPending": bool(getattr(iface, "_ipc_writes_pending", False)),
        "swigWritesLanded": bool(getattr(iface, "_swig_writes_landed", False)),
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
    # C8 truthfulness: most IPC-only tools self-heal by auto-launching KiCad on
    # first use (the ipc_gate path, unless KICAD_AUTO_LAUNCH=false); run_action
    # is the deliberate exception — it refuses without IPC and only launches
    # when the caller opts in with allowLaunch:true.  Spell that out so the
    # "unavailable" list doesn't imply every one of these tools behaves alike.
    response["unavailable_tools_note"] = (
        "Most of these auto-launch KiCad on first use to self-heal (unless "
        "KICAD_AUTO_LAUNCH=false). Exception: run_action requires IPC and "
        "refuses without it — it only launches KiCad when called with "
        "allowLaunch:true."
    )
    # Truthful, non-sticky detection: is_running() shares its strict process
    # check with manage_kicad_ui (get_process_info), so kicad_running here can
    # no longer contradict manage_kicad_ui's status (the P5 false positive).
    # The IPC socket is a corroborating signal for the guidance branch only —
    # never trusted alone to declare KiCad "running" (a stale api.sock can
    # linger after a crash), so it does not flip kicad_running.
    kicad_running = KiCADProcessManager.is_running()
    socket_live = KiCADProcessManager.is_ipc_socket_live()
    response["kicad_running"] = kicad_running
    response["ipcSocketPresent"] = socket_live
    if not kicad_running:
        response["message"] = (
            "On SWIG backend — KiCad isn't running. "
            f"Start KiCad (or call manage_kicad_ui(action=launch)) to enable "
            f"IPC and unlock {unavailable_count} IPC-only tools."
        )
        response["recommendation"] = (
            "Call ``manage_kicad_ui(action=launch)`` to start KiCad with IPC attached.  "
            "Alternatively start KiCad manually (any platform) — the next "
            "``get_backend_info`` call will retry the attach automatically.  "
            "Without IPC you lose: realtime UI sync (changes won't appear "
            "until KiCAD reloads the file), atomic transactions, the "
            f"selection API, and {unavailable_count} IPC-only tools "
            "(see unavailable_tools)."
        )
    else:
        socket_note = (
            "" if socket_live else "  (No IPC socket is present, so the server is most likely off.)"
        )
        response["message"] = (
            "On SWIG backend — KiCad is running but its IPC API server "
            "isn't reachable.  Enable it in KiCAD: Preferences → Plugins → "
            "Enable IPC API Server, then re-call get_backend_info." + socket_note
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
    detect ``RAS_INVALID`` vs ``RAS_FRAME_NOT_OPEN`` and recover.  On KiCad 10
    the working namespace is ``common.Control.<action>`` (e.g.
    ``common.Control.zoomFitScreen`` / ``common.Control.zoomFitObjects``); the
    older ``pcbnew.EditorControl.*`` / ``pcbnew.Control.*`` prefixes return
    ``RAS_INVALID`` (finding D4).

    Requires the IPC backend; SWIG has no equivalent.  Unlike the board-op
    self-heal path, run_action does NOT auto-launch KiCad by default (C8): when
    IPC is unavailable it refuses cleanly rather than opening a heavyweight GUI
    as a side effect.  Pass ``allowLaunch: true`` to opt into launching KiCad.
    """
    if not iface.use_ipc or not iface.ipc_backend:
        # Action names can target any frame (project manager / PCB / schematic
        # editor / plugin), so we don't require the PCB editor specifically —
        # kipy will report RAS_FRAME_NOT_OPEN with the action name if needed.
        # allow_launch defaults OFF so this escape hatch never spawns a GUI
        # behind the caller's back (C8); the caller must opt in explicitly.
        allow_launch = bool(params.get("allowLaunch", False))
        ok, reason = iface.ensure_ipc(allow_launch=allow_launch, require_pcb_editor=False)
        if not ok:
            opt_in = (
                ""
                if allow_launch
                else (
                    " Pass allowLaunch:true to have the server start KiCAD for "
                    "you (opens the heavyweight GUI), or launch it yourself with "
                    "manage_kicad_ui(action=launch)."
                )
            )
            # Message keeps the "requires the IPC backend" phrasing so
            # enrich_failure stamps the stable IPC_REQUIRED errorCode.
            return {
                "success": False,
                "message": ("run_action requires the IPC backend. " + reason + opt_in),
            }
    action = params.get("action")
    if not isinstance(action, str) or not action:
        return {"success": False, "message": "'action' parameter is required (string)"}
    try:
        result = iface.ipc_backend.run_action(action)
    except Exception as e:
        logger.error(f"Error invoking action {action!r}: {e}")
        return {"success": False, "action": action, "message": str(e)}
    # D5: an unknown/invalid action NAME surfaces as RAS_INVALID — a CLIENT
    # error the caller fixes by retrying with a valid name, NOT an internal
    # fault.  Stamp a stable INVALID_ACTION errorCode (enrich_failure won't
    # override a preset code) while KEEPING statusName so the documented
    # retry contract still holds.
    if (
        isinstance(result, dict)
        and result.get("success") is False
        and result.get("statusName") == "RAS_INVALID"
    ):
        result.setdefault("errorCode", "INVALID_ACTION")
        result.setdefault(
            "hint",
            "Unknown TOOL_ACTION name. On KiCad 10 use the 'common.Control.<action>' "
            "namespace (e.g. common.Control.zoomFitScreen); 'pcbnew.EditorControl.*' "
            "does not resolve. Retry with a valid name.",
        )

    # E: a quit action returns RAS_OK even when KiCad never actually quits
    # (verified no-op on 10.0.4 — the app ignores the request, likely a modal
    # confirm).  Never report RAS_OK-as-success for a quit without verifying the
    # process really exited: poll the IPC connection (a real quit tears the
    # socket down).  Still alive → override to success:false and say so.
    if isinstance(result, dict) and result.get("success") is True and "quit" in action.lower():
        if _ipc_still_alive_after(iface, timeout_s=3.0):
            result["success"] = False
            result["quitVerified"] = False
            result["errorCode"] = "QUIT_NOOP"
            result["message"] = (
                f"run_action({action!r}) returned RAS_OK but KiCad is still "
                "running — quit over IPC is a known no-op on KiCad 10.0.4 (the "
                "app ignores the request, likely awaiting a modal confirmation). "
                "To close a GUI the server launched, use "
                "manage_kicad_ui(action=quit); otherwise ask the user to quit "
                "KiCad (File → Quit)."
            )
            result.setdefault(
                "hint",
                "Don't trust RAS_OK for quit. Use manage_kicad_ui(action=quit) "
                "for a server-launched GUI, or have the user close KiCad.",
            )
        else:
            result["quitVerified"] = True
    return result


def _ipc_still_alive_after(iface: "KiCADInterface", timeout_s: float = 3.0) -> bool:
    """True if the IPC connection is still responsive after ``timeout_s``.

    Used to verify a quit action: a real quit tears the socket down, so a
    connection that stays live means the quit was ignored.  Returns True
    (assume still alive → surface the no-op) when there's no backend to probe,
    so we never mislabel an unverifiable quit as successful.
    """
    import time as _time

    ipc_backend = getattr(iface, "ipc_backend", None)
    if ipc_backend is None or not hasattr(ipc_backend, "is_connected"):
        return True
    deadline = _time.monotonic() + max(0.0, timeout_s)
    while _time.monotonic() < deadline:
        try:
            if not ipc_backend.is_connected():
                return False
        except Exception:
            return False
        _time.sleep(0.25)
    try:
        return bool(ipc_backend.is_connected())
    except Exception:
        return False


def handle_quit_kicad_ui(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Terminate the KiCad GUI that THIS server launched (manage_kicad_ui action=quit).

    Symmetric with ``launch`` (finding D6): a caller that brought KiCad up
    through the server can shut it down through the server, releasing the
    shared ``/tmp/kicad/api.sock`` and returning to the idle state.

    Safety: only ever signals a process the server itself recorded at launch
    (``KiCADProcessManager._launched_pids``) AND that is *currently* a running
    KiCad GUI binary — never an externally started KiCad (killing a user's own
    editor risks data loss), and never a reused PID that is no longer a GUI.
    Escalation is SIGTERM → bounded wait → SIGKILL.  The response reports every
    case truthfully: what we terminated, a GUI we did NOT launch left running,
    a launched GUI that had already exited, or nothing running at all.
    """
    result = KiCADProcessManager.terminate_launched()

    terminated = result.get("terminated") or []
    already_exited = result.get("alreadyExited") or []
    external = result.get("externalGuiPids") or []
    survived = result.get("survived") or []

    if terminated:
        # The IPC connection that GUI hosted is now dead — drop the backend so
        # the next call cleanly re-probes / falls back to SWIG instead of
        # erroring against a stale socket.
        try:
            iface.use_ipc = False
            iface.ipc_backend = None
            iface.ipc_board_api = None
        except Exception:  # pragma: no cover - defensive
            pass

    if survived:
        success = False
        message = (
            f"Sent SIGTERM+SIGKILL but {len(survived)} launched KiCad "
            f"process(es) {survived} are still running; terminate them manually."
        )
    elif terminated:
        success = True
        note = " (SIGKILL required)" if result.get("forced") else ""
        message = f"Terminated the KiCad GUI the server launched: pid(s) {terminated}{note}."
    elif external:
        success = True
        message = (
            f"A KiCad GUI is running (pid(s) {external}) but the server did not "
            "launch it, so it was left untouched. Close it from KiCad (File → "
            "Quit) if that was intended."
        )
    elif already_exited:
        success = True
        message = "The KiCad GUI the server launched has already exited; nothing to " "terminate."
    else:
        success = True
        message = "No KiCad GUI is running; nothing to terminate."

    out: Dict[str, Any] = {"success": success, **result, "message": message}
    try:
        out.update(iface._backend_status())
    except Exception:  # pragma: no cover - defensive
        pass
    return out


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
        # Besides MCP-tracked SWIG writes, the .kicad_pcb may have been
        # edited by an external actor (text editor, git, a script) that no
        # runtime flag knows about.  Compare the on-disk content signature
        # against the one recorded at load/save: a mismatch means disk has
        # content neither backend memory includes — treat it exactly like a
        # landed SWIG write (KiCad must revert; the SWIG board must reload).
        disk_changed_externally = False
        board_path = None
        try:
            board_path = iface.board.GetFileName() if iface.board else None
        except Exception:
            board_path = None
        if board_path:
            expected = getattr(iface, "_board_disk_signature", None)
            current = iface._disk_signature(board_path)
            if expected is not None and current is not None and expected[1] != current[1]:
                disk_changed_externally = True
                logger.info(
                    "reconcile_backends: on-disk board changed externally; "
                    "treating as swig_to_ipc content"
                )

        # Nothing landed on the SWIG side → nothing to push into KiCad.
        if not iface._swig_writes_landed and not disk_changed_externally:
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
        steps_taken = ["ipc_revert"]
        # External disk edits also make the SWIG in-memory board stale —
        # reload it so both backends reflect the on-disk content.
        if disk_changed_externally and board_path:
            reloaded = iface._safe_load_board(board_path)
            if reloaded is not None:
                iface.board = reloaded
                iface._update_command_handlers()
                steps_taken.append("swig_reload")
            iface._record_board_signature(board_path)
        iface._swig_writes_landed = False
        iface._ipc_writes_pending = False
        return {
            "success": True,
            "direction": "swig_to_ipc",
            "stepsTaken": steps_taken,
            "externalDiskChange": disk_changed_externally,
            "message": (
                "Reloaded KiCad's in-memory board from disk via revert"
                + (
                    "; the SWIG board was reloaded too (external disk edit)"
                    if disk_changed_externally
                    else "; KiCad now reflects the SWIG-written content."
                )
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

    steps_taken = []

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
