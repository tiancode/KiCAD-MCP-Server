"""
Project-lifecycle handlers — create/open/snapshot.

`open_project` and `create_project` here wrap the corresponding
`ProjectCommands` methods because we have to refresh the project-scope
symbol library after a successful load (so subsequent search_symbols
calls see the project's local sym-lib-table) and opportunistically
launch the KiCAD UI so the IPC backend can attach — without UI, the
agent silently degrades to SWIG and loses realtime sync, transactions,
selection, and 25+ IPC-only tools.

The post-call SWIG dehydration recovery + board signature recording
still lives in the dispatcher (`KiCADInterface.handle_command`) because
it has to update `iface.board` itself.

`snapshot_project` copies the entire project directory plus a session
log + prompt into <project>/snapshots/<name>_snapshot_<step>_<ts>/.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from utils.kicad_process import check_and_launch_kicad

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


def _autolaunch_disabled(params: Dict[str, Any]) -> Optional[str]:
    """Return a reason string if auto-launch is suppressed, else None.

    The user-facing knob is ``autoLaunch`` in params (default True).  The
    operator-facing knob is the ``KICAD_AUTO_LAUNCH`` env var (set to
    "false" to opt out of all auto-launching).  ``KICAD_BACKEND=swig``
    also disables, since the user has explicitly chosen the no-IPC path.
    """
    if params.get("autoLaunch") is False:
        return "autoLaunch=false passed explicitly"
    if os.environ.get("KICAD_AUTO_LAUNCH", "").strip().lower() == "false":
        return "KICAD_AUTO_LAUNCH=false in environment"
    if os.environ.get("KICAD_BACKEND", "").strip().lower() == "swig":
        return "KICAD_BACKEND=swig in environment"
    return None


def _resolve_project_launch_file(raw_path: Optional[str]) -> Optional[Path]:
    """Resolve a raw path string to the file KiCAD should be CLI-launched with.

    KiCAD's CLI opens the project pointed at by the path argument; if we pass
    a directory it just lands in the project manager and the user's new
    project never opens.  Callers feed this helper whichever string they have
    (.kicad_pro / .kicad_pcb / .kicad_sch / a directory).  We hand back the
    sibling .kicad_pro when possible so KiCAD opens the project, not a bare
    PCB or schematic.

    Returns None when there's nothing reasonable to launch with (KiCAD will
    then start to the welcome screen).
    """
    if not raw_path:
        return None
    try:
        p = Path(raw_path).expanduser()
    except Exception:
        return None

    if p.suffix == ".kicad_pro":
        return p
    if p.suffix in (".kicad_pcb", ".kicad_sch"):
        sibling = p.with_suffix(".kicad_pro")
        return sibling if sibling.exists() else p
    if p.is_dir():
        pros = list(p.glob("*.kicad_pro"))
        return pros[0] if len(pros) == 1 else None
    return p


def _expected_board_path(project_file: Path) -> Optional[Path]:
    """The .kicad_pcb that a freshly opened/created project_file maps to."""
    if project_file.suffix == ".kicad_pcb":
        return project_file
    if project_file.suffix == ".kicad_pro":
        return project_file.with_suffix(".kicad_pcb")
    return None


def _autolaunch_for_project(
    iface: "KiCADInterface",
    project_file: Optional[Path],
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """Launch KiCAD (if not running) pointing at ``project_file`` and
    opportunistically attach the IPC backend.  Returns a status dict (always
    the same shape — no key set varies across branches) suitable for
    embedding in the parent response as ``kicadUi``.

    Never raises and never blocks beyond the underlying launch poll —
    if KiCAD's IPC server isn't ready yet, the next IPC-capable tool
    call will re-attempt the connection via ``_try_enable_ipc_backend``.

    Includes a cross-project safety check: if IPC attaches to a KiCAD that
    has a DIFFERENT project loaded (because the user opened B while A was
    already open), we tear the IPC attachment back down and surface a
    warning rather than silently routing the agent's mutations to A.
    """
    base: Dict[str, Any] = {
        "attempted": False,
        "skipped": False,
        "alreadyRunning": False,
        "launched": False,
        "running": False,
        "ipcAttached": False,
        # ipcAttached=true with pcbDocumentOpen=false used to be the trap
        # behind silent 0×0 get_board_info / "Failed to move component"
        # results: the IPC socket was up but KiCad had no .kicad_pcb loaded,
        # so every board op acted on an empty stub.  Surface the document
        # state explicitly so callers don't have to discover it the hard way.
        "pcbDocumentOpen": False,
        "message": None,
        "reason": None,
        "error": None,
        "warning": None,
        "projectMismatch": None,
    }

    disabled = _autolaunch_disabled(params)
    if disabled:
        base["skipped"] = True
        base["reason"] = disabled
        return base

    base["attempted"] = True
    try:
        launch_info = check_and_launch_kicad(project_file, auto_launch=True)
    except Exception as exc:  # pragma: no cover - never fail the parent op
        logger.warning("Auto-launch of KiCAD failed: %s", exc)
        base["error"] = str(exc)
        return base

    launched = bool(launch_info.get("launched"))
    running = bool(launch_info.get("running"))
    base["launched"] = launched
    base["running"] = running
    base["alreadyRunning"] = running and not launched
    base["message"] = launch_info.get("message")

    if not running:
        return base

    try:
        # Force the connection attempt when we just spawned KiCAD — process
        # detection lags via /proc, so is_running() may be False even though
        # launch returned success.  When KiCAD was already running, the
        # default non-forced path is enough.
        ipc_attached = iface._try_enable_ipc_backend(force=launched)
    except Exception as exc:  # pragma: no cover - best effort
        logger.info("IPC attach after auto-launch failed: %s", exc)
        ipc_attached = False

    if not ipc_attached:
        return base

    # Cross-project safety: if KiCAD currently has a different project
    # loaded, IPC-routed mutations would silently target the wrong board.
    # Disengage IPC so the dispatcher falls back to SWIG for this project,
    # and tell the agent loudly.
    expected = _expected_board_path(project_file) if project_file else None
    actual = None
    try:
        cur = iface._current_board_path()
        actual = Path(cur) if cur else None
    except Exception:
        actual = None

    if expected is not None and actual is not None:
        try:
            mismatch = actual.resolve() != expected.resolve()
        except OSError:
            mismatch = str(actual) != str(expected)
        if mismatch:
            iface.ipc_board_api = None
            iface.use_ipc = False
            base["ipcAttached"] = False
            base["projectMismatch"] = {
                "ipcBoardPath": str(actual),
                "expectedBoardPath": str(expected),
            }
            base["warning"] = (
                "KiCAD already has a DIFFERENT project open "
                f"({actual}); refusing to attach IPC to it because mutations "
                f"would silently target the wrong board instead of "
                f"{expected}. Close the other project in KiCAD (or use "
                "ipc_save / save_project there first) and call "
                "launch_kicad_ui to retry."
            )
            logger.warning(base["warning"])
            return base

    base["ipcAttached"] = True
    try:
        base["pcbDocumentOpen"] = bool(iface._ipc_has_open_board_document())
    except Exception:
        base["pcbDocumentOpen"] = False
    if not base["pcbDocumentOpen"]:
        # Auto-launching `kicad <project.kicad_pro>` opens the project
        # manager but not the PCB editor frame.  The IPC socket attaches
        # immediately, which used to make ipcAttached:true look like
        # "everything's ready" — but the very next move_component etc.
        # would fail because no board doc is loaded.  Spell it out.
        base["warning"] = (
            "IPC is attached but KiCad has no .kicad_pcb document open. "
            "Subsequent board ops will be refused with needs_pcb_editor:true "
            "until the user opens the PCB editor in KiCad (or you call "
            "open_project with the .kicad_pcb path)."
        )
    return base


def _launch_file_for_result(result: Dict[str, Any], params: Dict[str, Any]) -> Optional[Path]:
    """Pick the right path to feed to KiCAD's CLI from a create/open result."""
    project_info = result.get("project") or {}
    raw = (
        project_info.get("path")
        or project_info.get("boardPath")
        or params.get("path")
        or params.get("filename")
    )
    return _resolve_project_launch_file(raw)


def handle_open_project(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap project_commands.open_project so project-scope symbol libraries
    become visible to subsequent search_symbols / list_symbol_libraries /
    get_symbol_info calls, and auto-launch the KiCAD UI by default so the
    IPC backend can attach (opt out via ``autoLaunch=false`` or
    ``KICAD_AUTO_LAUNCH=false``)."""
    result = iface.project_commands.open_project(params)
    if result.get("success"):
        project_info = result.get("project") or {}
        project_dir = iface._project_path_from_filename(
            project_info.get("path") or project_info.get("boardPath") or params.get("filename")
        )
        iface._refresh_symbol_library_for_project(project_dir)
        result["kicadUi"] = _autolaunch_for_project(
            iface, _launch_file_for_result(result, params), params
        )
    return result


def handle_create_project(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap project_commands.create_project for the same reason as open_project,
    and auto-launch the KiCAD UI by default so the IPC backend can attach
    (opt out via ``autoLaunch=false`` or ``KICAD_AUTO_LAUNCH=false``)."""
    result = iface.project_commands.create_project(params)
    if result.get("success"):
        project_info = result.get("project") or {}
        project_dir = iface._project_path_from_filename(
            project_info.get("path")
            or project_info.get("boardPath")
            or params.get("path")
            or params.get("filename")
        )
        iface._refresh_symbol_library_for_project(project_dir)
        result["kicadUi"] = _autolaunch_for_project(
            iface, _launch_file_for_result(result, params), params
        )
    return result


def handle_snapshot_project(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Copy the entire project folder to a snapshot directory for checkpoint/resume."""
    try:
        step = params.get("step", "")
        label = params.get("label", "")
        prompt_text = params.get("prompt", "")
        # Determine project directory from loaded board or explicit path
        project_dir = None
        if iface.board:
            board_file = iface.board.GetFileName()
            if board_file:
                project_dir = str(Path(board_file).parent)
        if not project_dir:
            project_dir = params.get("projectPath")
        if not project_dir or not os.path.isdir(project_dir):
            return {
                "success": False,
                "message": "Could not determine project directory for snapshot",
            }

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Save prompt + log into logs/ subdirectory before snapshotting
        logs_dir = Path(project_dir) / "logs"
        logs_dir.mkdir(exist_ok=True)

        prompt_file = None
        if prompt_text:
            prompt_filename = f"PROMPT_step{step}_{ts}.md" if step else f"PROMPT_{ts}.md"
            prompt_file = logs_dir / prompt_filename
            prompt_file.write_text(prompt_text, encoding="utf-8")
            logger.info(f"Prompt saved: {prompt_file}")

        # Copy current MCP session log into logs/ before snapshotting
        system = platform.system()
        if system == "Windows":
            mcp_log_dir = os.path.join(os.environ.get("APPDATA", ""), "Claude", "logs")
        elif system == "Darwin":
            mcp_log_dir = os.path.expanduser("~/Library/Logs/Claude")
        else:
            mcp_log_dir = os.path.expanduser("~/.config/Claude/logs")
        mcp_log_src = os.path.join(mcp_log_dir, "mcp-server-kicad.log")
        mcp_log_dest = None
        if os.path.exists(mcp_log_src):
            with open(mcp_log_src, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            session_start = 0
            for i, line in enumerate(all_lines):
                if "Initializing server" in line:
                    session_start = i
            session_lines = all_lines[session_start:]
            log_filename = f"mcp_log_step{step}_{ts}.txt" if step else f"mcp_log_{ts}.txt"
            mcp_log_dest = logs_dir / log_filename
            with open(mcp_log_dest, "w", encoding="utf-8") as f:
                f.writelines(session_lines)
            logger.info(f"MCP session log saved: {mcp_log_dest} ({len(session_lines)} lines)")

        base_name = Path(project_dir).name
        suffix_parts = [p for p in [f"step{step}" if step else "", label, ts] if p]
        snapshot_name = base_name + "_snapshot_" + "_".join(suffix_parts)
        snapshots_base = Path(project_dir) / "snapshots"
        snapshots_base.mkdir(exist_ok=True)
        snapshot_dir = str(snapshots_base / snapshot_name)

        shutil.copytree(project_dir, snapshot_dir, ignore=shutil.ignore_patterns("snapshots"))
        logger.info(f"Project snapshot saved: {snapshot_dir}")
        return {
            "success": True,
            "message": f"Snapshot saved: {snapshot_name}",
            "snapshotPath": snapshot_dir,
            "sourceDir": project_dir,
            "promptSaved": str(prompt_file) if prompt_file else None,
            "mcpLogSaved": str(mcp_log_dest) if mcp_log_dest else None,
        }
    except Exception as e:
        logger.error(f"snapshot_project error: {e}")
        return {"success": False, "message": str(e)}
