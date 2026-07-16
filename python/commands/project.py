"""
Project-related command implementations for KiCAD interface
"""

import json
import logging
import os
from typing import Any, Dict, Optional

import pcbnew  # type: ignore
from utils.responses import failed, no_board_loaded

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


# Stable BOARD methods used to detect a SWIG-dehydrated proxy.  pcbnew.LoadBoard
# can hand back a raw SwigPyObject (it type-checks, but every method access
# raises AttributeError) on some KiCad builds / for garbage input.  Mirrors
# KiCADInterface._BOARD_HEALTH_METHODS; kept here so open_project can validate a
# load transactionally BEFORE committing it as self.board (E2E round 7 C1/C2).
_BOARD_HEALTH_METHODS = ("GetDesignSettings", "GetBoardEdgesBoundingBox", "GetFileName")


def _board_has_live_dispatch(board: Any) -> bool:
    """True when ``board`` exposes live SWIG method dispatch (not a dehydrated proxy)."""
    return board is not None and all(hasattr(board, m) for m in _BOARD_HEALTH_METHODS)


class ProjectCommands:
    """Handles project-related KiCAD operations"""

    def __init__(self, board: Optional[pcbnew.BOARD] = None):
        """Initialize with optional board instance"""
        self.board = board

    def create_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new KiCAD project.

        ``path`` is the *directory* in which to create ``<name>.kicad_pro``.
        As a convenience we also accept the full ``.kicad_pro`` FILE path in
        ``path`` (a common caller mistake) and split it back into
        (directory, name); otherwise ``os.path.join(path, name)`` produced a
        doubled sub-path and left a stray directory literally named
        ``<name>.kicad_pro`` that then blocked retries (E2E round 6 S1).
        """
        # Track what THIS call creates so a mid-way failure cleans up after
        # itself instead of leaving partial artifacts (E2E round 6 S1b).
        created_files: list[str] = []
        created_dir_root: Optional[str] = None
        leaf_dir: Optional[str] = None
        try:
            # Accept both 'name' (from MCP tool) and 'projectName' (legacy).
            # Resolve the effective name, treating an empty/whitespace value as
            # absent so it can't slip past as a truthy "   ".
            raw_name = params.get("name")
            if raw_name is None or not str(raw_name).strip():
                raw_name = params.get("projectName")
            project_name = raw_name if (raw_name and str(raw_name).strip()) else None
            path = params.get("path")
            template = params.get("template")

            # C9: distinguish an EXPLICITLY empty/whitespace name (user error →
            # INVALID_NAME) from an omitted one (may still be derived from a
            # .kicad_pro path below, else defaults to "New_Project").  The bare
            # `or`-collapse used before silently turned "" into the default, so
            # an accidental empty name produced a surprise "New_Project" project.
            name_key_present = ("name" in params) or ("projectName" in params)
            name_explicitly_empty = name_key_present and project_name is None

            # Normalise a caller that passed the .kicad_pro FILE as ``path``
            # instead of its containing directory (E2E round 6 S1a).
            if path and str(path).endswith(".kicad_pro"):
                derived_name = os.path.splitext(os.path.basename(path))[0]
                if project_name:
                    given = project_name
                    if given.endswith(".kicad_pro"):
                        given = given[: -len(".kicad_pro")]
                    if given != derived_name:
                        return {
                            "success": False,
                            "message": (
                                f'Conflicting project names: path implies "{derived_name}" '
                                f'(from {os.path.basename(path)}) but name="{project_name}". '
                                "Pass `path` as the directory and `name` as the project "
                                "name, or make the two agree."
                            ),
                            "errorCode": "PROJECT_NAME_CONFLICT",
                            "hint": (
                                "`path` is the directory to create <name>.kicad_pro in. "
                                "You passed a .kicad_pro file whose basename disagrees "
                                "with `name`."
                            ),
                        }
                project_name = derived_name
                path = os.path.dirname(path) or os.getcwd()

            # Defaults (preserved from the original behaviour).
            if not project_name:
                # C9: an explicitly-provided empty name is a user error, not a
                # silent fallback.  (A .kicad_pro `path` above may already have
                # supplied the name, in which case project_name is truthy here.)
                if name_explicitly_empty:
                    return {
                        "success": False,
                        "message": (
                            "Project name is empty. Pass a non-empty `name` — it "
                            "becomes the <name>.kicad_pro basename."
                        ),
                        "errorCode": "INVALID_NAME",
                        "hint": (
                            "`name` must be a non-empty identifier; an empty or "
                            "whitespace string is not accepted (it would otherwise "
                            "silently become 'New_Project')."
                        ),
                    }
                project_name = "New_Project"
            if not path:
                path = os.getcwd()
            # Strip a stray project extension a caller may have baked into name.
            if project_name.endswith(".kicad_pro"):
                project_name = project_name[: -len(".kicad_pro")]

            # Build the sibling paths from a single base.  Never str.replace:
            # it rewrote EVERY ".kicad_pro" in a doubled path, which is how the
            # schematic/board paths got mangled in the first place (E2E S1).
            project_path = os.path.join(path, project_name + ".kicad_pro")
            base = os.path.splitext(project_path)[0]
            board_path = base + ".kicad_pcb"
            schematic_path = base + ".kicad_sch"

            # Refuse to clobber an existing project. Only a real FILE counts —
            # a stray *directory* named "<name>.kicad_pro" (leftover from an
            # earlier malformed call) must NOT masquerade as an existing
            # project and permanently block the good call (E2E round 6 S1c).
            overwrite = bool(params.get("overwrite", False))
            if not overwrite:
                existing = [
                    p for p in (project_path, board_path, schematic_path) if os.path.isfile(p)
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

            # A prior malformed call could have left a stray *directory* whose
            # name is one of our target files.  An empty one is reused (removed
            # so the real file can take its place); a non-empty one is a
            # distinct, actionable error instead of an opaque IsADirectoryError
            # at write time (E2E round 6 S1c).
            for p in (project_path, board_path, schematic_path):
                if os.path.isdir(p):
                    if os.listdir(p):
                        return {
                            "success": False,
                            "message": (
                                f"A non-empty directory named {os.path.basename(p)} "
                                f"already exists at {os.path.dirname(p)}; refusing to "
                                "replace it. Move or remove it, or choose a different "
                                "name/path."
                            ),
                            "errorCode": "PATH_IS_DIRECTORY",
                            "hint": (
                                "The target project file name is occupied by a directory "
                                "(usually left by an earlier malformed create_project "
                                "call). Remove the stray directory and retry."
                            ),
                            "path": p,
                        }
                    os.rmdir(p)

            # Create the project directory if needed, remembering the topmost
            # level we create so failure-cleanup removes exactly this call's
            # chain and never a pre-existing directory.
            leaf_dir = os.path.dirname(project_path)
            if leaf_dir and not os.path.isdir(leaf_dir):
                topmost = leaf_dir
                while True:
                    up = os.path.dirname(topmost)
                    if not up or os.path.isdir(up):
                        break
                    topmost = up
                created_dir_root = topmost
                os.makedirs(leaf_dir, exist_ok=True)

            board = pcbnew.BOARD()

            board.GetTitleBlock().SetTitle(project_name)

            from datetime import datetime

            current_date = datetime.now().strftime("%Y-%m-%d")
            board.GetTitleBlock().SetDate(current_date)

            if template:
                template_path = os.path.expanduser(template)
                if os.path.exists(template_path):
                    template_board = pcbnew.LoadBoard(template_path)
                    board.SetDesignSettings(template_board.GetDesignSettings())
                    board.SetLayerStack(template_board.GetLayerStack())

            # Save the board (board_path computed above for the overwrite guard).
            # aSkipSettings=True: SaveBoard must not emit its own .kicad_pro from
            # the board's default in-memory PROJECT — we write a faithful minimal
            # project file explicitly below (E2E B7 / B10).  Register each output
            # path with the cleanup list BEFORE the write so a mid-write failure
            # (which can leave a truncated/empty file) still gets rolled back.
            created_files.append(board_path)
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
            created_files.append(schematic_path)
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
            created_files.append(project_path)
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
            # Clean up partial artifacts so one bad call can't block retries
            # (E2E round 6 S1b): remove the files we wrote, then any empty
            # directories we created (leaf-first, stopping at what pre-existed).
            for p in created_files:
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                except OSError:
                    pass
            if created_dir_root and leaf_dir:
                d = leaf_dir
                while d and os.path.isdir(d):
                    try:
                        os.rmdir(d)  # only succeeds while empty
                    except OSError:
                        break
                    if os.path.normpath(d) == os.path.normpath(created_dir_root):
                        break
                    d = os.path.dirname(d)
            return failed("Failed to create project", e)

    def open_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Open an existing KiCAD project.

        Accepts ``filename`` (legacy) or ``path`` (E2E round 6 S15) — the two
        are interchangeable.  Either may point at the ``.kicad_pro`` /
        ``.kicad_pcb`` file OR at the directory that contains the project, in
        which case we resolve to the single ``.kicad_pro`` inside (and error
        clearly when the directory holds zero or several).
        """
        try:
            filename = params.get("filename") or params.get("path")
            if not filename:
                return {
                    "success": False,
                    "message": "No project path provided",
                    "errorDetails": (
                        "Pass `filename` or `path` — a .kicad_pro/.kicad_pcb file, or a "
                        "directory containing exactly one .kicad_pro."
                    ),
                    "errorCode": "MISSING_PATH",
                }

            filename = os.path.abspath(os.path.expanduser(filename))

            # A directory → resolve to the single .kicad_pro it contains.
            if os.path.isdir(filename):
                import glob as _glob

                pros = sorted(_glob.glob(os.path.join(filename, "*.kicad_pro")))
                if len(pros) == 0:
                    return {
                        "success": False,
                        "message": f"No .kicad_pro found in directory: {filename}",
                        "errorCode": "NO_PROJECT_IN_DIR",
                        "hint": (
                            "Point at the .kicad_pro/.kicad_pcb file directly, or a "
                            "directory that contains exactly one project."
                        ),
                    }
                if len(pros) > 1:
                    return {
                        "success": False,
                        "message": (
                            f"Multiple .kicad_pro files in directory: {filename}; "
                            "specify which one to open."
                        ),
                        "errorCode": "AMBIGUOUS_PROJECT",
                        "hint": "Pass the specific .kicad_pro file path you want to open.",
                        "candidates": pros,
                    }
                filename = pros[0]

            # C1: validate the extension BEFORE touching the loader.
            # open_project handles KiCad project/board documents only — a .txt
            # or other file is user error (UNSUPPORTED_FILE), not an opaque
            # INTERNAL_ERROR from feeding garbage to pcbnew.LoadBoard.
            low = filename.lower()
            if not (low.endswith(".kicad_pro") or low.endswith(".kicad_pcb")):
                return {
                    "success": False,
                    "message": (f"Unsupported file for open_project: {os.path.basename(filename)}"),
                    "errorCode": "UNSUPPORTED_FILE",
                    "hint": (
                        "Pass a .kicad_pro or .kicad_pcb file (or a directory "
                        "containing exactly one .kicad_pro)."
                    ),
                }

            # Resolve the board (.kicad_pcb) and project (.kicad_pro) siblings.
            base = os.path.splitext(filename)[0]
            board_path = base + ".kicad_pcb"
            project_path = base + ".kicad_pro"

            # C1: a missing file must read as a clean FILE_NOT_FOUND, never a
            # false "loaded the board … restart the MCP server".  Point at
            # whichever file the caller actually named when that's what's
            # missing, else at the board file we need.
            if not os.path.isfile(board_path):
                if filename.endswith(".kicad_pro") and not os.path.isfile(filename):
                    missing, what = filename, "project file"
                else:
                    missing, what = board_path, "board file"
                return {
                    "success": False,
                    "message": f"{what} not found: {missing}",
                    "errorCode": "FILE_NOT_FOUND",
                    "hint": (
                        "Check the path is absolute and the project/board file " "exists on disk."
                    ),
                }

            # C1 + C2: load into a LOCAL, health-probe it, and only swap
            # self.board on success.  A corrupt/unreadable file must surface a
            # truthful PARSE_ERROR (no bogus restart advice) AND must leave the
            # previously-loaded board intact — open is not committed until it
            # succeeds (transactional open).
            try:
                board = pcbnew.LoadBoard(board_path)
            except Exception as load_err:
                logger.error(f"LoadBoard({board_path!r}) failed: {load_err}")
                return {
                    "success": False,
                    "message": f"Could not read board file: {board_path}",
                    "errorCode": "PARSE_ERROR",
                    "errorDetails": str(load_err),
                    "hint": (
                        "The .kicad_pcb file appears to be corrupt or not a valid "
                        "KiCad board. The previously-loaded project (if any) is kept."
                    ),
                }

            if not _board_has_live_dispatch(board):
                return {
                    "success": False,
                    "message": f"Could not read board file: {board_path}",
                    "errorCode": "PARSE_ERROR",
                    "hint": (
                        "The .kicad_pcb file appears to be corrupt or not a valid "
                        "KiCad board. The previously-loaded project (if any) is kept."
                    ),
                }

            # Commit only after validation.
            self.board = board

            return {
                "success": True,
                "message": f"Opened project: {os.path.basename(board_path)}",
                "project": {
                    # C11: project.path is ALWAYS the .kicad_pro; the board file
                    # is reported separately as boardPath.
                    "name": os.path.splitext(os.path.basename(board_path))[0],
                    "path": project_path,
                    "boardPath": board_path,
                },
            }

        except Exception as e:
            logger.error(f"Error opening project: {str(e)}")
            return failed("Failed to open project", e)

    def save_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Save the current KiCAD project"""
        try:
            if not self.board:
                return no_board_loaded()

            # Save-as target: accept `path` (TS schema) or `filename` (legacy).
            # Previously only `filename` was read, so the documented `path`
            # param was silently ignored (E2E round 6, folded observation).
            target = params.get("path") or params.get("filename")
            if target:
                # Save to a new location.  A .kicad_pro maps to its sibling
                # .kicad_pcb (SaveBoard writes the board file).
                target = os.path.abspath(os.path.expanduser(target))
                if target.endswith(".kicad_pro"):
                    target = os.path.splitext(target)[0] + ".kicad_pcb"
                self.board.SetFileName(target)

            # Save the board.  aSkipSettings=True: SaveBoard must not rewrite the
            # sibling .kicad_pro from the board's in-memory PROJECT, or it clobbers
            # netclass / design-rule edits that live only in that JSON (E2E B10).
            saved_path = self.board.GetFileName()
            pcbnew.SaveBoard(saved_path, self.board, True)

            # Name WHICH project was saved explicitly.  save_project targets the
            # currently-loaded board, which is whatever was last created/opened;
            # stating the path removes the ambiguity when several projects have
            # been touched in one session (E2E round 6, folded observation).
            # C11: standardize project.path on the .kicad_pro across the tool
            # family; report the actual board file written as boardPath (and,
            # for save-as clarity, the concrete savedPath — the .kicad_pcb).
            project_base = os.path.splitext(saved_path)[0]
            return {
                "success": True,
                "message": f"Saved project to: {saved_path}",
                "savedPath": saved_path,
                "project": {
                    "name": os.path.basename(project_base),
                    "path": project_base + ".kicad_pro",
                    "boardPath": saved_path,
                },
            }

        except Exception as e:
            logger.error(f"Error saving project: {str(e)}")
            return failed("Failed to save project", e)

    def get_project_info(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get information about the current project"""
        try:
            if not self.board:
                return no_board_loaded()

            title_block = self.board.GetTitleBlock()
            filename = self.board.GetFileName()

            # C11: report project.path as the .kicad_pro and the loaded board
            # file separately as boardPath — consistent with create/open/save.
            base = os.path.splitext(filename)[0]
            project_path = (base + ".kicad_pro") if filename else filename

            return {
                "success": True,
                "project": {
                    "name": os.path.basename(base) if filename else "",
                    "path": project_path,
                    "boardPath": filename,
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
            return failed("Failed to get project information", e)
