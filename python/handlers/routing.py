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
    """Refuse the SWIG refill path by default; require ``force=True`` to attempt it.

    ``pcbnew.ZONE_FILLER.Fill()`` has a long-standing history of C++
    access violations (Windows 0xC0000005) and silently-wrong fills on
    SWIG-only KiCad installs — the very class of bug the IPC backend
    exists to avoid.  Even subprocess isolation prevents the MCP from
    crashing but doesn't guarantee a correct fill, so the safe default
    is to refuse and tell the caller how to get a trustworthy result:

      - Open the project in KiCad with IPC enabled.  ``refill_zones``
        will then route via the IPC fast-path (``handlers/ipc_fastpath
        .handle_refill_zones``) and reflect the real pcbnew result.
      - Or just let KiCad fill on open (press B): zones are already
        defined on disk, gerber export only needs the fill at export
        time — so do that step via KiCad UI.

    For callers that explicitly accept the risk (CI flow with no GUI,
    one-off scripts), pass ``force=True`` and we'll fall back to the
    subprocess-isolated SWIG fill.  Surface that the result may be
    silently wrong in ``warnings`` so the agent can choose to verify.
    """
    if not bool(params.get("force", False)):
        zone_count = (
            iface.board.GetAreaCount()
            if iface.board is not None and hasattr(iface.board, "GetAreaCount")
            else None
        )
        return {
            "success": False,
            "requires_ipc": True,
            "message": (
                "refill_zones refused on the SWIG backend: "
                "pcbnew.ZONE_FILLER has a known segfault/wrong-fill risk "
                "when invoked outside KiCAD's own process.  Open the "
                "project in KiCad with the IPC API server enabled "
                "(Preferences → Plugins → Enable IPC API Server) and "
                "retry — refill_zones will then route through the IPC "
                "fast-path.  Or let KiCad refill on open (press B) — "
                "the zone definitions are already on disk.  Pass "
                "force=true here if you accept the SWIG-path risk for a "
                "headless flow."
            ),
            "zoneCount": zone_count,
            "recommendation": (
                "Most flows that need a filled board for gerber export "
                "should call ``manage_kicad_ui(action=launch)`` (or "
                "save_project after opening KiCad) and re-run refill_zones "
                "— the IPC path is reliable.  If running headless, ``force=true`` "
                "uses subprocess isolation so the MCP won't crash, but "
                "the resulting fill may be subtly wrong; verify with "
                "run_drc or open the gerber to check."
            ),
        }

    # force=True path — subprocess-isolated SWIG fill, identical to the
    # historical behaviour.  We only reach here when the caller has
    # explicitly opted into the risk.
    logger.info("Refilling zones (subprocess isolation, force=True)")
    try:
        if not iface.board:
            return {
                "success": False,
                "message": "No board is loaded",
                "errorDetails": "Load or create a board first",
            }

        # First make sure the on-disk file and the in-memory board agree so
        # the subprocess fills the right data.  If an external actor (text
        # editor, git, another tool) modified the file since we loaded it,
        # the disk version wins: reload it instead of clobbering those edits
        # with the stale in-memory board (the subprocess reads from disk, so
        # the in-memory copy isn't needed for the fill itself).
        board_path = iface.board.GetFileName()
        if not board_path:
            return {
                "success": False,
                "message": "Board has no file path — save first",
            }
        expected = getattr(iface, "_board_disk_signature", None)
        current = iface._disk_signature(board_path)
        if expected is not None and current is not None and expected[1] != current[1]:
            logger.info(
                "refill_zones: on-disk board changed externally; reloading "
                "from disk instead of overwriting it"
            )
            reloaded = iface._safe_load_board(board_path)
            if reloaded is None:
                return {
                    "success": False,
                    "message": (
                        "The on-disk board changed externally and could not "
                        "be reloaded into pcbnew — refusing to overwrite it. "
                        "Check the file or restart the MCP server."
                    ),
                }
            iface.board = reloaded
            iface._update_command_handlers()
            iface._record_board_signature(board_path)
        else:
            iface._save_board_and_record(iface.board, board_path)

        zone_count = iface.board.GetAreaCount() if hasattr(iface.board, "GetAreaCount") else 0

        # F9: the SWIG ZONE_FILLER insets the fill from the board outline by
        # the board's copper-to-edge clearance (BOARD_DESIGN_SETTINGS
        # .m_CopperEdgeClearance).  When that setting is unset/zero the fill
        # reaches Edge.Cuts at 0.0 mm → a copper_edge_clearance DRC error.
        # Default it to 0.5 mm first, then RE-LOAD before filling: the filler
        # only honours the edge clearance read from the freshly-loaded board,
        # not an in-memory setter applied to an already-loaded board.  Zone
        # local clearance / min-thickness are untouched.
        script = textwrap.dedent(f"""
            import pcbnew, sys
            board = pcbnew.LoadBoard({board_path!r})
            ds = board.GetDesignSettings()
            edge_clr = ds.m_CopperEdgeClearance
            if not edge_clr or edge_clr <= 0:
                ds.m_CopperEdgeClearance = pcbnew.FromMM(0.5)
                board.Save({board_path!r})
                board = pcbnew.LoadBoard({board_path!r})
                print("edge_clearance_defaulted")
            filler = pcbnew.ZONE_FILLER(board)
            filler.Fill(board.Zones())
            board.Save({board_path!r})
            print("ok")
            """)
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
                # Subprocess rewrote the file; align in-memory expectation
                # so the dispatcher's auto-save doesn't refuse the next write.
                iface._record_board_signature(board_path)
                logger.info("Zone fill subprocess succeeded")
                warnings = [
                    "force=true was set; this fill ran via SWIG "
                    "subprocess isolation and may differ from KiCad's "
                    "own result.  Verify with run_drc or open the "
                    "gerber to check."
                ]
                if "edge_clearance_defaulted" in (result.stdout or ""):
                    warnings.append(
                        "Board copper-to-edge clearance was unset/zero; "
                        "defaulted to 0.5 mm before filling so the pour "
                        "does not touch Edge.Cuts (would be a "
                        "copper_edge_clearance DRC error). This design "
                        "setting is now persisted to the board."
                    )
                return {
                    "success": True,
                    "message": f"Zones refilled successfully ({zone_count} zones)",
                    "zoneCount": zone_count,
                    "warnings": warnings,
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
