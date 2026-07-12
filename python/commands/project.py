"""
Project-related command implementations for KiCAD interface
"""

import json
import logging
import os
from typing import Any, Dict, Optional

import pcbnew  # type: ignore

logger = logging.getLogger("kicad_interface")


def _new_project_document(pro_filename: str) -> Dict[str, Any]:
    """Return a faithful *minimal* KiCad 10 ``.kicad_pro`` document.

    Values mirror what KiCad 10.0.4 itself writes for a brand-new project
    (captured from ``pcbnew.SaveBoard`` output), trimmed to the keys that
    matter: ``meta``, ``net_settings`` (with a full ``Default`` net class),
    ``board.design_settings`` (defaults + rule minimums), ``schematic``, and the
    small library/tool skeleton.  KiCad fills any omitted key with its own
    default on load, so this opens cleanly while staying well under the full
    ~9.5 KB dump.

    This replaces the old ~112-byte stub (E2E B7).  The net class here is the
    canonical store that ``create_netclass`` / ``set_design_rules`` edit and
    that the auto-save path must not clobber (E2E B10).
    """
    return {
        "board": {
            "design_settings": {
                "defaults": {
                    "board_outline_line_width": 0.05,
                    "copper_line_width": 0.2,
                    "copper_text_size_h": 1.5,
                    "copper_text_size_v": 1.5,
                    "copper_text_thickness": 0.3,
                    "courtyard_line_width": 0.05,
                    "dimension_precision": 4,
                    "dimension_units": 3,
                    "fab_line_width": 0.1,
                    "fab_text_size_h": 1.0,
                    "fab_text_size_v": 1.0,
                    "fab_text_thickness": 0.15,
                    "other_line_width": 0.1,
                    "other_text_size_h": 1.0,
                    "other_text_size_v": 1.0,
                    "other_text_thickness": 0.15,
                    "pads": {"drill": 0.8, "height": 1.27, "width": 1.27},
                    "silk_line_width": 0.1,
                    "silk_text_size_h": 1.0,
                    "silk_text_size_v": 1.0,
                    "silk_text_thickness": 0.1,
                },
                "meta": {"version": 2},
                "rules": {
                    "max_error": 0.005,
                    "min_clearance": 0.0,
                    "min_connection": 0.0,
                    "min_copper_edge_clearance": 0.5,
                    "min_hole_clearance": 0.25,
                    "min_hole_to_hole": 0.25,
                    "min_microvia_diameter": 0.2,
                    "min_microvia_drill": 0.1,
                    "min_resolved_spokes": 2,
                    "min_silk_clearance": 0.0,
                    "min_text_height": 0.8,
                    "min_text_thickness": 0.08,
                    "min_through_hole_diameter": 0.3,
                    "min_track_width": 0.2,
                    "min_via_annular_width": 0.1,
                    "min_via_diameter": 0.5,
                    "solder_mask_to_copper_clearance": 0.0,
                    "use_height_for_length_calcs": True,
                },
            }
        },
        "boards": [],
        "cvpcb": {"equivalence_files": []},
        "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
        "meta": {"filename": pro_filename, "version": 3},
        "net_settings": {
            "classes": [
                {
                    "bus_width": 12,
                    "clearance": 0.2,
                    "diff_pair_gap": 0.25,
                    "diff_pair_via_gap": 0.25,
                    "diff_pair_width": 0.2,
                    "line_style": 0,
                    "microvia_diameter": 0.3,
                    "microvia_drill": 0.1,
                    "name": "Default",
                    "pcb_color": "rgba(0, 0, 0, 0.000)",
                    "priority": 2147483647,
                    "schematic_color": "rgba(0, 0, 0, 0.000)",
                    "track_width": 0.2,
                    "tuning_profile": "",
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                    "wire_width": 6,
                }
            ],
            "meta": {"version": 5},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": [],
        },
        "pcbnew": {
            "last_paths": {
                "idf": "",
                "netlist": "",
                "plot": "",
                "specctra_dsn": "",
                "vrml": "",
            },
            "page_layout_descr_file": "",
        },
        "schematic": {
            "bus_aliases": {},
            "legacy_lib_dir": "",
            "legacy_lib_list": [],
            "top_level_sheets": [],
        },
        "sheets": [],
        "text_variables": {},
    }


class ProjectCommands:
    """Handles project-related KiCAD operations"""

    def __init__(self, board: Optional[pcbnew.BOARD] = None):
        """Initialize with optional board instance"""
        self.board = board

    def create_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new KiCAD project"""
        try:
            # Accept both 'name' (from MCP tool) and 'projectName' (legacy)
            project_name = params.get("name") or params.get("projectName", "New_Project")
            path = params.get("path", os.getcwd())
            template = params.get("template")

            # Generate the full project path
            project_path = os.path.join(path, project_name)
            if not project_path.endswith(".kicad_pro"):
                project_path += ".kicad_pro"

            # Sibling files this command writes; needed up front for the
            # overwrite guard below.
            board_path = project_path.replace(".kicad_pro", ".kicad_pcb")
            schematic_path = project_path.replace(".kicad_pro", ".kicad_sch")

            # Refuse to clobber an existing project. The SaveBoard / schematic /
            # project writes below would otherwise silently overwrite the user's
            # board and schematic. Mirrors create_footprint / create_symbol, which
            # default overwrite=False and refuse when the target exists.
            overwrite = bool(params.get("overwrite", False))
            if not overwrite:
                existing = [
                    p for p in (project_path, board_path, schematic_path) if os.path.exists(p)
                ]
                if existing:
                    return {
                        "success": False,
                        "message": (
                            f'Project "{project_name}" already exists at {path}. '
                            "Pass overwrite=true to replace it, or choose a different name."
                        ),
                        "errorCode": "PROJECT_EXISTS",
                        "hint": (
                            "Refusing to overwrite an existing project. Use open_project "
                            "to edit it, pick a new name/path, or set overwrite=true."
                        ),
                        "existingFiles": existing,
                    }

            # Create project directory if it doesn't exist
            os.makedirs(os.path.dirname(project_path), exist_ok=True)

            # Create a new board
            board = pcbnew.BOARD()

            # Set project properties
            board.GetTitleBlock().SetTitle(project_name)

            # Set current date with proper parameter
            from datetime import datetime

            current_date = datetime.now().strftime("%Y-%m-%d")
            board.GetTitleBlock().SetDate(current_date)

            # If template is specified, try to load it
            if template:
                template_path = os.path.expanduser(template)
                if os.path.exists(template_path):
                    template_board = pcbnew.LoadBoard(template_path)
                    # Copy settings from template
                    board.SetDesignSettings(template_board.GetDesignSettings())
                    board.SetLayerStack(template_board.GetLayerStack())

            # Save the board (board_path computed above for the overwrite guard).
            # aSkipSettings=True: SaveBoard must not emit its own .kicad_pro from
            # the board's default in-memory PROJECT — we write a faithful minimal
            # project file explicitly below (E2E B7 / B10).
            board.SetFileName(board_path)
            pcbnew.SaveBoard(board_path, board, True)

            # Create a minimal empty schematic.  The old code seeded every new
            # project from template_with_symbols_expanded.kicad_sch, which
            # preloaded 13 unused lib_symbols (LM358, Crystal, …) with zero
            # placed instances (E2E B6).  add_schematic_component drives the
            # dynamic symbol loader, which injects each symbol's definition into
            # lib_symbols on demand, so the preload was dead weight that ERC then
            # auto-refreshed forever.  A clean empty lib_symbols block is all the
            # loader needs.
            import uuid as uuid_module

            schematic_uuid = str(uuid_module.uuid4())
            with open(schematic_path, "w", encoding="utf-8", newline="\n") as f:
                f.write('(kicad_sch (version 20250114) (generator "KiCAD-MCP-Server")\n\n')
                f.write(f"  (uuid {schematic_uuid})\n\n")
                f.write('  (paper "A4")\n\n')
                f.write("  (lib_symbols\n  )\n\n")
                f.write('  (sheet_instances\n    (path "/" (page "1"))\n  )\n')
                f.write(")\n")
            logger.info(f"Created minimal schematic: {schematic_path}")

            # Write a faithful minimal KiCad 10 project file (E2E B7).  The old
            # code wrote a ~112-byte stub that only self-healed once a
            # kicad-cli-backed op ran; this ships a real project document with a
            # Default net class so create_netclass / set_design_rules have a
            # canonical store to edit from the start.
            with open(project_path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(
                    _new_project_document(os.path.basename(project_path)),
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
                f.write("\n")

            self.board = board

            return {
                "success": True,
                "message": f"Created project: {project_name}",
                "project": {
                    "name": project_name,
                    "path": project_path,
                    "boardPath": board_path,
                    "schematicPath": schematic_path,
                },
            }

        except Exception as e:
            logger.error(f"Error creating project: {str(e)}")
            return {
                "success": False,
                "message": "Failed to create project",
                "errorDetails": str(e),
            }

    def open_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Open an existing KiCAD project"""
        try:
            filename = params.get("filename")
            if not filename:
                return {
                    "success": False,
                    "message": "No filename provided",
                    "errorDetails": "The filename parameter is required",
                }

            # Expand user path and make absolute
            filename = os.path.abspath(os.path.expanduser(filename))

            # If it's a project file, get the board file
            if filename.endswith(".kicad_pro"):
                board_path = filename.replace(".kicad_pro", ".kicad_pcb")
            else:
                board_path = filename

            # Load the board
            board = pcbnew.LoadBoard(board_path)
            self.board = board

            return {
                "success": True,
                "message": f"Opened project: {os.path.basename(board_path)}",
                "project": {
                    "name": os.path.splitext(os.path.basename(board_path))[0],
                    "path": filename,
                    "boardPath": board_path,
                },
            }

        except Exception as e:
            logger.error(f"Error opening project: {str(e)}")
            return {
                "success": False,
                "message": "Failed to open project",
                "errorDetails": str(e),
            }

    def save_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Save the current KiCAD project"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            filename = params.get("filename")
            if filename:
                # Save to new location
                filename = os.path.abspath(os.path.expanduser(filename))
                self.board.SetFileName(filename)

            # Save the board.  aSkipSettings=True: SaveBoard must not rewrite the
            # sibling .kicad_pro from the board's in-memory PROJECT, or it clobbers
            # netclass / design-rule edits that live only in that JSON (E2E B10).
            pcbnew.SaveBoard(self.board.GetFileName(), self.board, True)

            return {
                "success": True,
                "message": f"Saved project to: {self.board.GetFileName()}",
                "project": {
                    "name": os.path.splitext(os.path.basename(self.board.GetFileName()))[0],
                    "path": self.board.GetFileName(),
                },
            }

        except Exception as e:
            logger.error(f"Error saving project: {str(e)}")
            return {
                "success": False,
                "message": "Failed to save project",
                "errorDetails": str(e),
            }

    def get_project_info(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get information about the current project"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            title_block = self.board.GetTitleBlock()
            filename = self.board.GetFileName()

            return {
                "success": True,
                "project": {
                    "name": os.path.splitext(os.path.basename(filename))[0],
                    "path": filename,
                    "title": title_block.GetTitle(),
                    "date": title_block.GetDate(),
                    "revision": title_block.GetRevision(),
                    "company": title_block.GetCompany(),
                    "comment1": title_block.GetComment(0),
                    "comment2": title_block.GetComment(1),
                    "comment3": title_block.GetComment(2),
                    "comment4": title_block.GetComment(3),
                },
            }

        except Exception as e:
            logger.error(f"Error getting project info: {str(e)}")
            return {
                "success": False,
                "message": "Failed to get project information",
                "errorDetails": str(e),
            }
