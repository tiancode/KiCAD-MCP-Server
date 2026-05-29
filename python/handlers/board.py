"""
Board handlers, extracted from kicad_interface.py.

See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

import sexpdata
from commands.component import ComponentCommands
from commands.library import LibraryManager as FootprintLibraryManager
from commands.schematic import SchematicManager
from commands.wire_manager import WireManager

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


def handle_import_svg_logo(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Import an SVG file as PCB graphic polygons on the silkscreen"""
    logger.info("Importing SVG logo into PCB")
    try:
        from commands.svg_import import import_svg_to_pcb

        pcb_path = params.get("pcbPath")
        svg_path = params.get("svgPath")
        x = float(params.get("x", 0))
        y = float(params.get("y", 0))
        width = float(params.get("width", 10))
        layer = params.get("layer", "F.SilkS")
        stroke_width = float(params.get("strokeWidth", 0))
        filled = bool(params.get("filled", True))

        if not pcb_path or not svg_path:
            return {
                "success": False,
                "message": "Missing required parameters: pcbPath, svgPath",
            }

        result = import_svg_to_pcb(pcb_path, svg_path, x, y, width, layer, stroke_width, filled)

        # import_svg_to_pcb writes gr_poly entries directly to the .kicad_pcb file,
        # bypassing the pcbnew in-memory board object.  Any subsequent board.Save()
        # call would overwrite the file with the stale in-memory state, erasing the
        # logo.  Reload the board from disk so pcbnew's memory matches the file.
        if result.get("success") and iface.board:
            reloaded = iface._safe_load_board(pcb_path)
            if reloaded is not None:
                iface.board = reloaded
                iface._update_command_handlers()
                logger.info("Reloaded board into pcbnew after SVG logo import")
            else:
                logger.warning(
                    "Board reload after SVG import failed (non-fatal); "
                    "next mutation may operate on stale in-memory state"
                )

        return result

    except Exception as e:
        logger.error(f"Error importing SVG logo: {str(e)}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_place_component(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Place a component on the PCB, with project-local fp-lib-table support.
    If boardPath is given and differs from the currently loaded board, the
    board is reloaded from boardPath before placing — prevents silent failures
    when Claude provides a boardPath that was not yet loaded.
    """
    from pathlib import Path

    board_path = params.get("boardPath")
    if board_path:
        board_path_norm = str(Path(board_path).resolve())
        current_board_file = str(Path(iface.board.GetFileName()).resolve()) if iface.board else ""
        if board_path_norm != current_board_file:
            logger.info(f"boardPath differs from current board — reloading: {board_path}")
            reloaded = iface._safe_load_board(board_path)
            if reloaded is None:
                return {
                    "success": False,
                    "message": f"Could not load board from boardPath: {board_path}",
                    "errorDetails": (
                        "pcbnew.LoadBoard failed or returned a dehydrated "
                        "SWIG proxy that could not be recovered"
                    ),
                }
            iface.board = reloaded
            iface._update_command_handlers()
            logger.info("Board reloaded from boardPath")

        project_path = Path(board_path).parent
        if project_path != getattr(iface, "_current_project_path", None):
            iface._current_project_path = project_path
            local_lib = FootprintLibraryManager(project_path=project_path)
            iface.component_commands = ComponentCommands(iface.board, local_lib)
            logger.info(f"Reloaded FootprintLibraryManager with project_path={project_path}")

    return iface.component_commands.place_component(params)
