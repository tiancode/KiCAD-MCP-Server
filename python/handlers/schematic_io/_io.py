"""Schematic create / load / export(svg,pdf) / sync handlers.

Split out of the former handlers/schematic_io.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Tuple

import pcbnew  # type: ignore[import-not-found]
import sexpdata
from commands.schematic import SchematicManager

if TYPE_CHECKING:
    from pathlib import Path

    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.schematic_io")


def handle_sync_schematic_to_board(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Sync schematic netlist to PCB board (equivalent to KiCAD F8 'Update PCB from Schematic').
    Reads net connections from the schematic and assigns them to the matching pads in the PCB.
    """
    logger.info("Syncing schematic to board")
    try:
        from pathlib import Path

        schematic_path = params.get("schematicPath")
        board_path = params.get("boardPath")

        # Determine board to work with
        board = None
        if board_path:
            board = iface._safe_load_board(board_path)
            if board is None:
                return {
                    "success": False,
                    "message": f"Could not load board from {board_path}",
                    "errorDetails": (
                        "pcbnew.LoadBoard failed or returned a dehydrated "
                        "SWIG proxy that could not be recovered"
                    ),
                }
        elif iface.board:
            board = iface.board
            board_path = board.GetFileName() if not board_path else board_path
        else:
            return {
                "success": False,
                "message": "No board loaded. Use open_project first or provide boardPath.",
            }

        if not board_path:
            board_path = board.GetFileName()

        # Determine schematic path if not provided
        if not schematic_path:
            sch = Path(board_path).with_suffix(".kicad_sch")
            if sch.exists():
                schematic_path = str(sch)
            else:
                project_dir = Path(board_path).parent
                sch_files = list(project_dir.glob("*.kicad_sch"))
                if sch_files:
                    schematic_path = str(sch_files[0])

        if not schematic_path or not Path(schematic_path).exists():
            return {
                "success": False,
                "message": f"Schematic not found. Provide schematicPath. Tried: {schematic_path}",
            }

        # Build hierarchical pad→net map (walks all sub-sheets)
        pad_net_map, net_names = iface._build_hierarchical_pad_net_map(schematic_path)

        # Add missing footprints from the schematic to the board *before*
        # we add nets and assign pads — F8 in KiCad does this implicitly
        # ("Update PCB from Schematic"), but our previous implementation
        # only mutated nets, leaving newly-added schematic symbols with no
        # PCB footprint at all.
        added_footprints, skipped_footprints = iface._add_missing_footprints_from_schematic(
            board, schematic_path
        )

        # Add all nets to board
        netinfo = board.GetNetInfo()
        nets_by_name = netinfo.NetsByName()
        added_nets = []
        for net_name in net_names:
            if not nets_by_name.has_key(net_name):
                net_item = pcbnew.NETINFO_ITEM(board, net_name)
                board.Add(net_item)
                added_nets.append(net_name)

        # Refresh nets map after additions
        netinfo = board.GetNetInfo()
        nets_by_name = netinfo.NetsByName()

        # Assign nets to pads (now also covers any footprints we just added)
        assigned_pads = 0
        unmatched = []
        for fp in board.GetFootprints():
            ref = fp.GetReference()
            for pad in fp.Pads():
                pad_num = pad.GetNumber()
                key = (ref, str(pad_num))
                if key in pad_net_map:
                    net_name = pad_net_map[key]
                    if nets_by_name.has_key(net_name):
                        pad.SetNet(nets_by_name[net_name])
                        assigned_pads += 1
                else:
                    unmatched.append(f"{ref}/{pad_num}")

        # Route through the iface helper so the in-memory signature tracks
        # the new on-disk hash; otherwise the dispatcher's follow-up
        # _auto_save_board() sees a mismatch and refuses the next write.
        iface._save_board_and_record(board, board_path)

        # If board was loaded fresh, update internal reference
        if params.get("boardPath"):
            iface.board = board
            iface._update_command_handlers()

        logger.info(
            f"sync_schematic_to_board: {len(added_nets)} nets added, "
            f"{len(added_footprints)} footprints added, {assigned_pads} pads assigned"
        )
        # Surface the grid-placement contract so agents know each new
        # footprint landed at a distinct position and which positions
        # they were — previously they all stacked at (0, 0) and the
        # caller had to issue N move_component calls before anything
        # was visible.
        layout_note: Optional[str] = None
        if added_footprints:
            positions = [fp.get("position") for fp in added_footprints if fp.get("position")]
            if positions:
                xs = [p["x_mm"] for p in positions]
                ys = [p["y_mm"] for p in positions]
                layout_note = (
                    f"{len(added_footprints)} new footprints grid-placed: "
                    f"x in [{min(xs)}, {max(xs)}] mm, "
                    f"y in [{min(ys)}, {max(ys)}] mm. "
                    f"Call move_component on each ref to reposition."
                )
        return {
            "success": True,
            "message": (
                f"PCB updated from schematic: {len(added_footprints)} footprints added, "
                f"{len(added_nets)} nets added, {assigned_pads} pads assigned"
            ),
            "nets_added": added_nets,
            "nets_total": len(net_names),
            "pads_assigned": assigned_pads,
            "unmatched_pads_sample": unmatched[:10],
            "footprints_added": added_footprints,
            "footprints_skipped": skipped_footprints,
            "layout_note": layout_note,
        }

    except Exception as e:
        logger.error(f"Error in sync_schematic_to_board: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_export_schematic_svg(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Export schematic to SVG using kicad-cli"""
    logger.info("Exporting schematic SVG")
    import glob
    import shutil
    import subprocess

    try:
        schematic_path = params.get("schematicPath")
        output_path = params.get("outputPath")

        if not schematic_path or not output_path:
            return {
                "success": False,
                "message": "schematicPath and outputPath are required",
            }

        if not os.path.exists(schematic_path):
            return {
                "success": False,
                "message": f"Schematic not found: {schematic_path}",
            }

        # kicad-cli's --output flag for SVG export expects a directory, not a file path.
        # The output file is auto-named based on the schematic name.
        output_dir = os.path.dirname(output_path)
        if not output_dir:
            output_dir = "."

        os.makedirs(output_dir, exist_ok=True)

        cmd = [
            "kicad-cli",
            "sch",
            "export",
            "svg",
            schematic_path,
            "-o",
            output_dir,
        ]

        if params.get("blackAndWhite"):
            cmd.append("--black-and-white")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode != 0:
            return {
                "success": False,
                "message": f"kicad-cli failed: {result.stderr}",
            }

        # kicad-cli names the file after the schematic, so find the generated SVG
        svg_files = glob.glob(os.path.join(output_dir, "*.svg"))
        if not svg_files:
            return {
                "success": False,
                "message": "No SVG file produced by kicad-cli",
            }

        generated_svg = svg_files[0]

        # Move/rename to the user-specified output path if it differs
        if os.path.abspath(generated_svg) != os.path.abspath(output_path):
            shutil.move(generated_svg, output_path)

        return {"success": True, "file": {"path": output_path}}

    except FileNotFoundError:
        return {"success": False, "message": "kicad-cli not found in PATH"}
    except Exception as e:
        logger.error(f"Error exporting schematic SVG: {e}")
        return {"success": False, "message": str(e)}


def handle_export_schematic_pdf(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Export schematic to PDF"""
    logger.info("Exporting schematic to PDF")
    try:
        schematic_path = params.get("schematicPath")
        output_path = params.get("outputPath")

        if not schematic_path:
            return {"success": False, "message": "Schematic path is required"}
        if not output_path:
            return {"success": False, "message": "Output path is required"}

        if not os.path.exists(schematic_path):
            return {
                "success": False,
                "message": f"Schematic not found: {schematic_path}",
            }

        import subprocess

        cmd = [
            "kicad-cli",
            "sch",
            "export",
            "pdf",
            "--output",
            output_path,
            schematic_path,
        ]

        if params.get("blackAndWhite"):
            cmd.insert(-1, "--black-and-white")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode == 0:
            return {"success": True, "file": {"path": output_path}}
        else:
            return {
                "success": False,
                "message": f"kicad-cli failed: {result.stderr}",
            }

    except FileNotFoundError:
        return {"success": False, "message": "kicad-cli not found in PATH"}
    except Exception as e:
        logger.error(f"Error exporting schematic to PDF: {str(e)}")
        return {"success": False, "message": str(e)}


def handle_load_schematic(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Load an existing schematic"""
    logger.info("Loading schematic")
    try:
        filename = params.get("filename")

        if not filename:
            return {"success": False, "message": "Filename is required"}

        schematic = SchematicManager.load_schematic(filename)
        success = schematic is not None

        if success:
            metadata = SchematicManager.get_schematic_metadata(schematic)
            return {"success": success, "metadata": metadata}
        else:
            return {"success": False, "message": "Failed to load schematic"}
    except Exception as e:
        logger.error(f"Error loading schematic: {str(e)}")
        return {"success": False, "message": str(e)}


def handle_create_schematic(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new schematic"""
    logger.info("Creating schematic")
    try:
        # Support multiple parameter naming conventions for compatibility:
        # - TypeScript tools use: name, path
        # - Python schema uses: filename, title
        # - Legacy uses: projectName, path, metadata
        project_name = params.get("projectName") or params.get("name") or params.get("title")

        # Handle filename parameter - it may contain full path
        filename = params.get("filename")
        if filename:
            # If filename provided, extract name and path from it
            if filename.endswith(".kicad_sch"):
                filename = filename[:-10]  # Remove .kicad_sch extension
            path = os.path.dirname(filename) or "."
            project_name = project_name or os.path.basename(filename)
        else:
            path = params.get("path", ".")
        metadata = params.get("metadata", {})

        if not project_name:
            return {
                "success": False,
                "message": "Schematic name is required. Provide 'name', 'projectName', or 'filename' parameter.",
            }

        base_name = (
            project_name if project_name.endswith(".kicad_sch") else f"{project_name}.kicad_sch"
        )
        normalized_path = path or "."
        file_path = os.path.join(normalized_path, base_name)

        # Refuse to clobber an existing schematic. create_schematic copies the
        # template over file_path unconditionally, so without this guard a name
        # collision silently wipes the user's sheet.
        if not bool(params.get("overwrite", False)) and os.path.exists(file_path):
            return {
                "success": False,
                "message": (
                    f"Schematic already exists: {file_path}. "
                    "Pass overwrite=true to replace it, or choose a different name."
                ),
                "errorCode": "SCHEMATIC_EXISTS",
                "hint": "Refusing to overwrite an existing schematic. Pick a new name or set overwrite=true.",
            }

        sch_path = path if path and path != "." else None
        schematic = SchematicManager.create_schematic(
            project_name, path=sch_path, metadata=metadata
        )
        success = SchematicManager.save_schematic(schematic, file_path)

        return {"success": success, "file_path": file_path}
    except Exception as e:
        logger.error(f"Error creating schematic: {str(e)}")
        return {"success": False, "message": str(e)}
