"""
Routing-domain handlers that aren't already covered by direct method
references to `routing_commands.*` in the command_routes table.

For now this module owns the single inline routing handler
`refill_zones`, which is non-trivial because it runs pcbnew's
ZONE_FILLER in an isolated subprocess (the in-process call can SIGSEGV
on SWIG-only KiCAD installs — issue history in the docstring below).
The simpler routing commands stay as direct references to
`commands.routing.RoutingCommands` in the dispatcher.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


def handle_refill_zones(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Refill all copper pour zones on the board.

    pcbnew.ZONE_FILLER.Fill() can cause a C++ access violation (0xC0000005)
    that crashes the entire Python process when called from SWIG outside the
    KiCAD UI.  To avoid killing the main process we run the fill in an
    isolated subprocess.  If the subprocess crashes or times out, we return
    a non-fatal warning so the caller can continue — KiCAD Pcbnew will
    refill zones automatically when the board is opened (press B).
    """
    logger.info("Refilling zones (subprocess isolation)")
    try:
        if not iface.board:
            return {
                "success": False,
                "message": "No board is loaded",
                "errorDetails": "Load or create a board first",
            }

        # First save the board so the subprocess can load it fresh
        board_path = iface.board.GetFileName()
        if not board_path:
            return {
                "success": False,
                "message": "Board has no file path — save first",
            }
        iface.board.Save(board_path)

        zone_count = iface.board.GetAreaCount() if hasattr(iface.board, "GetAreaCount") else 0

        script = textwrap.dedent(
            f"""
            import pcbnew, sys
            board = pcbnew.LoadBoard({board_path!r})
            filler = pcbnew.ZONE_FILLER(board)
            filler.Fill(board.Zones())
            board.Save({board_path!r})
            print("ok")
            """
        )
        try:
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0 and "ok" in result.stdout:
                # Reload board after subprocess modified it
                reloaded = iface._safe_load_board(board_path)
                if reloaded is None:
                    return {
                        "success": False,
                        "message": (
                            "Zone fill subprocess succeeded but the board "
                            "could not be reloaded into pcbnew (SWIG state "
                            "is corrupt — restart the MCP server)"
                        ),
                        "zoneCount": zone_count,
                    }
                iface.board = reloaded
                iface._update_command_handlers()
                logger.info("Zone fill subprocess succeeded")
                return {
                    "success": True,
                    "message": f"Zones refilled successfully ({zone_count} zones)",
                    "zoneCount": zone_count,
                }
            else:
                logger.warning(
                    f"Zone fill subprocess failed: rc={result.returncode} "
                    f"stderr={result.stderr[:200]}"
                )
                return {
                    "success": False,
                    "message": (
                        "Zone fill failed in subprocess — zones are defined and will "
                        "fill when opened in KiCAD (press B). Continuing is safe."
                    ),
                    "zoneCount": zone_count,
                    "details": (result.stderr[:300] if result.stderr else result.stdout[:300]),
                }
        except subprocess.TimeoutExpired:
            logger.warning("Zone fill subprocess timed out after 60s")
            return {
                "success": False,
                "message": (
                    "Zone fill timed out — zones are defined and will fill when opened in "
                    "KiCAD (press B). Continuing is safe."
                ),
                "zoneCount": zone_count,
            }

    except Exception as e:
        logger.error(f"Error refilling zones: {str(e)}")
        return {"success": False, "message": str(e)}
