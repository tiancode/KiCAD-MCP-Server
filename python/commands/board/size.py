"""
Board size command implementations for KiCAD interface
"""

import logging
from typing import Any, Dict, Optional

import pcbnew

logger = logging.getLogger("kicad_interface")


class BoardSizeCommands:
    """Handles board size operations"""

    def __init__(self, board: Optional[pcbnew.BOARD] = None):
        """Initialize with optional board instance"""
        self.board = board

    def set_board_size(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Set the size of the PCB board by creating edge cuts outline.

        By default this REPLACES any existing Edge.Cuts geometry — the verb
        is "set", not "append". Pass ``clearExisting=false`` to opt out and
        layer the new rectangle on top of whatever is already there.
        """
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            width = params.get("width")
            height = params.get("height")
            unit = params.get("unit", "mm")
            clear_existing = bool(params.get("clearExisting", True))

            if width is None or height is None:
                return {
                    "success": False,
                    "message": "Missing dimensions",
                    "errorDetails": "Both width and height are required",
                }

            # Wipe existing Edge.Cuts shapes so set_board_size + a follow-up
            # add_board_outline doesn't leave two overlapping board frames —
            # that scenario triggered 7 spurious tracks_crossing DRC errors
            # in real use.
            removed = 0
            if clear_existing:
                for d in list(self.board.GetDrawings()):
                    try:
                        if d.GetLayer() == pcbnew.Edge_Cuts:
                            self.board.Remove(d)
                            removed += 1
                    except Exception:
                        # PCB_SHAPE accessors are sometimes wonky on SWIG; skip
                        # rather than abort the resize for one stubborn shape.
                        continue

            # Create board outline using BoardOutlineCommands
            # This properly creates edge cuts on Edge.Cuts layer
            from commands.board.outline import BoardOutlineCommands

            outline_commands = BoardOutlineCommands(self.board)

            # Create rectangular outline centered at origin
            result = outline_commands.add_board_outline(
                {
                    "shape": "rectangle",
                    "centerX": width / 2,  # Center X
                    "centerY": height / 2,  # Center Y
                    "width": width,
                    "height": height,
                    "unit": unit,
                }
            )

            if result.get("success"):
                return {
                    "success": True,
                    "message": (
                        f"Created board outline: {width}x{height} {unit}"
                        + (f" (replaced {removed} existing Edge.Cuts shape(s))" if removed else "")
                    ),
                    "size": {"width": width, "height": height, "unit": unit},
                    "removedExistingShapes": removed,
                }
            else:
                return result

        except Exception as e:
            logger.error(f"Error setting board size: {str(e)}")
            return {"success": False, "message": "Failed to set board size", "errorDetails": str(e)}
