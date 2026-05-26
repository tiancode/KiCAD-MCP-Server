"""
Project-lifecycle handlers — create/open/snapshot.

`open_project` and `create_project` here wrap the corresponding
`ProjectCommands` methods because we have to refresh the project-scope
symbol library after a successful load (so subsequent search_symbols
calls see the project's local sym-lib-table).  The post-call SWIG
dehydration recovery + board signature recording still lives in the
dispatcher (`KiCADInterface.handle_command`) because it has to update
`iface.board` itself.

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
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


def handle_open_project(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap project_commands.open_project so project-scope symbol libraries
    become visible to subsequent search_symbols / list_symbol_libraries /
    get_symbol_info calls."""
    result = iface.project_commands.open_project(params)
    if result.get("success"):
        project_info = result.get("project") or {}
        project_path = iface._project_path_from_filename(
            project_info.get("path") or project_info.get("boardPath") or params.get("filename")
        )
        iface._refresh_symbol_library_for_project(project_path)
    return result


def handle_create_project(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap project_commands.create_project for the same reason as open_project."""
    result = iface.project_commands.create_project(params)
    if result.get("success"):
        project_info = result.get("project") or {}
        project_path = iface._project_path_from_filename(
            project_info.get("path")
            or project_info.get("boardPath")
            or params.get("path")
            or params.get("filename")
        )
        iface._refresh_symbol_library_for_project(project_path)
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
