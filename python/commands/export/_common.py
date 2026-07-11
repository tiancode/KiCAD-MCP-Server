"""Shared export helpers: kicad-cli discovery and dev log copy.

Split out of the former monolithic commands/export.py.
"""

import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger("kicad_interface")


class CommonMixin:
    def _find_kicad_cli(self) -> Optional[str]:
        """Find kicad-cli executable (see utils.kicad_cli.find_kicad_cli)."""
        from utils.kicad_cli import find_kicad_cli

        return find_kicad_cli()

    def _dev_copy_mcp_log(self, output_dir: str) -> None:
        """DEV MODE: Copy the MCP server log for the current session into the project folder.

        Activated by env var KICAD_MCP_DEV=1.
        The log is placed alongside the Gerber output as:
            <project_dir>/mcp_log_<YYYYMMDD_HHMMSS>.txt

        Only lines from the current server session (today's date) are included
        to keep the file focused on the relevant run.
        """
        import platform

        # Resolve Claude log path per platform
        system = platform.system()
        if system == "Windows":
            log_dir = os.path.join(os.environ.get("APPDATA", ""), "Claude", "logs")
        elif system == "Darwin":
            log_dir = os.path.expanduser("~/Library/Logs/Claude")
        else:
            log_dir = os.path.expanduser("~/.config/Claude/logs")

        log_src = os.path.join(log_dir, "mcp-server-kicad.log")
        if not os.path.exists(log_src):
            logger.warning(f"[DEV] MCP log not found at: {log_src}")
            return

        # Project dir = parent of outputDir (the Gerber subfolder)
        project_dir = os.path.dirname(output_dir)

        # Extract only lines from the current session start (find last "Initializing server")
        with open(log_src, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        # Find last occurrence of server start so we get only the current run
        session_start = 0
        for i, line in enumerate(all_lines):
            if "Initializing server" in line:
                session_start = i

        session_lines = all_lines[session_start:]

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        from pathlib import Path

        logs_dir = Path(project_dir) / "logs"
        logs_dir.mkdir(exist_ok=True)
        dest = str(logs_dir / f"mcp_log_{timestamp}.txt")
        with open(dest, "w", encoding="utf-8") as f:
            f.writelines(session_lines)

        logger.info(f"[DEV] MCP session log saved to: {dest} ({len(session_lines)} lines)")
