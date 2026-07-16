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
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from utils.kicad_process import check_and_launch_kicad

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)

# How long to poll for IPC attach after we just launched KiCAD.  KiCad's
# wxApp init typically takes 3–8 s on Linux and up to ~15 s on macOS, so
# a sub-10s deadline can return a misleading ``ipcAttached: false`` to
# the caller.  Module-level so tests can monkey-patch it to 0 and avoid
# adding real-time delay to the suite.
_AUTOLAUNCH_IPC_POLL_DEADLINE_S = 10.0
_AUTOLAUNCH_IPC_POLL_INTERVAL_S = 0.5


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
        # Filled in by the attach-poll loop below.  Useful to surface so
        # callers can tell apart "took 5s, fine" from "polled 20× and
        # gave up — KiCAD is hung".
        "ipcAttachAttempts": 0,
        "ipcAttachElapsedMs": 0,
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
    # Prefer launching the standalone PCB editor (pcbnew <board>) over the bare
    # project manager (kicad <pro>) when the project actually has a board file.
    # This launch exists solely to let the IPC backend attach for board ops, and
    # a bare project manager can't serve them: it owns the IPC socket but has no
    # editor frame, so every board request comes back "KiCad is not ready to
    # reply" and the next IPC tool has to spawn pcbnew itself (ensure_ipc's
    # running-but-unusable self-heal).  Opening pcbnew here surfaces the board
    # over IPC immediately and mirrors ensure_ipc's own cold-launch (which also
    # passes the .kicad_pcb to KiCADProcessManager.launch).  A project with no
    # board still lands in the project manager, which is the sane place for it.
    launch_target = project_file
    if project_file is not None:
        board = _expected_board_path(project_file)
        if board is not None and board.is_file():
            launch_target = board
    try:
        launch_info = check_and_launch_kicad(launch_target, auto_launch=True)
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

    # IPC attach with polling when we just spawned KiCAD — its wxApp
    # init takes 3–8 s on Linux / 8–15 s on macOS, so a single-shot
    # attach right after ``launch()`` almost always returns ``False``
    # and the user sees ``ipcAttached: false`` even though the server
    # comes up a few seconds later.  Poll on a short interval up to
    # ``_AUTOLAUNCH_IPC_POLL_DEADLINE_S``; when KiCAD was already
    # running, single-shot is plenty.
    import time as _time

    poll_deadline_s = _AUTOLAUNCH_IPC_POLL_DEADLINE_S if launched else 0.0
    poll_interval_s = _AUTOLAUNCH_IPC_POLL_INTERVAL_S
    poll_started_at = _time.monotonic()
    ipc_attached = False
    poll_attempts = 0
    while True:
        poll_attempts += 1
        try:
            ipc_attached = iface._try_enable_ipc_backend(force=launched)
        except Exception as exc:  # pragma: no cover - best effort
            logger.info("IPC attach after auto-launch failed: %s", exc)
            ipc_attached = False
        if ipc_attached:
            break
        elapsed = _time.monotonic() - poll_started_at
        if elapsed >= poll_deadline_s:
            break
        _time.sleep(poll_interval_s)

    poll_elapsed_ms = int((_time.monotonic() - poll_started_at) * 1000)
    base["ipcAttachAttempts"] = poll_attempts
    base["ipcAttachElapsedMs"] = poll_elapsed_ms

    if not ipc_attached:
        # We just spawned KiCAD and it's still booting (or the IPC
        # API server is disabled in Preferences).  Tell the caller
        # exactly what to do instead of leaving them to guess from
        # ``ipcAttached: false``.
        if launched:
            base["retryAfterMs"] = 5000
            base["warning"] = (
                f"IPC attach didn't land within {int(poll_deadline_s)}s of "
                "launching KiCAD — its wxApp init can take longer on some "
                "systems.  Wait ~5 seconds and re-call get_backend_info / "
                "the failing tool; the next attempt will retry the attach.  "
                "If it still fails, open KiCAD → Preferences → Plugins → "
                "Enable IPC API Server and re-launch."
            )
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
                "save_project there first) and call "
                "manage_kicad_ui(action=launch) to retry."
            )
            logger.warning(base["warning"])
            return base

    base["ipcAttached"] = True
    try:
        base["pcbDocumentOpen"] = bool(iface._ipc_has_open_board_document())
    except Exception:
        base["pcbDocumentOpen"] = False

    # Best-effort: ask the project manager to open the PCB editor frame
    # via the IPC TOOL_ACTION API.  Action names are KiCad-internal and
    # not guaranteed stable, so we try a handful in order and stop on
    # the first ``RAS_OK``.  If none succeed (RAS_INVALID across the
    # board, older/newer KiCad with renamed actions), we fall through to
    # the manual-steps warning below — no harm done.
    if not base["pcbDocumentOpen"]:
        base["pcbEditorAutoOpenAttempted"] = True
        ipc_backend = getattr(iface, "ipc_backend", None)
        if ipc_backend is not None and getattr(ipc_backend, "is_connected", lambda: False)():
            _action_candidates = iface._PCB_EDITOR_OPEN_ACTIONS
            for action in _action_candidates:
                try:
                    result = ipc_backend.run_action(action)
                except Exception as exc:  # pragma: no cover - best-effort
                    logger.debug(f"PCB-editor auto-open via {action!r} raised: {exc}")
                    continue
                if isinstance(result, dict) and result.get("success"):
                    logger.info(f"Opened PCB editor via run_action({action!r})")
                    try:
                        base["pcbDocumentOpen"] = bool(iface._ipc_has_open_board_document())
                    except Exception:
                        pass
                    if base["pcbDocumentOpen"]:
                        base["pcbEditorAutoOpened"] = action
                        break

    if not base["pcbDocumentOpen"]:
        # Auto-launching ``kicad <project.kicad_pro>`` opens the project
        # manager but not the PCB editor frame, and the run_action
        # attempts above didn't land either (action name drift or KiCad
        # refusing the request).  Tell the user what's actually
        # required — DON'T repeat "call open_project with .kicad_pcb"
        # since that's the call that got us here in the first place.
        base["warning"] = (
            "IPC is attached but KiCad has no .kicad_pcb document open.  "
            "The MCP attempted to open the PCB editor frame via run_action "
            "but no candidate action name was accepted by this KiCad version "
            "(action names are KiCad-internal and unstable across releases).  "
            "Manual recovery: in the KiCad project manager window, "
            "double-click the PCB editor icon (top of the left sidebar) — "
            "or double-click the .kicad_pcb file in the project tree.  "
            "After that, every IPC board op (move_component, get_board_info, "
            "…) will see the open document and stop refusing with "
            "needs_pcb_editor: true."
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
        iface._refresh_footprint_library_for_project(project_dir)
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
        iface._refresh_footprint_library_for_project(project_dir)
        result["kicadUi"] = _autolaunch_for_project(
            iface, _launch_file_for_result(result, params), params
        )
    return result


# Layers rendered into the snapshot PDF — a board-overview checkpoint (both
# copper sides, silkscreen and the board outline).  All are canonical
# untranslated KiCad layer names that exist on every board.
_SNAPSHOT_PDF_LAYERS = "F.Cu,B.Cu,F.Silkscreen,B.Silkscreen,Edge.Cuts"


def _snapshot_board_file(iface: "KiCADInterface", snapshot_dir: Path) -> Optional[Path]:
    """Locate the .kicad_pcb inside the freshly-copied snapshot dir to render.

    Prefers the loaded board's basename (the copytree preserved names), else
    falls back to a lone *.kicad_pcb in the snapshot.  Returns None when the
    snapshot has no unambiguous board file (e.g. board never saved).
    """
    board = getattr(iface, "board", None)
    if board is not None:
        try:
            bf = board.GetFileName()
        except Exception:
            bf = None
        if bf:
            cand = snapshot_dir / Path(bf).name
            if cand.is_file():
                return cand
    pcbs = sorted(snapshot_dir.glob("*.kicad_pcb"))
    return pcbs[0] if len(pcbs) == 1 else None


def _render_snapshot_pdf(iface: "KiCADInterface", snapshot_dir: Path) -> Dict[str, Optional[str]]:
    """Best-effort render of the snapshot board to a PDF inside the snapshot.

    Returns ``{"pdf": <path or None>, "pdfNote": <truthful note or None>}``.
    Never raises — the snapshot itself must succeed even when the render can't
    (board unsaved, kicad-cli missing, render error); the reason is surfaced in
    ``pdfNote`` so the documented "renders board to PDF" claim stays truthful.
    """
    board_file = _snapshot_board_file(iface, snapshot_dir)
    if board_file is None:
        return {
            "pdf": None,
            "pdfNote": (
                "PDF not rendered: no saved .kicad_pcb found for this project "
                "(save the board first)."
            ),
        }

    from utils.kicad_cli import c_locale_env, find_kicad_cli

    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        return {
            "pdf": None,
            "pdfNote": ("PDF not rendered: kicad-cli not found (install KiCAD 8.0+ or set PATH)."),
        }

    pdf_out = snapshot_dir / (board_file.stem + ".pdf")
    cmd = [
        kicad_cli,
        "pcb",
        "export",
        "pdf",
        "--output",
        str(pdf_out),
        "--mode-single",
        "--layers",
        _SNAPSHOT_PDF_LAYERS,
        str(board_file),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180, env=c_locale_env()
        )
    except Exception as render_err:  # noqa: BLE001 — never fail the snapshot
        logger.warning("snapshot PDF render error: %s", render_err)
        return {"pdf": None, "pdfNote": f"PDF render failed: {render_err}"}

    if result.returncode == 0 and pdf_out.is_file() and pdf_out.stat().st_size > 0:
        logger.info("snapshot PDF rendered: %s", pdf_out)
        return {"pdf": str(pdf_out), "pdfNote": None}

    detail = result.stderr.strip() or "no output file produced"
    logger.warning("snapshot PDF render failed (exit %s): %s", result.returncode, detail)
    return {
        "pdf": None,
        "pdfNote": f"PDF render failed (kicad-cli exit {result.returncode}): {detail}",
    }


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

        # Exclude the snapshots dir itself (no recursion) plus transient
        # artifacts that only bloat a checkpoint: KiCad lock files (~*.lck),
        # the MCP's own rotating backups / edit history, VCS metadata, and
        # Python bytecode caches.  Real design files are kept.
        shutil.copytree(
            project_dir,
            snapshot_dir,
            ignore=shutil.ignore_patterns(
                "snapshots",
                "*.lck",
                ".history",
                ".mcp-backups",
                "__pycache__",
                ".git",
            ),
        )
        logger.info(f"Project snapshot saved: {snapshot_dir}")

        # Best-effort board->PDF render into the snapshot (the tool description
        # and README promise "renders board to PDF").  Degrades truthfully via
        # pdfNote when it can't run — the snapshot copy itself always succeeds.
        pdf_result = _render_snapshot_pdf(iface, Path(snapshot_dir))

        return {
            "success": True,
            "message": f"Snapshot saved: {snapshot_name}",
            "snapshotPath": snapshot_dir,
            "sourceDir": project_dir,
            "promptSaved": str(prompt_file) if prompt_file else None,
            "mcpLogSaved": str(mcp_log_dest) if mcp_log_dest else None,
            "pdf": pdf_result["pdf"],
            "pdfNote": pdf_result["pdfNote"],
        }
    except Exception as e:
        logger.error(f"snapshot_project error: {e}")
        return {"success": False, "message": str(e)}
