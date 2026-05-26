#!/usr/bin/env python3
"""
KiCAD Python Interface Script for Model Context Protocol

This script handles communication between the MCP TypeScript server
and KiCAD's Python API (pcbnew). It receives commands via stdin as
JSON and returns responses via stdout also as JSON.
"""

import hashlib
import json
import logging
import os
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Fix cairo DLL loading on Windows before any cairocffi import.
# cairocffi uses cffi's ffi.dlopen('cairo-2') which needs the DLL on PATH.
if sys.platform == "win32":
    for _bin_dir in [
        os.environ.get("PYTHONPATH", ""),
        os.path.dirname(sys.executable),
        r"C:\Program Files\KiCad\9.0\bin",
        r"C:\Program Files\KiCad\8.0\bin",
    ]:
        if _bin_dir and os.path.isfile(os.path.join(_bin_dir, "cairo-2.dll")):
            _current_path = os.environ.get("PATH", "")
            if _bin_dir not in _current_path:
                os.environ["PATH"] = _bin_dir + os.pathsep + _current_path
            break

import sexpdata
from annotations import AnnotationLoader
from commands.wire_manager import WireManager
from resources.resource_definitions import RESOURCE_DEFINITIONS, handle_resource_read

# Import tool schemas, resource definitions, and IPC API annotations
from schemas.tool_schemas import TOOL_SCHEMAS

_annotation_loader = AnnotationLoader()

# Configure logging.
#
# LOG_LEVEL env var (shared with the TypeScript side via src/server.ts /
# src/config.ts) drives both layers — defaults to INFO.  Previously this
# was hardcoded to DEBUG, flooding ~/.kicad-mcp/logs/kicad_interface.log
# every session and, on the no-write fallback path, dumping DEBUG noise
# onto stderr where the TS parent re-logs it all as ERROR.
#
# Try to set up a file handler in ~/.kicad-mcp/logs. If that directory
# isn't writable (e.g. sandboxed test environments, restricted CI
# runners), fall back to stderr-only logging so importing this module
# never crashes.
_log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
_log_level = getattr(logging, _log_level_name, logging.INFO)
_log_format = "%(asctime)s [%(levelname)s] %(message)s"
try:
    log_dir = os.path.join(os.path.expanduser("~"), ".kicad-mcp", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "kicad_interface.log")
    logging.basicConfig(
        level=_log_level,
        format=_log_format,
        handlers=[logging.FileHandler(log_file)],
        force=True,  # override any prior basicConfig (e.g. by upstream imports)
    )
except (OSError, PermissionError):
    logging.basicConfig(
        level=_log_level,
        format=_log_format,
        force=True,
    )
logger = logging.getLogger("kicad_interface")

# Log Python environment details
logger.info(f"Python version: {sys.version}")
logger.info(f"Python executable: {sys.executable}")
logger.info(f"Platform: {sys.platform}")
logger.info(f"Working directory: {os.getcwd()}")

# Windows-specific diagnostics
if sys.platform == "win32":
    logger.info("=== Windows Environment Diagnostics ===")
    logger.info(f"PYTHONPATH: {os.environ.get('PYTHONPATH', 'NOT SET')}")
    logger.info(f"PATH: {os.environ.get('PATH', 'NOT SET')[:200]}...")  # Truncate PATH

    # Check for common KiCAD installations
    common_kicad_paths = [r"C:\Program Files\KiCad", r"C:\Program Files (x86)\KiCad"]

    found_kicad = False
    for base_path in common_kicad_paths:
        if os.path.exists(base_path):
            logger.info(f"Found KiCAD installation at: {base_path}")
            # List versions
            try:
                versions = [
                    d for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d))
                ]
                logger.info(f"  Versions found: {', '.join(versions)}")
                for version in versions:
                    python_path = os.path.join(
                        base_path, version, "lib", "python3", "dist-packages"
                    )
                    if os.path.exists(python_path):
                        logger.info(f"  ✓ Python path exists: {python_path}")
                        found_kicad = True
                    else:
                        logger.warning(f"  ✗ Python path missing: {python_path}")
            except Exception as e:
                logger.warning(f"  Could not list versions: {e}")

    if not found_kicad:
        logger.warning("No KiCAD installations found in standard locations!")
        logger.warning(
            "Please ensure KiCAD 9.0+ is installed from https://www.kicad.org/download/windows/"
        )

    logger.info("========================================")

# Add utils directory to path for imports
utils_dir = os.path.join(os.path.dirname(__file__))
if utils_dir not in sys.path:
    sys.path.insert(0, utils_dir)

from utils.kicad_process import KiCADProcessManager, check_and_launch_kicad

# Import platform helper and add KiCAD paths
from utils.platform_helper import PlatformHelper

logger.info(f"Detecting KiCAD Python paths for {PlatformHelper.get_platform_name()}...")
paths_added = PlatformHelper.add_kicad_to_python_path()

if paths_added:
    logger.info("Successfully added KiCAD Python paths to sys.path")
else:
    logger.warning("No KiCAD Python paths found - attempting to import pcbnew from system path")

logger.info(f"Current Python path: {sys.path}")

# Check if auto-launch is enabled
AUTO_LAUNCH_KICAD = os.environ.get("KICAD_AUTO_LAUNCH", "false").lower() == "true"
if AUTO_LAUNCH_KICAD:
    logger.info("KiCAD auto-launch enabled")

# Check which backend to use
# KICAD_BACKEND can be: 'auto', 'ipc', or 'swig'
KICAD_BACKEND = os.environ.get("KICAD_BACKEND", "auto").lower()
logger.info(f"KiCAD backend preference: {KICAD_BACKEND}")

# Try to use IPC backend first if available and preferred
USE_IPC_BACKEND = False
ipc_backend = None

if KICAD_BACKEND in ("auto", "ipc"):
    try:
        logger.info("Checking IPC backend availability...")
        from kicad_api.ipc_backend import IPCBackend

        # Try to connect to running KiCAD
        ipc_backend = IPCBackend()
        if ipc_backend.connect():
            USE_IPC_BACKEND = True
            logger.info(f"✓ Using IPC backend - real-time UI sync enabled!")
            logger.info(f"  KiCAD version: {ipc_backend.get_version()}")
        else:
            logger.info("IPC backend available but KiCAD not running with IPC enabled")
            ipc_backend = None
    except ImportError:
        logger.info("IPC backend not available (kicad-python not installed)")
    except Exception as e:
        logger.info(f"IPC backend connection failed: {e}")
        ipc_backend = None

# Fall back to SWIG backend if IPC not available
if not USE_IPC_BACKEND and KICAD_BACKEND != "ipc":
    # Import KiCAD's Python API (SWIG)
    try:
        logger.info("Attempting to import pcbnew module (SWIG backend)...")
        import pcbnew  # type: ignore

        logger.info(f"Successfully imported pcbnew module from: {pcbnew.__file__}")
        # Deferred — GetBuildVersion() triggers 55-65 s wxApp init on macOS.
        # The _warmup handler pays this cost during startup (not on first tool call).
        logger.warning("Using SWIG backend - changes require manual reload in KiCAD UI")
    except ImportError as e:
        logger.error(f"Failed to import pcbnew module: {e}")
        logger.error(f"Current sys.path: {sys.path}")

        # Platform-specific help message
        help_message = ""
        if sys.platform == "win32":
            help_message = """
Windows Troubleshooting:
1. Verify KiCAD is installed: C:\\Program Files\\KiCad\\9.0
2. Check PYTHONPATH environment variable points to:
   C:\\Program Files\\KiCad\\9.0\\lib\\python3\\dist-packages
3. Test with: "C:\\Program Files\\KiCad\\9.0\\bin\\python.exe" -c "import pcbnew"
4. Log file location: %USERPROFILE%\\.kicad-mcp\\logs\\kicad_interface.log
5. Run setup-windows.ps1 for automatic configuration
"""
        elif sys.platform == "darwin":
            help_message = """
macOS Troubleshooting:
1. Verify KiCAD is installed: /Applications/KiCad/KiCad.app
2. Check PYTHONPATH points to KiCAD's Python packages
3. Run: python3 -c "import pcbnew" to test
"""
        else:  # Linux
            help_message = """
Linux Troubleshooting:
1. Verify KiCAD is installed: apt list --installed | grep kicad
2. Check: /usr/lib/kicad/lib/python3/dist-packages exists
3. Test: python3 -c "import pcbnew"
"""

        logger.error(help_message)

        error_response = {
            "success": False,
            "message": "Failed to import pcbnew module - KiCAD Python API not found",
            "errorDetails": f"Error: {str(e)}\n\n{help_message}\n\nPython sys.path:\n{chr(10).join(sys.path)}",
        }
        print(json.dumps(error_response))
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error importing pcbnew: {e}")
        logger.error(traceback.format_exc())
        error_response = {
            "success": False,
            "message": "Error importing pcbnew module",
            "errorDetails": str(e),
        }
        print(json.dumps(error_response))
        sys.exit(1)

# If IPC-only mode requested but not available, exit with error
elif KICAD_BACKEND == "ipc" and not USE_IPC_BACKEND:
    error_response = {
        "success": False,
        "message": "IPC backend requested but not available",
        "errorDetails": "KiCAD must be running with IPC API enabled. Enable at: Preferences > Plugins > Enable IPC API Server",
    }
    print(json.dumps(error_response))
    sys.exit(1)

# Import command handlers
try:
    logger.info("Importing command handlers...")
    from commands.board import BoardCommands
    from commands.component import ComponentCommands
    from commands.component_schematic import ComponentManager
    from commands.connection_schematic import ConnectionManager
    from commands.datasheet_manager import DatasheetManager
    from commands.design_rules import DesignRuleCommands
    from commands.export import ExportCommands
    from commands.footprint import FootprintCreator
    from commands.freerouting import FreeroutingCommands
    from commands.jlcpcb import JLCPCBClient, test_jlcpcb_connection
    from commands.jlcpcb_parts import JLCPCBPartsManager
    from commands.library import (
        LibraryCommands,
    )
    from commands.library import LibraryManager as FootprintLibraryManager
    from commands.library_schematic import LibraryManager as SchematicLibraryManager
    from commands.library_symbol import SymbolLibraryCommands, SymbolLibraryManager
    from commands.project import ProjectCommands
    from commands.routing import RoutingCommands
    from commands.schematic import SchematicManager
    from commands.symbol_creator import SymbolCreator

    logger.info("Successfully imported all command handlers")
except ImportError as e:
    logger.error(f"Failed to import command handlers: {e}")
    error_response = {
        "success": False,
        "message": "Failed to import command handlers",
        "errorDetails": str(e),
    }
    print(json.dumps(error_response))
    sys.exit(1)


class KiCADInterface:
    """Main interface class to handle KiCAD operations"""

    def __init__(self) -> None:
        """Initialize the interface and command handlers"""
        self.board = None
        self.project_filename = None
        # On-disk signature (mtime_ns, sha256_hex) of self.board's file as of
        # last load or successful auto-save.  Used by _auto_save_board() to
        # detect external modifications and refuse to clobber them.
        self._board_disk_signature: Optional[Tuple[int, str]] = None
        self._last_auto_save_status: Optional[Dict[str, Any]] = None
        # Number of timestamped backups to keep in .mcp-backups/ per board file.
        self._auto_save_backup_keep = 20
        self.use_ipc = USE_IPC_BACKEND
        self.ipc_backend = ipc_backend
        self.ipc_board_api = None

        if self.use_ipc:
            logger.info("Initializing with IPC backend (real-time UI sync enabled)")
            try:
                self.ipc_board_api = self.ipc_backend.get_board()
                logger.info("✓ Got IPC board API")
            except Exception as e:
                logger.warning(f"Could not get IPC board API: {e}")
        else:
            logger.info("Initializing with SWIG backend")

        logger.info("Initializing command handlers...")

        # Initialize footprint library manager
        self.footprint_library = FootprintLibraryManager()

        # Initialize command handlers
        self.project_commands = ProjectCommands(self.board)
        self.board_commands = BoardCommands(self.board)
        self.component_commands = ComponentCommands(self.board, self.footprint_library)
        self.routing_commands = RoutingCommands(self.board)
        self.freerouting_commands = FreeroutingCommands(self.board)
        self.design_rule_commands = DesignRuleCommands(self.board)
        self.export_commands = ExportCommands(self.board)
        self.library_commands = LibraryCommands(self.footprint_library)
        self._current_project_path: Optional[Path] = None  # set when boardPath is known

        # Initialize symbol library manager (for searching local KiCad symbol libraries)
        self.symbol_library_commands = SymbolLibraryCommands()

        # Initialize JLCPCB API integration
        self.jlcpcb_client = JLCPCBClient()  # Official API (requires auth)
        from commands.jlcsearch import JLCSearchClient

        self.jlcsearch_client = JLCSearchClient()  # Public API (no auth required)
        self.jlcpcb_parts = JLCPCBPartsManager()

        # Schematic-related classes don't need board reference
        # as they operate directly on schematic files

        # Command routing dictionary
        self.command_routes = {
            # Project commands
            "create_project": self._handle_create_project,
            "open_project": self._handle_open_project,
            "save_project": self.project_commands.save_project,
            "snapshot_project": self._handle_snapshot_project,
            "get_project_info": self.project_commands.get_project_info,
            # Board commands
            "set_board_size": self.board_commands.set_board_size,
            "add_layer": self.board_commands.add_layer,
            "set_active_layer": self.board_commands.set_active_layer,
            "get_board_info": self.board_commands.get_board_info,
            "get_layer_list": self.board_commands.get_layer_list,
            "get_board_2d_view": self.board_commands.get_board_2d_view,
            "get_board_extents": self.board_commands.get_board_extents,
            "add_board_outline": self.board_commands.add_board_outline,
            "add_mounting_hole": self.board_commands.add_mounting_hole,
            "add_text": self.board_commands.add_text,
            "add_board_text": self.board_commands.add_text,  # Alias for TypeScript tool
            # Component commands
            "route_pad_to_pad": self.routing_commands.route_pad_to_pad,
            "place_component": self._handle_place_component,
            "move_component": self.component_commands.move_component,
            "rotate_component": self.component_commands.rotate_component,
            "delete_component": self.component_commands.delete_component,
            "edit_component": self.component_commands.edit_component,
            "get_component_properties": self.component_commands.get_component_properties,
            "get_component_list": self.component_commands.get_component_list,
            "find_component": self.component_commands.find_component,
            "get_component_pads": self.component_commands.get_component_pads,
            "get_pad_position": self.component_commands.get_pad_position,
            "place_component_array": self.component_commands.place_component_array,
            "align_components": self.component_commands.align_components,
            "check_courtyard_overlaps": self.component_commands.check_courtyard_overlaps,
            "duplicate_component": self.component_commands.duplicate_component,
            # Routing commands
            "add_net": self.routing_commands.add_net,
            "route_trace": self.routing_commands.route_trace,
            "route_arc_trace": self.routing_commands.route_arc_trace,
            "add_via": self.routing_commands.add_via,
            "delete_trace": self.routing_commands.delete_trace,
            "query_traces": self.routing_commands.query_traces,
            "query_zones": self.routing_commands.query_zones,
            "add_gnd_stitching_vias": self.routing_commands.add_gnd_stitching_vias,
            "modify_trace": self.routing_commands.modify_trace,
            "copy_routing_pattern": self.routing_commands.copy_routing_pattern,
            "get_nets_list": self.routing_commands.get_nets_list,
            "create_netclass": self.routing_commands.create_netclass,
            "add_copper_pour": self.routing_commands.add_copper_pour,
            "route_differential_pair": self.routing_commands.route_differential_pair,
            "refill_zones": self._handle_refill_zones,
            # Design rule commands
            "set_design_rules": self.design_rule_commands.set_design_rules,
            "get_design_rules": self.design_rule_commands.get_design_rules,
            "run_drc": self.design_rule_commands.run_drc,
            "get_drc_violations": self.design_rule_commands.get_drc_violations,
            # Export commands
            "export_gerber": self.export_commands.export_gerber,
            "export_pdf": self.export_commands.export_pdf,
            "export_svg": self.export_commands.export_svg,
            "export_3d": self.export_commands.export_3d,
            "export_bom": self.export_commands.export_bom,
            # Library commands (footprint management)
            "list_libraries": self.library_commands.list_libraries,
            "search_footprints": self.library_commands.search_footprints,
            "list_library_footprints": self.library_commands.list_library_footprints,
            "get_footprint_info": self.library_commands.get_footprint_info,
            # Symbol library commands (local KiCad symbol library search)
            "list_symbol_libraries": self.symbol_library_commands.list_symbol_libraries,
            "search_symbols": self.symbol_library_commands.search_symbols,
            "list_library_symbols": self.symbol_library_commands.list_library_symbols,
            "get_symbol_info": self.symbol_library_commands.get_symbol_info,
            # JLCPCB API commands (complete parts catalog via API)
            "download_jlcpcb_database": self._handle_download_jlcpcb_database,
            "search_jlcpcb_parts": self._handle_search_jlcpcb_parts,
            "get_jlcpcb_part": self._handle_get_jlcpcb_part,
            "get_jlcpcb_database_stats": self._handle_get_jlcpcb_database_stats,
            "suggest_jlcpcb_alternatives": self._handle_suggest_jlcpcb_alternatives,
            # Datasheet commands
            "enrich_datasheets": self._handle_enrich_datasheets,
            "get_datasheet_url": self._handle_get_datasheet_url,
            # Schematic commands
            "create_schematic": self._handle_create_schematic,
            "load_schematic": self._handle_load_schematic,
            "add_schematic_component": self._handle_add_schematic_component,
            "delete_schematic_component": self._handle_delete_schematic_component,
            "edit_schematic_component": self._handle_edit_schematic_component,
            "set_schematic_component_property": self._handle_set_schematic_component_property,
            "remove_schematic_component_property": self._handle_remove_schematic_component_property,
            "get_schematic_component": self._handle_get_schematic_component,
            "add_schematic_wire": self._handle_add_schematic_wire,
            "add_schematic_net_label": self._handle_add_schematic_net_label,
            "add_no_connect": self._handle_add_no_connect,
            "connect_to_net": self._handle_connect_to_net,
            "connect_passthrough": self._handle_connect_passthrough,
            "get_schematic_pin_locations": self._handle_get_schematic_pin_locations,
            "get_net_connections": self._handle_get_net_connections,
            "get_wire_connections": self._handle_get_wire_connections,
            "get_net_at_point": self._handle_get_net_at_point,
            "run_erc": self._handle_run_erc,
            "export_netlist": self._handle_export_netlist,
            "generate_netlist": self._handle_generate_netlist,
            "sync_schematic_to_board": self._handle_sync_schematic_to_board,
            "list_schematic_libraries": self._handle_list_schematic_libraries,
            "get_schematic_view": self._handle_get_schematic_view,
            "list_schematic_components": self._handle_list_schematic_components,
            "list_schematic_nets": self._handle_list_schematic_nets,
            "list_schematic_wires": self._handle_list_schematic_wires,
            "list_schematic_labels": self._handle_list_schematic_labels,
            "move_schematic_component": self._handle_move_schematic_component,
            "rotate_schematic_component": self._handle_rotate_schematic_component,
            "annotate_schematic": self._handle_annotate_schematic,
            "delete_schematic_wire": self._handle_delete_schematic_wire,
            "delete_schematic_net_label": self._handle_delete_schematic_net_label,
            "move_schematic_net_label": self._handle_move_schematic_net_label,
            "export_schematic_pdf": self._handle_export_schematic_pdf,
            "export_schematic_svg": self._handle_export_schematic_svg,
            # Schematic analysis tools (read-only)
            "get_schematic_view_region": self._handle_get_schematic_view_region,
            "find_overlapping_elements": self._handle_find_overlapping_elements,
            "get_elements_in_region": self._handle_get_elements_in_region,
            "find_wires_crossing_symbols": self._handle_find_wires_crossing_symbols,
            "find_orphaned_wires": self._handle_find_orphaned_wires,
            "list_floating_labels": self._handle_list_floating_labels,
            "snap_to_grid": self._handle_snap_to_grid,
            "add_schematic_hierarchical_label": self._handle_add_schematic_hierarchical_label,
            "add_schematic_text": self._handle_add_schematic_text,
            "list_schematic_texts": self._handle_list_schematic_texts,
            "add_sheet_pin": self._handle_add_sheet_pin,
            "import_svg_logo": self._handle_import_svg_logo,
            # UI/Process management commands
            "get_backend_state": self._handle_get_backend_state,
            "check_kicad_ui": self._handle_check_kicad_ui,
            "launch_kicad_ui": self._handle_launch_kicad_ui,
            # Internal warm-up (pays wxApp init cost during startup)
            "_warmup": self._handle_warmup,
            # IPC-specific commands (real-time operations)
            "get_backend_info": self._handle_get_backend_info,
            "ipc_add_track": self._handle_ipc_add_track,
            "ipc_add_via": self._handle_ipc_add_via,
            "ipc_add_text": self._handle_ipc_add_text,
            "ipc_list_components": self._handle_ipc_list_components,
            "ipc_get_tracks": self._handle_ipc_get_tracks,
            "ipc_get_vias": self._handle_ipc_get_vias,
            "ipc_save_board": self._handle_ipc_save_board,
            # Footprint commands
            "create_footprint": self._handle_create_footprint,
            "edit_footprint_pad": self._handle_edit_footprint_pad,
            "list_footprint_libraries": self._handle_list_footprint_libraries,
            "register_footprint_library": self._handle_register_footprint_library,
            # Symbol creator commands
            "create_symbol": self._handle_create_symbol,
            "delete_symbol": self._handle_delete_symbol,
            "list_symbols_in_library": self._handle_list_symbols_in_library,
            "register_symbol_library": self._handle_register_symbol_library,
            # Freerouting autoroute commands
            "autoroute": self.freerouting_commands.autoroute,
            "export_dsn": self.freerouting_commands.export_dsn,
            "import_ses": self.freerouting_commands.import_ses,
            "check_freerouting": self.freerouting_commands.check_freerouting,
        }

        logger.info(f"KiCAD interface initialized (backend: {'IPC' if self.use_ipc else 'SWIG'})")

    # Commands that can be handled via IPC for real-time updates
    IPC_CAPABLE_COMMANDS = {
        # Routing commands
        "route_trace": "_ipc_route_trace",
        "route_arc_trace": "_ipc_route_arc_trace",
        "add_via": "_ipc_add_via",
        "add_net": "_ipc_add_net",
        "delete_trace": "_ipc_delete_trace",
        "query_traces": "_ipc_query_traces",
        "get_nets_list": "_ipc_get_nets_list",
        # Zone commands
        "add_copper_pour": "_ipc_add_copper_pour",
        "refill_zones": "_ipc_refill_zones",
        # Board commands
        "add_text": "_ipc_add_text",
        "add_board_text": "_ipc_add_text",
        "set_board_size": "_ipc_set_board_size",
        "get_board_info": "_ipc_get_board_info",
        "add_board_outline": "_ipc_add_board_outline",
        "add_mounting_hole": "_ipc_add_mounting_hole",
        "get_layer_list": "_ipc_get_layer_list",
        # Component commands
        "place_component": "_ipc_place_component",
        "move_component": "_ipc_move_component",
        "rotate_component": "_ipc_rotate_component",
        "delete_component": "_ipc_delete_component",
        "get_component_list": "_ipc_get_component_list",
        "get_component_properties": "_ipc_get_component_properties",
        # Save command
        "save_project": "_ipc_save_project",
    }

    # Commands that are implemented by the explicit IPC command handlers in
    # command_routes, rather than by the generic IPC_CAPABLE_COMMANDS fast path.
    IPC_DIRECT_COMMANDS = {
        "ipc_add_track",
        "ipc_add_via",
        "ipc_add_text",
        "ipc_list_components",
        "ipc_get_tracks",
        "ipc_get_vias",
        "ipc_save_board",
    }

    def _refresh_ipc_board_api(self) -> bool:
        """Refresh the IPC board API after KiCAD or a board becomes available."""
        ipc_backend = getattr(self, "ipc_backend", None)
        if not ipc_backend or not ipc_backend.is_connected():
            self.ipc_board_api = None
            return False

        try:
            self.ipc_board_api = ipc_backend.get_board()
            return True
        except Exception as e:
            logger.warning(f"Connected to KiCAD IPC, but no board API is available yet: {e}")
            self.ipc_board_api = None
            return False

    def _try_enable_ipc_backend(self, force: bool = False) -> bool:
        """Try to switch an already-running interface to IPC when KiCAD is available."""
        if KICAD_BACKEND == "swig":
            return False

        ipc_backend = getattr(self, "ipc_backend", None)
        if self.use_ipc and ipc_backend and ipc_backend.is_connected():
            self._refresh_ipc_board_api()
            return True

        if not force and not KiCADProcessManager.is_running():
            return False

        try:
            from kicad_api.ipc_backend import IPCBackend

            backend = ipc_backend or IPCBackend()
            if not backend.is_connected():
                backend.connect()

            self.ipc_backend = backend
            self.use_ipc = True
            self._refresh_ipc_board_api()
            logger.info("Switched to IPC backend after KiCAD became available")
            return True
        except Exception as e:
            logger.info(f"Runtime IPC connection not available: {e}")
            return False

    def _backend_status(self) -> Dict[str, Any]:
        """Return backend status fields for command responses."""
        ipc_backend = getattr(self, "ipc_backend", None)
        ipc_connected = ipc_backend.is_connected() if ipc_backend else False
        return {
            "backend": "ipc" if self.use_ipc and ipc_connected else "swig",
            "realtime_sync": self.use_ipc and ipc_connected,
            "ipc_connected": ipc_connected,
        }

    @staticmethod
    def _normalize_ipc_layer_name(layer: Any) -> str:
        """Convert KiCad IPC layer enum strings to common layer names."""
        layer_name = str(layer)
        if layer_name.startswith("BL_"):
            return layer_name[3:].replace("_", ".")
        return layer_name

    def _result_backend_for_command(self, command: str, result: Dict[str, Any]) -> str:
        """Return the backend label for a command result."""
        if command in {
            "get_backend_info",
            "get_backend_state",
            "check_kicad_ui",
            "launch_kicad_ui",
        }:
            return result.get("backend", "ipc" if self.use_ipc else "swig")

        if command in self.IPC_DIRECT_COMMANDS:
            return "ipc" if self.use_ipc else "unavailable"

        return "swig"

    def handle_command(self, command: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Route command to appropriate handler, preferring IPC when available"""
        logger.info(f"Handling command: {command}")
        logger.debug(f"Command parameters: {params}")

        try:
            if command in self.IPC_CAPABLE_COMMANDS:
                self._try_enable_ipc_backend()

            # Check if we can use IPC for this command (real-time UI sync)
            if self.use_ipc and self.ipc_board_api and command in self.IPC_CAPABLE_COMMANDS:
                ipc_handler_name = self.IPC_CAPABLE_COMMANDS[command]
                ipc_handler = getattr(self, ipc_handler_name, None)

                if ipc_handler:
                    logger.info(f"Using IPC backend for {command} (real-time sync)")
                    result = ipc_handler(params)

                    # Add indicator that IPC was used
                    if isinstance(result, dict):
                        result["_backend"] = "ipc"
                        result["_realtime"] = True

                    logger.debug(f"IPC command result: {result}")
                    return result

            # Fall back to SWIG-based handler
            if self.use_ipc and command in self.IPC_CAPABLE_COMMANDS:
                logger.warning(
                    f"IPC handler not available for {command}, falling back to SWIG (deprecated)"
                )

            # Get the handler for the command
            handler = self.command_routes.get(command)

            if handler:
                # Execute the command
                result = handler(params)
                logger.debug(f"Command result: {result}")

                # Add backend indicator
                if isinstance(result, dict):
                    backend = self._result_backend_for_command(command, result)
                    result["_backend"] = backend
                    result["_realtime"] = bool(
                        backend == "ipc" and result.get("realtime", self.use_ipc)
                    )

                # Update board reference if command was successful
                if result.get("success", False):
                    if command == "create_project" or command == "open_project":
                        logger.info("Updating board reference...")
                        # Get board from the project commands handler
                        self.board = self.project_commands.board

                        # Detect SWIG dehydration before claiming success.
                        # Without this, every later board op sees a raw
                        # SwigPyObject and raises AttributeError, while the
                        # MCP keeps reporting "Opened project" — the exact
                        # symptom users hit on KiCAD nightlies.
                        if not self._is_board_healthy():
                            board_path = (result.get("project") or {}).get("boardPath")
                            recovered = None
                            if board_path:
                                logger.warning(
                                    "Board after %s is SWIG-dehydrated; attempting recovery",
                                    command,
                                )
                                recovered = self._safe_load_board(board_path)
                            if recovered is not None:
                                self.board = recovered
                                self.project_commands.board = recovered
                                result.setdefault("warnings", []).append(
                                    "SWIG board proxy was dehydrated on load; "
                                    "recovered via pcbnew module reload"
                                )
                            else:
                                # Surface the truth — never claim success when
                                # the board is unusable.
                                return {
                                    "success": False,
                                    "message": (
                                        f"{command} loaded the board but the SWIG "
                                        "proxy is dehydrated and recovery failed"
                                    ),
                                    "errorDetails": (
                                        "pcbnew.LoadBoard returned a BOARD whose "
                                        "method dispatch is missing (raw SwigPyObject). "
                                        "This indicates SWIG state corruption in the "
                                        "current Python process — restart the MCP "
                                        "server to recover."
                                    ),
                                    "_backend": "swig",
                                    "_realtime": False,
                                }
                        self._update_command_handlers()
                        # Record the file's signature so subsequent auto-saves
                        # can detect external modifications and refuse to
                        # overwrite them.
                        self._record_board_signature()
                        self._last_auto_save_status = None
                    elif command == "save_project":
                        self._record_board_signature()
                        self._last_auto_save_status = None
                    elif command in self._BOARD_MUTATING_COMMANDS:
                        # Auto-save after every board mutation via SWIG.
                        # Prevents data loss if Claude hits context limit before
                        # an explicit save_project call.  When auto-save refuses
                        # because the on-disk file changed externally, surface
                        # a warning to the caller so they don't believe their
                        # mutation was persisted.
                        save_status = self._auto_save_board()
                        self._last_auto_save_status = save_status
                        if isinstance(result, dict) and not save_status.get("saved"):
                            if save_status.get("warning"):
                                result.setdefault("warnings", []).append(save_status["warning"])
                            result["autoSave"] = save_status

                return result
            else:
                logger.error(f"Unknown command: {command}")
                return {
                    "success": False,
                    "message": f"Unknown command: {command}",
                    "errorDetails": "The specified command is not supported",
                }

        except Exception as e:
            # Get the full traceback
            traceback_str = traceback.format_exc()
            logger.error(f"Error handling command {command}: {str(e)}\n{traceback_str}")
            return {
                "success": False,
                "message": f"Error handling command: {command}",
                "errorDetails": f"{str(e)}\n{traceback_str}",
            }

    # Board-mutating commands that trigger auto-save on SWIG path
    _BOARD_MUTATING_COMMANDS = {
        "place_component",
        "move_component",
        "rotate_component",
        "delete_component",
        "route_trace",
        "route_arc_trace",
        "route_pad_to_pad",
        "add_via",
        "delete_trace",
        "add_net",
        "add_board_outline",
        "add_mounting_hole",
        "add_text",
        "add_board_text",
        "add_copper_pour",
        "refill_zones",
        "import_svg_logo",
        "sync_schematic_to_board",
        "connect_passthrough",
        "connect_to_net",
    }

    @staticmethod
    def _disk_signature(path: str) -> Optional[Tuple[int, str]]:
        """Return (mtime_ns, sha256_hex) for the file, or None if missing/unreadable.

        The sha256 is always recomputed from disk: the conflict guard in
        ``_auto_save_board`` compares hashes (content), not mtime, so we
        cannot use mtime as a cache key without re-introducing the bug
        where two writes inside one mtime tick on a coarse-resolution
        filesystem (FAT32, network mounts, etc.) would mask a real
        content change.
        """
        try:
            st = os.stat(path)
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return (st.st_mtime_ns, h.hexdigest())
        except OSError:
            return None

    def _record_board_signature(self) -> None:
        """Record the current on-disk signature of self.board's file.

        Call this after a fresh load (open_project / create_project) or after
        any save we perform ourselves, so that _auto_save_board() can detect
        when an external actor has modified the file in between.
        """
        if not self.board:
            self._board_disk_signature = None
            return
        try:
            path = self.board.GetFileName()
        except Exception:
            path = None
        self._board_disk_signature = self._disk_signature(path) if path else None

    def _current_board_path(self) -> Optional[str]:
        """Return the current board file path, if a healthy board is loaded."""
        board = getattr(self, "board", None)
        if not board or not self._is_board_healthy(board):
            return None
        try:
            path = board.GetFileName()
        except Exception:
            return None
        return os.path.abspath(path) if path else None

    def _current_project_file_path(self, board_path: Optional[str]) -> Optional[str]:
        """Best-effort project file path for the currently loaded board."""
        candidates = []
        project_path = getattr(self, "_current_project_path", None)

        if project_path:
            project_path = Path(project_path)
            if project_path.suffix == ".kicad_pro":
                candidates.append(project_path)
            elif board_path:
                candidates.append(project_path / (Path(board_path).stem + ".kicad_pro"))
            elif project_path.is_dir():
                candidates.extend(project_path.glob("*.kicad_pro"))

        if board_path and board_path.endswith(".kicad_pcb"):
            candidates.append(Path(board_path).with_suffix(".kicad_pro"))

        for candidate in candidates:
            if candidate.exists():
                return str(candidate.resolve())

        return str(Path(candidates[0]).resolve()) if candidates else None

    def _dirty_state(self, board_path: Optional[str]) -> Dict[str, Any]:
        """Return the best-known dirty state for the loaded board.

        dirty is intentionally tri-state: True/False when the MCP has evidence,
        None when no reliable disk signature exists.
        """
        if not board_path:
            return {
                "dirty": False,
                "dirtyReason": "No board is loaded",
                "diskChangedExternally": False,
            }

        last_auto_save = getattr(self, "_last_auto_save_status", None) or {}
        if last_auto_save.get("memChangesUnsaved"):
            return {
                "dirty": True,
                "dirtyReason": "Auto-save refused after a board mutation; memory changes are not saved",
                "diskChangedExternally": bool(last_auto_save.get("diskChangedExternally")),
            }

        expected = getattr(self, "_board_disk_signature", None)
        current = self._disk_signature(board_path)

        if expected is None:
            return {
                "dirty": None,
                "dirtyReason": "No recorded disk signature for the loaded board",
                "diskChangedExternally": False,
            }

        if current is None:
            return {
                "dirty": None,
                "dirtyReason": "Board file is missing or unreadable on disk",
                "diskChangedExternally": False,
            }

        if expected[1] != current[1]:
            return {
                "dirty": True,
                "dirtyReason": "Board file contents changed on disk since this MCP session loaded it",
                "diskChangedExternally": True,
            }

        return {
            "dirty": False,
            "dirtyReason": "Board file matches the MCP recorded disk signature",
            "diskChangedExternally": False,
        }

    def _prune_auto_save_backups(self, backup_dir: str, base_name: str) -> None:
        """Keep only the most recent `_auto_save_backup_keep` backups for `base_name`."""
        try:
            entries = [
                os.path.join(backup_dir, f)
                for f in os.listdir(backup_dir)
                if f.startswith(base_name + ".")
            ]
            entries.sort(key=os.path.getmtime, reverse=True)
            for old in entries[self._auto_save_backup_keep :]:
                try:
                    os.remove(old)
                except OSError:
                    pass
        except OSError as e:
            logger.debug(f"Backup pruning skipped: {e}")

    def _auto_save_board(self) -> Dict[str, Any]:
        """Save the in-memory board to disk after a SWIG-path mutation.

        Behaviour:
          * If the file's on-disk signature has diverged from the one we
            recorded at load (or at our last successful save), refuse to
            overwrite — an external actor (KiCad GUI, another process, git)
            has touched the file and saving would clobber their changes.
          * Otherwise, copy the existing file to ``<dir>/.mcp-backups/<name>.<ts>``
            (rotating, keeps the most recent `_auto_save_backup_keep`),
            then call pcbnew.SaveBoard().
          * Update the recorded signature on success.
          * If SaveBoard leaves the in-memory BOARD dehydrated (observed on
            KiCAD nightlies after delete_trace + auto-save), reload from disk
            so the next command sees a usable proxy instead of a SwigPyObject.

        Returns a status dict that handle_command merges into the caller's
        response so warnings about refused saves are visible:
          {"saved": True,  "boardPath": ..., "backup": <path-or-None>}
          {"saved": False, "skipped": <reason>}                      -- nothing to save
          {"saved": False, "warning": ..., "diskChangedExternally": True, ...}
          {"saved": False, "error": ...}                             -- pcbnew error
        """
        if not self.board:
            return {"saved": False, "skipped": "no board loaded"}

        try:
            board_path = self.board.GetFileName()
        except Exception as e:
            return {"saved": False, "skipped": f"GetFileName failed: {e}"}

        if not board_path:
            return {"saved": False, "skipped": "no board path"}

        expected = self._board_disk_signature
        current = self._disk_signature(board_path)

        # Only refuse if the file's CONTENT (sha256) has actually diverged
        # from what we recorded. mtime alone is not a conflict signal —
        # `touch`, atime-driven backups, or even some MCP read paths can
        # advance mtime without changing content, and refusing on that
        # basis traps users in a state where every write needs an explicit
        # save_project workaround.
        #
        # If expected is None, treat this as "first save" and proceed —
        # otherwise pre-existing setups (open_project ran before this guard
        # was introduced) would never be able to save.
        if expected is not None and current is not None and expected[1] != current[1]:
            warning = (
                "Auto-save refused: the on-disk PCB file's contents changed "
                "externally since this MCP session loaded it. To avoid "
                "clobbering those changes, the in-memory mutation has NOT "
                "been written to disk. Reload via open_project to refresh, "
                "then re-apply the change."
            )
            logger.warning(f"{warning} ({board_path})")
            logger.warning(f"  expected sha256={expected[1][:12]}… mtime_ns={expected[0]}")
            logger.warning(f"  current  sha256={current[1][:12]}… mtime_ns={current[0]}")
            return {
                "saved": False,
                "warning": warning,
                "boardPath": board_path,
                "diskChangedExternally": True,
                "expectedMtimeNs": expected[0],
                "currentMtimeNs": current[0],
                "memChangesUnsaved": True,
            }

        # Content matches but mtime advanced (e.g. external `touch`): refresh
        # the recorded mtime so we don't re-hash on every subsequent call.
        if expected is not None and current is not None and expected != current:
            self._board_disk_signature = current

        # Make a rotating backup of the existing file (best-effort).
        backup_path: Optional[str] = None
        if current is not None:
            try:
                backup_dir = os.path.join(os.path.dirname(board_path) or ".", ".mcp-backups")
                os.makedirs(backup_dir, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
                base = os.path.basename(board_path)
                backup_path = os.path.join(backup_dir, f"{base}.{stamp}")
                shutil.copy2(board_path, backup_path)
                self._prune_auto_save_backups(backup_dir, base)
            except OSError as e:
                logger.warning(f"Auto-save backup failed (continuing): {e}")
                backup_path = None

        # Write the board.
        try:
            pcbnew.SaveBoard(board_path, self.board)
            logger.debug(f"Auto-saved board to: {board_path}")
            self._board_disk_signature = self._disk_signature(board_path)
        except Exception as e:
            logger.warning(f"Auto-save failed: {e}")
            return {"saved": False, "error": str(e), "backup": backup_path}

        # Post-save dehydration check. If the BOARD lost its bindings during
        # save, reload from disk while we still know the path. board_path is
        # guaranteed non-empty here (we returned early above otherwise).
        if not self._is_board_healthy():
            logger.warning(
                "Board became dehydrated during auto-save; reloading from %s",
                board_path,
            )
            recovered = self._safe_load_board(board_path)
            if recovered is not None:
                self.board = recovered
                self._update_command_handlers()
            else:
                logger.error(
                    "Board dehydration after auto-save is unrecoverable — "
                    "subsequent commands will fail until MCP restart"
                )

        return {"saved": True, "boardPath": board_path, "backup": backup_path}

    def _update_command_handlers(self) -> None:
        """Update board reference in all command handlers"""
        logger.debug("Updating board reference in command handlers")
        self.project_commands.board = self.board
        self.board_commands.board = self.board
        self.component_commands.board = self.board
        self.routing_commands.board = self.board
        self.design_rule_commands.board = self.board
        self.export_commands.board = self.board
        self.freerouting_commands.board = self.board

    # Stable BOARD methods used to detect SWIG dehydration. Newer KiCAD nightly
    # builds occasionally return a raw SwigPyObject from pcbnew.LoadBoard after
    # certain mutating sequences (delete_trace, refill_zones, …) — the proxy
    # type-checks but every method access raises AttributeError. Probing for
    # these methods catches that state without segfaulting.
    _BOARD_HEALTH_METHODS = (
        "GetDesignSettings",
        "GetBoardEdgesBoundingBox",
        "GetFileName",
    )

    def _is_board_healthy(self, board: Optional[Any] = None) -> bool:
        """Return True if the board (default self.board) has live SWIG dispatch."""
        target = board if board is not None else self.board
        if target is None:
            return False
        return all(hasattr(target, m) for m in self._BOARD_HEALTH_METHODS)

    def _safe_load_board(self, path: str) -> Optional[Any]:
        """Load a board from disk, recovering from SWIG dehydration if pcbnew is broken.

        If pcbnew.LoadBoard returns a dehydrated proxy, reload the pcbnew
        module once and retry. Returns the new board, or None if recovery
        is impossible (caller must surface a real failure rather than fake
        success).
        """
        global pcbnew
        try:
            board = pcbnew.LoadBoard(path)
        except Exception as e:
            logger.error(f"LoadBoard({path!r}) raised: {e}")
            return None

        if self._is_board_healthy(board):
            return board

        logger.warning(
            f"LoadBoard({path!r}) returned a dehydrated SWIG proxy; "
            "reloading pcbnew module and retrying"
        )
        try:
            import importlib

            pcbnew = importlib.reload(pcbnew)
        except Exception as e:
            logger.error(f"pcbnew module reload failed: {e}")
            return None

        try:
            board = pcbnew.LoadBoard(path)
        except Exception as e:
            logger.error(f"LoadBoard retry after pcbnew reload failed: {e}")
            return None

        if not self._is_board_healthy(board):
            logger.error(
                "Board still dehydrated after pcbnew reload; SWIG state is "
                "unrecoverable in this process — restart the MCP server"
            )
            return None

        logger.info("Recovered from SWIG dehydration via pcbnew reload")
        return board

    # Schematic command handlers
    def _handle_create_schematic(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_io as _si
    
        return _si.handle_create_schematic(self, params)
    def _handle_load_schematic(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_io as _si
    
        return _si.handle_load_schematic(self, params)
    def _project_path_from_filename(self, filename: Optional[str]) -> Optional[Path]:
        """Resolve a project directory from a filename param.

        Accepts a .kicad_pro file, a .kicad_pcb file, or a directory.
        """
        if not filename:
            return None
        try:
            p = Path(filename).expanduser()
        except Exception:
            return None
        if p.is_file() or p.suffix in (".kicad_pro", ".kicad_pcb", ".kicad_sch"):
            return p.parent
        return p

    def _refresh_symbol_library_for_project(self, project_path: Optional[Path]) -> None:
        """Rebuild SymbolLibraryCommands' manager so project-scope sym-lib-table
        is visible to subsequent search/list/info calls. No-op if unchanged."""
        if project_path is None:
            return
        self._current_project_path = project_path
        try:
            self.symbol_library_commands.use_project(project_path)
        except Exception as e:
            logger.warning(f"Failed to refresh symbol library for project {project_path}: {e}")

    # Project handlers live in handlers/project.py.
    def _handle_open_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import project as _project

        return _project.handle_open_project(self, params)

    def _handle_create_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import project as _project

        return _project.handle_create_project(self, params)

    def _handle_place_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Place a component on the PCB, with project-local fp-lib-table support.
        If boardPath is given and differs from the currently loaded board, the
        board is reloaded from boardPath before placing — prevents silent failures
        when Claude provides a boardPath that was not yet loaded.
        """
        from pathlib import Path

        board_path = params.get("boardPath")
        if board_path:
            board_path_norm = str(Path(board_path).resolve())
            current_board_file = str(Path(self.board.GetFileName()).resolve()) if self.board else ""
            if board_path_norm != current_board_file:
                logger.info(f"boardPath differs from current board — reloading: {board_path}")
                reloaded = self._safe_load_board(board_path)
                if reloaded is None:
                    return {
                        "success": False,
                        "message": f"Could not load board from boardPath: {board_path}",
                        "errorDetails": (
                            "pcbnew.LoadBoard failed or returned a dehydrated "
                            "SWIG proxy that could not be recovered"
                        ),
                    }
                self.board = reloaded
                self._update_command_handlers()
                logger.info("Board reloaded from boardPath")

            project_path = Path(board_path).parent
            if project_path != getattr(self, "_current_project_path", None):
                self._current_project_path = project_path
                local_lib = FootprintLibraryManager(project_path=project_path)
                self.component_commands = ComponentCommands(self.board, local_lib)
                logger.info(f"Reloaded FootprintLibraryManager with project_path={project_path}")

        return self.component_commands.place_component(params)

    def _handle_add_schematic_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_component as _sc
    
        return _sc.handle_add_schematic_component(self, params)
    def _handle_delete_schematic_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_component as _sc
    
        return _sc.handle_delete_schematic_component(self, params)
    # Built-in property names that have dedicated parameters and cannot be removed
    # via the generic removeProperties path. They are also written by KiCad on every
    # save, so deleting them produces an invalid schematic.
    _PROTECTED_PROPERTY_FIELDS = frozenset({"Reference", "Value", "Footprint", "Datasheet"})

    @staticmethod
    def _escape_sexpr_string(value: str) -> str:
        """Escape a string for safe insertion into an S-expression double-quoted token."""
        return value.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _find_matching_paren(s: str, start: int) -> int:
        """Return the index of the closing paren matching the opening paren at `start`.

        Returns -1 if no match is found. Does not understand string literals — that's
        fine for KiCAD .kicad_sch files because property values cannot contain a
        bare `(` or `)` character (they would be backslash-escaped).
        """
        depth = 0
        i = start
        while i < len(s):
            if s[i] == "(":
                depth += 1
            elif s[i] == ")":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return -1

    def _set_property_in_block(
        self,
        block: str,
        name: str,
        spec: Dict[str, Any],
        default_position: Tuple[float, float],
    ) -> Tuple[str, str]:
        """Add or update a property within a placed-symbol block.

        Args:
            block: The full text of the (symbol ...) block.
            name: Property name (e.g. "MPN", "Manufacturer").
            spec: Dict that may contain keys: value, x, y, angle, hide, fontSize.
            default_position: (x, y) of the parent symbol — used as the default
                location for newly-created properties so the field is anchored
                near the component, not at (0, 0).

        Returns:
            Tuple of (new_block_text, action_taken) where action is "added" or "updated".
        """
        import re

        new_value = spec.get("value")
        new_x = spec.get("x")
        new_y = spec.get("y")
        new_angle = spec.get("angle")
        new_hide = spec.get("hide")
        font_size = spec.get("fontSize", 1.27)

        existing_match = re.search(
            r'\(property\s+"' + re.escape(name) + r'"\s+"',
            block,
        )

        if existing_match:
            # Property exists — patch value / position / hide in place
            if new_value is not None:
                escaped = self._escape_sexpr_string(str(new_value))
                block = re.sub(
                    r'(\(property\s+"' + re.escape(name) + r'"\s+)"[^"]*"',
                    rf'\1"{escaped}"',
                    block,
                    count=1,
                )

            if new_x is not None or new_y is not None or new_angle is not None:
                pos_match = re.search(
                    r'(\(property\s+"'
                    + re.escape(name)
                    + r'"\s+"[^"]*"\s+\(at\s+)([\d\.\-]+)\s+([\d\.\-]+)\s+([\d\.\-]+)(\s*\))',
                    block,
                )
                if pos_match:
                    cx = new_x if new_x is not None else float(pos_match.group(2))
                    cy = new_y if new_y is not None else float(pos_match.group(3))
                    ca = new_angle if new_angle is not None else float(pos_match.group(4))
                    block = (
                        block[: pos_match.start()]
                        + pos_match.group(1)
                        + f"{cx} {cy} {ca}"
                        + pos_match.group(5)
                        + block[pos_match.end() :]
                    )

            if new_hide is not None:
                block = self._set_hide_on_property(block, name, bool(new_hide))

            return block, "updated"

        # Property does not exist — append a new one after the last existing property
        if new_value is None:
            # Adding a brand-new property requires at least a value
            raise ValueError(
                f"Property '{name}' does not exist on this component yet — supply a value to create it"
            )

        cx = new_x if new_x is not None else default_position[0]
        cy = new_y if new_y is not None else default_position[1]
        ca = new_angle if new_angle is not None else 0
        # New properties default to hidden (BOM/sourcing data normally has no
        # visible footprint on the schematic canvas).
        hide_str = "(hide yes)" if (new_hide is None or new_hide) else "(hide no)"
        escaped = self._escape_sexpr_string(str(new_value))
        escaped_name = self._escape_sexpr_string(str(name))

        new_prop = (
            f'    (property "{escaped_name}" "{escaped}" (at {cx} {cy} {ca})\n'
            f"      (effects (font (size {font_size} {font_size})) {hide_str})\n"
            f"    )"
        )

        # Find the last existing property block and insert immediately after it.
        last_prop_end = -1
        for m in re.finditer(r'\(property\s+"', block):
            end = self._find_matching_paren(block, m.start())
            if end > last_prop_end:
                last_prop_end = end

        if last_prop_end < 0:
            # No properties at all — insert just before the closing paren of the symbol
            block_close = block.rfind(")")
            if block_close < 0:
                raise ValueError("Malformed symbol block: no closing paren")
            block = block[:block_close] + "\n" + new_prop + "\n  " + block[block_close:]
        else:
            block = block[: last_prop_end + 1] + "\n" + new_prop + block[last_prop_end + 1 :]

        return block, "added"

    def _set_hide_on_property(self, block: str, name: str, hide: bool) -> str:
        """Set the (hide yes|no) flag on a named property's effects clause.

        Handles three pre-existing forms:
            (effects (font (size 1.27 1.27)))                   — no hide flag
            (effects (font (size 1.27 1.27)) hide)              — legacy bare token
            (effects (font (size 1.27 1.27)) (hide yes|no))     — KiCad 9 form
        """
        import re

        prop_match = re.search(
            r'\(property\s+"' + re.escape(name) + r'"',
            block,
        )
        if not prop_match:
            return block
        prop_start = prop_match.start()
        prop_end = self._find_matching_paren(block, prop_start)
        if prop_end < 0:
            return block

        # Locate the (effects ...) clause inside the property
        prop_segment = block[prop_start : prop_end + 1]
        eff_match = re.search(r"\(effects\b", prop_segment)
        if not eff_match:
            return block
        eff_start = prop_start + eff_match.start()
        eff_end = self._find_matching_paren(block, eff_start)
        if eff_end < 0:
            return block

        eff_inner = block[eff_start + 1 : eff_end]  # 'effects (font ...) ...'
        eff_inner = re.sub(r"\s*\(hide\s+(yes|no)\)", "", eff_inner)
        eff_inner = re.sub(r"\s+hide\b(?!\s+(yes|no))", "", eff_inner)
        eff_inner = eff_inner.rstrip() + f' (hide {"yes" if hide else "no"})'

        new_effects = "(" + eff_inner + ")"
        return block[:eff_start] + new_effects + block[eff_end + 1 :]

    def _remove_property_from_block(self, block: str, name: str) -> Tuple[str, bool]:
        """Remove a property from the symbol block. Returns (new_block, removed_bool)."""
        import re

        m = re.search(r'\(property\s+"' + re.escape(name) + r'"\s+"', block)
        if not m:
            return block, False
        start = m.start()
        end = self._find_matching_paren(block, start)
        if end < 0:
            return block, False

        # Trim surrounding whitespace (leading newline + indent) so the resulting
        # file does not develop blank lines after every removal.
        trim_start = start
        while trim_start > 0 and block[trim_start - 1] in (" ", "\t"):
            trim_start -= 1
        if trim_start > 0 and block[trim_start - 1] == "\n":
            trim_start -= 1
        return block[:trim_start] + block[end + 1 :], True

    def _handle_edit_schematic_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_component as _sc
    
        return _sc.handle_edit_schematic_component(self, params)
    def _handle_set_schematic_component_property(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_component as _sc
    
        return _sc.handle_set_schematic_component_property(self, params)
    def _handle_remove_schematic_component_property(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_component as _sc
    
        return _sc.handle_remove_schematic_component_property(self, params)
    def _handle_get_schematic_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_component as _sc
    
        return _sc.handle_get_schematic_component(self, params)
    def _handle_add_schematic_wire(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_wire as _sw
    
        return _sw.handle_add_schematic_wire(self, params)
    def _handle_list_schematic_libraries(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_query as _sq
    
        return _sq.handle_list_schematic_libraries(self, params)
    def _handle_find_unconnected_pins(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_query as _sq
    
        return _sq.handle_find_unconnected_pins(self, params)
    def _handle_check_wire_collisions(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_query as _sq
    
        return _sq.handle_check_wire_collisions(self, params)
    # Footprint + symbol-creator handlers live in handlers/footprint.py and
    # handlers/symbol_creator.py.

    def _handle_create_footprint(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import footprint as _fp

        return _fp.handle_create_footprint(self, params)

    def _handle_edit_footprint_pad(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import footprint as _fp

        return _fp.handle_edit_footprint_pad(self, params)

    def _handle_list_footprint_libraries(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import footprint as _fp

        return _fp.handle_list_footprint_libraries(self, params)

    def _handle_register_footprint_library(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import footprint as _fp

        return _fp.handle_register_footprint_library(self, params)

    def _handle_create_symbol(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import symbol_creator as _sc

        return _sc.handle_create_symbol(self, params)

    def _handle_delete_symbol(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import symbol_creator as _sc

        return _sc.handle_delete_symbol(self, params)

    def _handle_list_symbols_in_library(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import symbol_creator as _sc

        return _sc.handle_list_symbols_in_library(self, params)

    def _handle_register_symbol_library(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import symbol_creator as _sc

        return _sc.handle_register_symbol_library(self, params)

    def _handle_export_schematic_pdf(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_io as _si
    
        return _si.handle_export_schematic_pdf(self, params)
    def _handle_add_schematic_net_label(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_wire as _sw
    
        return _sw.handle_add_schematic_net_label(self, params)
    def _handle_add_no_connect(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_wire as _sw
    
        return _sw.handle_add_no_connect(self, params)
    def _handle_connect_to_net(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_wire as _sw
    
        return _sw.handle_connect_to_net(self, params)
    def _handle_connect_passthrough(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_wire as _sw
    
        return _sw.handle_connect_passthrough(self, params)
    def _assign_net_to_pad(self, component_ref: str, pin_name: str, net_name: str) -> bool:
        """Assign a net to a specific pad on the PCB board.

        Ensures the net exists on the board and sets it on the matching pad.
        Needed because pcbnew.SaveBoard() drops nets that are not referenced
        by any board element (pad/track/via/zone).
        Returns True if the pad was found and updated.
        """
        board = self.board
        if not board:
            return False

        netinfo = board.GetNetInfo()
        nets_map = netinfo.NetsByName()
        if not nets_map.has_key(net_name):
            net_item = pcbnew.NETINFO_ITEM(board, net_name)
            board.Add(net_item)
            netinfo = board.GetNetInfo()
            nets_map = netinfo.NetsByName()

        if not nets_map.has_key(net_name):
            logger.warning(f"Net '{net_name}' could not be created on board")
            return False

        net_obj = nets_map[net_name]

        for fp in board.GetFootprints():
            if fp.GetReference() == component_ref:
                for pad in fp.Pads():
                    if str(pad.GetNumber()) == str(pin_name):
                        pad.SetNet(net_obj)
                        logger.info(
                            f"Assigned net '{net_name}' to pad {component_ref}/{pin_name} on PCB"
                        )
                        return True
                logger.warning(f"Pad '{pin_name}' not found on footprint '{component_ref}'")
                return False

        logger.warning(f"Footprint '{component_ref}' not found on board")
        return False

    def _handle_get_schematic_pin_locations(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_query as _sq
    
        return _sq.handle_get_schematic_pin_locations(self, params)
    def _handle_get_schematic_view(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get a rasterised image of the schematic (SVG export → optional PNG conversion)"""
        logger.info("Getting schematic view")
        import base64
        import subprocess
        import tempfile

        try:
            schematic_path = params.get("schematicPath")
            if not schematic_path or not os.path.exists(schematic_path):
                return {
                    "success": False,
                    "message": f"Schematic not found: {schematic_path}",
                }

            fmt = params.get("format", "png")
            width = params.get("width", 1200)
            height = params.get("height", 900)

            # Step 1: Export schematic to SVG via kicad-cli
            with tempfile.TemporaryDirectory() as tmpdir:
                svg_path = os.path.join(tmpdir, "schematic.svg")
                cmd = [
                    "kicad-cli",
                    "sch",
                    "export",
                    "svg",
                    "--output",
                    tmpdir,
                    "--no-background-color",
                    schematic_path,
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

                if result.returncode != 0:
                    return {
                        "success": False,
                        "message": f"kicad-cli SVG export failed: {result.stderr}",
                    }

                # kicad-cli may name the file after the schematic, find it
                import glob

                svg_files = glob.glob(os.path.join(tmpdir, "*.svg"))
                if not svg_files:
                    return {
                        "success": False,
                        "message": "No SVG file produced by kicad-cli",
                    }
                svg_path = svg_files[0]

                if fmt == "svg":
                    with open(svg_path, "r", encoding="utf-8") as f:
                        svg_data = f.read()
                    return {"success": True, "imageData": svg_data, "format": "svg"}

                # Step 2: Convert SVG to PNG using cairosvg
                try:
                    from cairosvg import svg2png
                except ImportError:
                    # Fallback: return SVG data with a note
                    with open(svg_path, "r", encoding="utf-8") as f:
                        svg_data = f.read()
                    return {
                        "success": True,
                        "imageData": svg_data,
                        "format": "svg",
                        "message": "cairosvg not installed — returning SVG instead of PNG. Install with: pip install cairosvg",
                    }

                png_data = svg2png(url=svg_path, output_width=width, output_height=height)

                return {
                    "success": True,
                    "imageData": base64.b64encode(png_data).decode("utf-8"),
                    "format": "png",
                    "width": width,
                    "height": height,
                }

        except FileNotFoundError:
            return {"success": False, "message": "kicad-cli not found in PATH"}
        except Exception as e:
            logger.error(f"Error getting schematic view: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return {"success": False, "message": str(e)}

    def _handle_list_schematic_components(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_query as _sq
    
        return _sq.handle_list_schematic_components(self, params)
    def _handle_list_schematic_nets(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_query as _sq
    
        return _sq.handle_list_schematic_nets(self, params)
    def _handle_list_schematic_wires(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_query as _sq
    
        return _sq.handle_list_schematic_wires(self, params)
    def _handle_list_schematic_labels(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_query as _sq
    
        return _sq.handle_list_schematic_labels(self, params)
    def _handle_move_schematic_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_component as _sc
    
        return _sc.handle_move_schematic_component(self, params)
    def _handle_rotate_schematic_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_component as _sc
    
        return _sc.handle_rotate_schematic_component(self, params)
    def _handle_annotate_schematic(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_component as _sc
    
        return _sc.handle_annotate_schematic(self, params)
    def _handle_delete_schematic_wire(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_wire as _sw
    
        return _sw.handle_delete_schematic_wire(self, params)
    def _handle_delete_schematic_net_label(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_wire as _sw
    
        return _sw.handle_delete_schematic_net_label(self, params)
    def _handle_move_schematic_net_label(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_wire as _sw
    
        return _sw.handle_move_schematic_net_label(self, params)
    def _handle_export_schematic_svg(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_io as _si
    
        return _si.handle_export_schematic_svg(self, params)
    def _handle_get_net_connections(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_query as _sq
    
        return _sq.handle_get_net_connections(self, params)
    def _handle_get_wire_connections(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_query as _sq
    
        return _sq.handle_get_wire_connections(self, params)
    def _handle_get_net_at_point(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_query as _sq
    
        return _sq.handle_get_net_at_point(self, params)
    def _handle_list_schematic_texts(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_query as _sq
    
        return _sq.handle_list_schematic_texts(self, params)
    def _handle_add_schematic_text(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_query as _sq
    
        return _sq.handle_add_schematic_text(self, params)
    def _handle_add_schematic_hierarchical_label(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_wire as _sw
    
        return _sw.handle_add_schematic_hierarchical_label(self, params)
    def _handle_add_sheet_pin(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_wire as _sw
    
        return _sw.handle_add_sheet_pin(self, params)
    def _handle_run_erc(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_io as _si
    
        return _si.handle_run_erc(self, params)
    # ------------------------------------------------------------------
    # kicad-cli helper shared by netlist handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_kicad_cli_static() -> Optional[str]:
        """Return path to kicad-cli executable, or None."""
        import platform
        import shutil

        cli = shutil.which("kicad-cli")
        if cli:
            return cli

        system = platform.system()
        if system == "Windows":
            candidates = [
                r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
                r"C:\Program Files\KiCad\8.0\bin\kicad-cli.exe",
                r"C:\Program Files (x86)\KiCad\9.0\bin\kicad-cli.exe",
                r"C:\Program Files (x86)\KiCad\8.0\bin\kicad-cli.exe",
            ]
        elif system == "Darwin":
            candidates = [
                "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
                "/usr/local/bin/kicad-cli",
            ]
        else:
            candidates = [
                "/usr/bin/kicad-cli",
                "/usr/local/bin/kicad-cli",
            ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    # ------------------------------------------------------------------

    def _handle_export_netlist(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_io as _si
    
        return _si.handle_export_netlist(self, params)
    def _handle_generate_netlist(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_io as _si
    
        return _si.handle_generate_netlist(self, params)
    def _build_hierarchical_pad_net_map(self, project_sch_path: str):
        """Walk all .kicad_sch files in the project and build a {(ref, pin_num): net_name} map.

        Handles hierarchical schematics by scanning every sub-sheet file.  Net names
        from global_label / hierarchical_label / local label / power symbols are all
        collected.  Wire connectivity is traced via BFS so labels not placed directly
        on a pin endpoint still reach through wire segments.

        Returns: (pad_net_map, net_names_set)
        """
        from collections import defaultdict
        from pathlib import Path

        from commands.pin_locator import PinLocator
        from skip import Schematic

        TOLERANCE = 0.5  # mm; schematic grid is 1.27 mm so 0.5 is safe

        def snap(x, y):
            """Round to 2 dp to use exact dict lookup instead of O(n²) scan."""
            return (round(float(x), 2), round(float(y), 2))

        def nearby_net(pt, point_net, tol=TOLERANCE):
            """Return net name for the nearest occupied grid point, or None."""
            x, y = pt
            # Try exact snap first (fast path)
            key = snap(x, y)
            if key in point_net:
                return point_net[key]
            # Slow fallback for off-grid placements
            for (lx, ly), name in point_net.items():
                if abs(x - lx) < tol and abs(y - ly) < tol:
                    return name
            return None

        project_dir = Path(project_sch_path).parent
        pad_net_map: dict = {}
        all_net_names: set = set()
        pin_locator = PinLocator()

        sch_files = sorted(project_dir.rglob("*.kicad_sch"))
        logger.info(f"_build_hierarchical_pad_net_map: scanning {len(sch_files)} schematic files")

        for sch_path in sch_files:
            try:
                sch = Schematic(str(sch_path))
            except Exception as e:
                logger.warning(f"Could not load {sch_path}: {e}")
                continue

            # ── 1. Collect explicit label positions → net name ──────────────
            point_net: dict = {}  # snap(x,y) -> net_name

            for attr in ("label", "global_label", "hierarchical_label"):
                for lbl in getattr(sch, attr, None) or []:
                    try:
                        pos = lbl.at.value
                        name = lbl.value
                        if name:
                            k = snap(pos[0], pos[1])
                            point_net[k] = name
                            all_net_names.add(name)
                    except Exception:
                        pass

            # Power symbols (#PWR / #FLG): value property IS the net name; use pin 1 pos
            for sym in getattr(sch, "symbol", None) or []:
                try:
                    ref = sym.property.Reference.value
                    if not (ref.startswith("#PWR") or ref.startswith("#FLG")):
                        continue
                    net_name = sym.property.Value.value
                    if not net_name:
                        continue
                    all_pins = pin_locator.get_all_symbol_pins(sch_path, ref)
                    for _pin_num, (px, py) in all_pins.items():
                        k = snap(px, py)
                        point_net[k] = net_name
                        all_net_names.add(net_name)
                except Exception:
                    pass

            # ── 2. Build wire adjacency and BFS-propagate net names ──────────
            wire_segments = []
            for wire in getattr(sch, "wire", None) or []:
                try:
                    pts = []
                    for pt in wire.pts.xy:
                        pts.append(snap(pt.value[0], pt.value[1]))
                    if len(pts) >= 2:
                        wire_segments.append(pts)
                except Exception:
                    pass

            # Adjacency: connect endpoints of different segments that share a grid point
            point_adj: dict = defaultdict(set)
            for seg in wire_segments:
                # Connect consecutive points within the segment
                for i in range(len(seg) - 1):
                    point_adj[seg[i]].add(seg[i + 1])
                    point_adj[seg[i + 1]].add(seg[i])

            # All unique wire points
            all_wire_pts = set()
            for seg in wire_segments:
                all_wire_pts.update(seg)

            # BFS: propagate known net names through wire connections
            queue = [pt for pt in all_wire_pts if pt in point_net]
            visited = set(queue)
            while queue:
                pt = queue.pop()
                net = point_net[pt]
                for neighbor in point_adj[pt]:
                    if neighbor not in point_net:
                        point_net[neighbor] = net
                        all_net_names.add(net)
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)

            # ── 3. Match component pin positions to net names ────────────────
            for sym in getattr(sch, "symbol", None) or []:
                try:
                    ref = sym.property.Reference.value
                    if ref.startswith("#"):
                        continue
                except Exception:
                    continue

                pin_positions = pin_locator.get_all_symbol_pins(sch_path, ref)
                for pin_num, (px, py) in pin_positions.items():
                    net = nearby_net((px, py), point_net)
                    if net:
                        pad_net_map[(ref, pin_num)] = net

        logger.info(
            f"_build_hierarchical_pad_net_map: {len(pad_net_map)} pin→net assignments, "
            f"{len(all_net_names)} unique nets"
        )
        return pad_net_map, all_net_names

    def _handle_sync_schematic_to_board(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import schematic_io as _si
    
        return _si.handle_sync_schematic_to_board(self, params)
    def _extract_components_from_schematic(self, schematic_path: str) -> List[Dict[str, str]]:
        """Run kicad-cli netlist export and return the flat list of components.

        Each entry: {"reference": str, "value": str, "footprint": str}
        Empty list on any failure (kicad-cli missing, parse error, etc.) — the
        caller treats that as "no missing footprints to add".
        """
        import subprocess
        import tempfile
        import xml.etree.ElementTree as ET

        kicad_cli = self._find_kicad_cli_static()
        if not kicad_cli:
            logger.warning("kicad-cli not found — sync will not add new footprints")
            return []

        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            cmd = [
                kicad_cli,
                "sch",
                "export",
                "netlist",
                "--format",
                "kicadxml",
                "--output",
                tmp_path,
                schematic_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                logger.warning(
                    f"kicad-cli netlist export failed (exit {result.returncode}): "
                    f"{result.stderr.strip()}"
                )
                return []

            tree = ET.parse(tmp_path)
            root = tree.getroot()
            components = []
            for comp in root.findall("./components/comp"):
                components.append(
                    {
                        "reference": comp.get("ref", ""),
                        "value": comp.findtext("value", ""),
                        "footprint": comp.findtext("footprint", ""),
                    }
                )
            return components
        except Exception as e:
            logger.warning(f"Failed to extract components from schematic: {e}")
            return []
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _add_missing_footprints_from_schematic(
        self, board: Any, schematic_path: str
    ) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
        """Add footprints to ``board`` for any schematic component not yet present.

        New footprints are placed at the board origin so the user can move them
        into position. Power/flag references (``#PWR``, ``#FLG``) are skipped —
        they have no PCB representation.

        Returns ``(added, skipped)``: each entry is
        ``{"reference": str, "footprint": str, "reason": str?}``.
        """
        from pathlib import Path

        from commands.library import LibraryManager

        added: List[Dict[str, str]] = []
        skipped: List[Dict[str, str]] = []

        components = self._extract_components_from_schematic(schematic_path)
        if not components:
            return added, skipped

        existing_refs = {fp.GetReference() for fp in board.GetFootprints()}
        project_dir = Path(schematic_path).parent
        library_manager = LibraryManager(project_path=project_dir)

        for comp in components:
            ref = comp["reference"]
            fp_str = comp["footprint"]
            if not ref or ref.startswith("#"):
                # Power flags / global indicators — no PCB footprint expected.
                continue
            if ref in existing_refs:
                continue
            if not fp_str or ":" not in fp_str:
                skipped.append(
                    {
                        "reference": ref,
                        "footprint": fp_str,
                        "reason": "no Library:Name footprint set on schematic symbol",
                    }
                )
                continue

            lib_name, fp_name = fp_str.split(":", 1)
            library_path = library_manager.libraries.get(lib_name)
            if not library_path:
                skipped.append(
                    {
                        "reference": ref,
                        "footprint": fp_str,
                        "reason": f"library '{lib_name}' not in fp-lib-table",
                    }
                )
                continue

            try:
                module = pcbnew.FootprintLoad(library_path, fp_name)
            except Exception as e:
                skipped.append(
                    {"reference": ref, "footprint": fp_str, "reason": f"FootprintLoad failed: {e}"}
                )
                continue

            if not module:
                skipped.append(
                    {
                        "reference": ref,
                        "footprint": fp_str,
                        "reason": f"footprint '{fp_name}' not in '{lib_name}'",
                    }
                )
                continue

            module.SetReference(ref)
            if comp["value"]:
                module.SetValue(comp["value"])
            module.SetFPID(pcbnew.LIB_ID(lib_name, fp_name))
            # Place at board origin; user / autoplacer can position from there.
            module.SetPosition(pcbnew.VECTOR2I(0, 0))

            board.Add(module)
            existing_refs.add(ref)
            added.append({"reference": ref, "footprint": fp_str})

        if added:
            logger.info(f"_add_missing_footprints_from_schematic: added {len(added)} footprints")
        return added, skipped

    # ===================================================================
    # Schematic analysis tools (read-only)
    # ===================================================================

    def _handle_get_schematic_view_region(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Export a cropped region of the schematic as an image"""
        logger.info("Exporting schematic view region")
        import base64
        import os
        import subprocess
        import tempfile

        try:
            schematic_path = params.get("schematicPath")
            if not schematic_path or not os.path.exists(schematic_path):
                return {"success": False, "message": "Schematic file not found"}

            x1 = float(params.get("x1", 0))
            y1 = float(params.get("y1", 0))
            x2 = float(params.get("x2", 297))
            y2 = float(params.get("y2", 210))
            x1, x2 = min(x1, x2), max(x1, x2)
            y1, y2 = min(y1, y2), max(y1, y2)
            out_format = params.get("format", "png")
            width = int(params.get("width", 800))
            height = int(params.get("height", 600))

            kicad_cli = self.design_rule_commands._find_kicad_cli()
            if not kicad_cli:
                return {"success": False, "message": "kicad-cli not found"}

            tmp_dir = tempfile.mkdtemp()
            svg_output = None

            try:
                cmd = [
                    kicad_cli,
                    "sch",
                    "export",
                    "svg",
                    "--output",
                    tmp_dir,
                    schematic_path,
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

                if result.returncode != 0:
                    return {
                        "success": False,
                        "message": f"SVG export failed: {result.stderr}",
                    }

                # kicad-cli names the file after the schematic
                svg_files = [f for f in os.listdir(tmp_dir) if f.endswith(".svg")]
                if not svg_files:
                    return {
                        "success": False,
                        "message": "kicad-cli produced no SVG output",
                    }
                svg_output = os.path.join(tmp_dir, svg_files[0])

                import xml.etree.ElementTree as ET

                tree = ET.parse(svg_output)
                root = tree.getroot()

                # KiCad schematic SVGs use mm as viewBox units directly
                vb = root.get("viewBox", "")
                if vb:
                    parts = vb.split()
                    if len(parts) == 4:
                        orig_vb_x = float(parts[0])
                        orig_vb_y = float(parts[1])

                        new_x = orig_vb_x + x1
                        new_y = orig_vb_y + y1
                        new_w = x2 - x1
                        new_h = y2 - y1

                        root.set("viewBox", f"{new_x} {new_y} {new_w} {new_h}")
                        root.set("width", str(width))
                        root.set("height", str(height))

                # Write modified SVG
                cropped_svg_path = os.path.join(tmp_dir, "cropped.svg")
                tree.write(cropped_svg_path, xml_declaration=True, encoding="utf-8")

                if out_format == "svg":
                    with open(cropped_svg_path, "r", encoding="utf-8") as f:
                        svg_data = f.read()
                    return {"success": True, "imageData": svg_data, "format": "svg"}
                else:
                    try:
                        from cairosvg import svg2png
                    except ImportError:
                        return {
                            "success": False,
                            "message": "PNG export requires the 'cairosvg' package. Install it with: pip install cairosvg",
                        }
                    png_data = svg2png(
                        url=cropped_svg_path, output_width=width, output_height=height
                    )
                    return {
                        "success": True,
                        "imageData": base64.b64encode(png_data).decode("utf-8"),
                        "format": "png",
                    }
            finally:
                import shutil

                shutil.rmtree(tmp_dir, ignore_errors=True)

        except Exception as e:
            logger.error(f"Error in get_schematic_view_region: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return {"success": False, "message": str(e)}

    def _handle_find_overlapping_elements(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Detect spatially overlapping symbols, wires, and labels"""
        logger.info("Finding overlapping elements in schematic")
        try:
            from pathlib import Path

            from commands.schematic_analysis import find_overlapping_elements

            schematic_path = params.get("schematicPath")
            if not schematic_path:
                return {"success": False, "message": "schematicPath is required"}

            tolerance = float(params.get("tolerance", 0.5))
            result = find_overlapping_elements(Path(schematic_path), tolerance)
            return {
                "success": True,
                **result,
                "message": f"Found {result['totalOverlaps']} overlap(s)",
            }
        except Exception as e:
            logger.error(f"Error finding overlapping elements: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return {"success": False, "message": str(e)}

    def _handle_get_elements_in_region(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """List all wires, labels, and symbols within a rectangular region"""
        logger.info("Getting elements in schematic region")
        try:
            from pathlib import Path

            from commands.schematic_analysis import get_elements_in_region

            schematic_path = params.get("schematicPath")
            if not schematic_path:
                return {"success": False, "message": "schematicPath is required"}

            x1 = float(params.get("x1", 0))
            y1 = float(params.get("y1", 0))
            x2 = float(params.get("x2", 0))
            y2 = float(params.get("y2", 0))

            result = get_elements_in_region(Path(schematic_path), x1, y1, x2, y2)
            return {
                "success": True,
                **result,
                "message": f"Found {result['counts']['symbols']} symbols, {result['counts']['wires']} wires, {result['counts']['labels']} labels in region",
            }
        except Exception as e:
            logger.error(f"Error getting elements in region: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return {"success": False, "message": str(e)}

    def _handle_find_wires_crossing_symbols(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Find wires that cross over component symbol bodies"""
        logger.info("Finding wires crossing symbols in schematic")
        try:
            from pathlib import Path

            from commands.schematic_analysis import find_wires_crossing_symbols

            schematic_path = params.get("schematicPath")
            if not schematic_path:
                return {"success": False, "message": "schematicPath is required"}

            result = find_wires_crossing_symbols(Path(schematic_path))
            return {
                "success": True,
                "collisions": result,
                "count": len(result),
                "message": f"Found {len(result)} wire(s) crossing symbols",
            }
        except Exception as e:
            logger.error(f"Error checking wire collisions: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return {"success": False, "message": str(e)}

    def _handle_find_orphaned_wires(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Find wire segments with at least one dangling (unconnected) endpoint"""
        logger.info("Finding orphaned wires in schematic")
        try:
            from pathlib import Path

            from commands.schematic_analysis import find_orphaned_wires

            schematic_path = params.get("schematicPath")
            if not schematic_path:
                return {"success": False, "message": "schematicPath is required"}

            result = find_orphaned_wires(Path(schematic_path))
            return {
                "success": True,
                **result,
                "message": f"Found {result['count']} orphaned wire(s)",
            }
        except Exception as e:
            logger.error(f"Error finding orphaned wires: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return {"success": False, "message": str(e)}

    def _handle_list_floating_labels(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """List net labels that are not connected to any component pin"""
        logger.info("Listing floating net labels in schematic")
        try:
            from commands.wire_connectivity import list_floating_labels

            schematic_path = params.get("schematicPath")
            if not schematic_path:
                return {"success": False, "message": "schematicPath is required"}

            schematic = SchematicManager.load_schematic(schematic_path)
            if not schematic:
                return {"success": False, "message": "Failed to load schematic"}

            labels = list_floating_labels(schematic, schematic_path)
            return {
                "success": True,
                "floating_labels": labels,
                "count": len(labels),
                "message": f"Found {len(labels)} floating label(s)",
            }
        except Exception as e:
            logger.error(f"Error listing floating labels: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return {"success": False, "message": str(e)}

    def _handle_snap_to_grid(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Snap schematic element coordinates to the nearest grid point"""
        logger.info("Snapping schematic elements to grid")
        try:
            from pathlib import Path

            from commands.schematic_snap import snap_to_grid

            schematic_path = params.get("schematicPath")
            if not schematic_path:
                return {"success": False, "message": "schematicPath is required"}

            grid_size = float(params.get("gridSize", 1.27))
            elements = params.get("elements")  # None → defaults inside snap_to_grid

            result = snap_to_grid(Path(schematic_path), grid_size=grid_size, elements=elements)
            total = result["snapped"] + result["already_on_grid"]
            return {
                "success": True,
                **result,
                "message": (
                    f"Snapped {result['snapped']} element(s) to {grid_size} mm grid "
                    f"({result['already_on_grid']} of {total} were already on grid)"
                ),
            }
        except Exception as e:
            logger.error(f"Error snapping to grid: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return {"success": False, "message": str(e)}

    def _handle_import_svg_logo(self, params: Dict[str, Any]) -> Dict[str, Any]:
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
            if result.get("success") and self.board:
                reloaded = self._safe_load_board(pcb_path)
                if reloaded is not None:
                    self.board = reloaded
                    self._update_command_handlers()
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

    def _handle_snapshot_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import project as _project

        return _project.handle_snapshot_project(self, params)

    # UI / backend-state handlers live in handlers/ui.py.  Thin trampolines
    # below preserve the `iface._handle_*` call surface that older tests
    # use; the actual implementation is in handlers.ui.
    def _handle_check_kicad_ui(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import ui as _ui

        return _ui.handle_check_kicad_ui(self, params)

    def _handle_launch_kicad_ui(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import ui as _ui

        return _ui.handle_launch_kicad_ui(self, params)

    def _handle_refill_zones(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import routing as _routing

        return _routing.handle_refill_zones(self, params)

    # =========================================================================
    # IPC Backend handlers - these provide real-time UI synchronization
    # These methods are called automatically when IPC is available
    # =========================================================================

    def _ipc_route_trace(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for route_trace - adds track with real-time UI update"""
        try:
            # Extract parameters matching the existing route_trace interface
            start = params.get("start", {})
            end = params.get("end", {})
            layer = params.get("layer", "F.Cu")
            width = params.get("width", 0.25)
            net = params.get("net")

            # Handle both dict format and direct x/y
            start_x = start.get("x", 0) if isinstance(start, dict) else params.get("startX", 0)
            start_y = start.get("y", 0) if isinstance(start, dict) else params.get("startY", 0)
            end_x = end.get("x", 0) if isinstance(end, dict) else params.get("endX", 0)
            end_y = end.get("y", 0) if isinstance(end, dict) else params.get("endY", 0)

            success = self.ipc_board_api.add_track(
                start_x=start_x,
                start_y=start_y,
                end_x=end_x,
                end_y=end_y,
                width=width,
                layer=layer,
                net_name=net,
            )

            return {
                "success": success,
                "message": (
                    "Added trace (visible in KiCAD UI)" if success else "Failed to add trace"
                ),
                "trace": {
                    "start": {"x": start_x, "y": start_y, "unit": "mm"},
                    "end": {"x": end_x, "y": end_y, "unit": "mm"},
                    "layer": layer,
                    "width": width,
                    "net": net,
                },
            }
        except Exception as e:
            logger.error(f"IPC route_trace error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_route_arc_trace(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for route_arc_trace - adds copper arc with real-time UI update"""
        try:
            start = params.get("start", {})
            mid = params.get("mid", {})
            end = params.get("end", {})
            layer = params.get("layer", "F.Cu")
            width = params.get("width", 0.25)
            net = params.get("net")

            start_x = start.get("x", 0)
            start_y = start.get("y", 0)
            mid_x = mid.get("x", 0)
            mid_y = mid.get("y", 0)
            end_x = end.get("x", 0)
            end_y = end.get("y", 0)

            if not hasattr(self.ipc_board_api, "add_arc_track"):
                return {
                    "success": False,
                    "message": "IPC backend does not support arc track on this installation",
                }

            success = self.ipc_board_api.add_arc_track(
                start_x=start_x,
                start_y=start_y,
                mid_x=mid_x,
                mid_y=mid_y,
                end_x=end_x,
                end_y=end_y,
                width=width,
                layer=layer,
                net_name=net,
            )

            return {
                "success": success,
                "message": (
                    "Added arc trace (visible in KiCAD UI)"
                    if success
                    else "Failed to add arc trace"
                ),
                "arc": {
                    "start": {"x": start_x, "y": start_y, "unit": "mm"},
                    "mid": {"x": mid_x, "y": mid_y, "unit": "mm"},
                    "end": {"x": end_x, "y": end_y, "unit": "mm"},
                    "layer": layer,
                    "width": width,
                    "net": net,
                },
            }
        except Exception as e:
            logger.error(f"IPC route_arc_trace error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_add_via(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for add_via - adds via with real-time UI update"""
        try:
            position = params.get("position", {})
            x = position.get("x", 0) if isinstance(position, dict) else params.get("x", 0)
            y = position.get("y", 0) if isinstance(position, dict) else params.get("y", 0)

            size = params.get("size", 0.8)
            drill = params.get("drill", 0.4)
            net = params.get("net")
            from_layer = params.get("from_layer", "F.Cu")
            to_layer = params.get("to_layer", "B.Cu")

            success = self.ipc_board_api.add_via(
                x=x, y=y, diameter=size, drill=drill, net_name=net, via_type="through"
            )

            return {
                "success": success,
                "message": ("Added via (visible in KiCAD UI)" if success else "Failed to add via"),
                "via": {
                    "position": {"x": x, "y": y, "unit": "mm"},
                    "size": size,
                    "drill": drill,
                    "from_layer": from_layer,
                    "to_layer": to_layer,
                    "net": net,
                },
            }
        except Exception as e:
            logger.error(f"IPC add_via error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_add_net(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for add_net"""
        # Note: Net creation via IPC is limited - nets are typically created
        # when components are placed. Return success for compatibility.
        name = params.get("name")
        logger.info(f"IPC add_net: {name} (nets auto-created with components)")
        return {
            "success": True,
            "message": f"Net '{name}' will be created when components are connected",
            "net": {"name": name},
        }

    def _ipc_add_copper_pour(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for add_copper_pour - adds zone with real-time UI update"""
        try:
            layer = params.get("layer", "F.Cu")
            net = params.get("net")
            clearance = params.get("clearance", 0.5)
            min_width = params.get("minWidth", 0.25)
            points = params.get("points", [])
            priority = params.get("priority", 0)
            fill_type = params.get("fillType", "solid")
            name = params.get("name", "")

            if not points or len(points) < 3:
                return {
                    "success": False,
                    "message": "At least 3 points are required for copper pour outline",
                }

            # Convert points format if needed (handle both {x, y} and {x, y, unit})
            formatted_points = []
            for point in points:
                formatted_points.append({"x": point.get("x", 0), "y": point.get("y", 0)})

            success = self.ipc_board_api.add_zone(
                points=formatted_points,
                layer=layer,
                net_name=net,
                clearance=clearance,
                min_thickness=min_width,
                priority=priority,
                fill_mode=fill_type,
                name=name,
            )

            return {
                "success": success,
                "message": (
                    "Added copper pour (visible in KiCAD UI)"
                    if success
                    else "Failed to add copper pour"
                ),
                "pour": {
                    "layer": layer,
                    "net": net,
                    "clearance": clearance,
                    "minWidth": min_width,
                    "priority": priority,
                    "fillType": fill_type,
                    "pointCount": len(points),
                },
            }
        except Exception as e:
            logger.error(f"IPC add_copper_pour error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_refill_zones(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for refill_zones - refills all zones with real-time UI update"""
        try:
            success = self.ipc_board_api.refill_zones()

            return {
                "success": success,
                "message": (
                    "Zones refilled (visible in KiCAD UI)" if success else "Failed to refill zones"
                ),
            }
        except Exception as e:
            logger.error(f"IPC refill_zones error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_add_text(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for add_text/add_board_text - adds text with real-time UI update"""
        try:
            text = params.get("text", "")
            position = params.get("position", {})
            x = position.get("x", 0) if isinstance(position, dict) else params.get("x", 0)
            y = position.get("y", 0) if isinstance(position, dict) else params.get("y", 0)
            layer = params.get("layer", "F.SilkS")
            size = params.get("size", 1.0)
            rotation = params.get("rotation", 0)

            success = self.ipc_board_api.add_text(
                text=text, x=x, y=y, layer=layer, size=size, rotation=rotation
            )

            return {
                "success": success,
                "message": (
                    f"Added text '{text}' (visible in KiCAD UI)"
                    if success
                    else "Failed to add text"
                ),
            }
        except Exception as e:
            logger.error(f"IPC add_text error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_set_board_size(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for set_board_size"""
        try:
            width = params.get("width", 100)
            height = params.get("height", 100)
            unit = params.get("unit", "mm")

            success = self.ipc_board_api.set_size(width, height, unit)

            return {
                "success": success,
                "message": (
                    f"Board size set to {width}x{height} {unit} (visible in KiCAD UI)"
                    if success
                    else "Failed to set board size"
                ),
                "boardSize": {"width": width, "height": height, "unit": unit},
            }
        except Exception as e:
            logger.error(f"IPC set_board_size error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_get_board_info(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for get_board_info"""
        try:
            size = self.ipc_board_api.get_size()
            components = self.ipc_board_api.list_components()
            tracks = self.ipc_board_api.get_tracks()
            vias = self.ipc_board_api.get_vias()
            nets = self.ipc_board_api.get_nets()

            return {
                "success": True,
                "boardInfo": {
                    "size": size,
                    "componentCount": len(components),
                    "trackCount": len(tracks),
                    "viaCount": len(vias),
                    "netCount": len(nets),
                    "backend": "ipc",
                    "realtime": True,
                },
            }
        except Exception as e:
            logger.error(f"IPC get_board_info error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_place_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for place_component - places component with real-time UI update"""
        try:
            reference = params.get("reference", params.get("componentId", ""))
            footprint = params.get("footprint", "")
            position = params.get("position", {})
            x = position.get("x", 0) if isinstance(position, dict) else params.get("x", 0)
            y = position.get("y", 0) if isinstance(position, dict) else params.get("y", 0)
            unit = position.get("unit", "mm") if isinstance(position, dict) else "mm"
            rotation = params.get("rotation", 0)
            layer = params.get("layer", "F.Cu")
            value = params.get("value", "")

            # Convert to mm since ipc_backend expects mm
            if unit == "inch":
                x = x * 25.4
                y = y * 25.4
            elif unit == "mil":
                x = x * 0.0254
                y = y * 0.0254

            success = self.ipc_board_api.place_component(
                reference=reference,
                footprint=footprint,
                x=x,
                y=y,
                rotation=rotation,
                layer=layer,
                value=value,
            )

            return {
                "success": success,
                "message": (
                    f"Placed component {reference} (visible in KiCAD UI)"
                    if success
                    else "Failed to place component"
                ),
                "component": {
                    "reference": reference,
                    "footprint": footprint,
                    "position": {"x": x, "y": y, "unit": "mm"},
                    "rotation": rotation,
                    "layer": layer,
                },
            }
        except Exception as e:
            logger.error(f"IPC place_component error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_move_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for move_component - moves component with real-time UI update"""
        try:
            reference = params.get("reference", params.get("componentId", ""))
            position = params.get("position", {})
            x = position.get("x", 0) if isinstance(position, dict) else params.get("x", 0)
            y = position.get("y", 0) if isinstance(position, dict) else params.get("y", 0)
            unit = position.get("unit", "mm") if isinstance(position, dict) else "mm"
            rotation = params.get("rotation")

            # Convert to mm since ipc_backend.move_component expects mm
            if unit == "inch":
                x = x * 25.4
                y = y * 25.4
            elif unit == "mil":
                x = x * 0.0254
                y = y * 0.0254

            success = self.ipc_board_api.move_component(
                reference=reference, x=x, y=y, rotation=rotation
            )

            return {
                "success": success,
                "message": (
                    f"Moved component {reference} (visible in KiCAD UI)"
                    if success
                    else "Failed to move component"
                ),
            }
        except Exception as e:
            logger.error(f"IPC move_component error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_delete_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for delete_component - deletes component with real-time UI update"""
        try:
            reference = params.get("reference", params.get("componentId", ""))

            success = self.ipc_board_api.delete_component(reference=reference)

            return {
                "success": success,
                "message": (
                    f"Deleted component {reference} (visible in KiCAD UI)"
                    if success
                    else "Failed to delete component"
                ),
            }
        except Exception as e:
            logger.error(f"IPC delete_component error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_get_component_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for get_component_list"""
        try:
            components = self.ipc_board_api.list_components()

            # If IPC didn't provide bounding boxes, enrich from SWIG backend
            if self.board and components and not components[0].get("boundingBox"):
                try:
                    swig_result = self.component_commands.get_component_list(params)
                    if swig_result.get("success"):
                        swig_map = {c["reference"]: c for c in swig_result.get("components", [])}
                        for comp in components:
                            swig_comp = swig_map.get(comp.get("reference"))
                            if swig_comp and swig_comp.get("boundingBox"):
                                comp["boundingBox"] = swig_comp["boundingBox"]
                except Exception:
                    pass

            return {"success": True, "components": components, "count": len(components)}
        except Exception as e:
            logger.error(f"IPC get_component_list error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_save_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for save_project"""
        try:
            success = self.ipc_board_api.save()

            return {
                "success": success,
                "message": "Project saved" if success else "Failed to save project",
            }
        except Exception as e:
            logger.error(f"IPC save_project error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_delete_trace(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for delete_trace - Note: IPC doesn't support direct trace deletion yet"""
        # IPC API doesn't have a direct delete track method
        # Fall back to SWIG for this operation
        logger.info("delete_trace: Falling back to SWIG (IPC doesn't support trace deletion)")
        return self.routing_commands.delete_trace(params)

    def _ipc_query_traces(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for query_traces - reads traces from the live KiCAD board."""
        try:
            net_name = params.get("net")
            layer_filter = params.get("layer")
            bbox = params.get("boundingBox")
            include_vias = params.get("includeVias", False)

            def point_in_bbox(point: Dict[str, Any]) -> bool:
                if not bbox:
                    return True
                unit_scale = 25.4 if bbox.get("unit", "mm") == "inch" else 1.0
                x1 = bbox.get("x1", 0) * unit_scale
                y1 = bbox.get("y1", 0) * unit_scale
                x2 = bbox.get("x2", 0) * unit_scale
                y2 = bbox.get("y2", 0) * unit_scale
                low_x, high_x = sorted((x1, x2))
                low_y, high_y = sorted((y1, y2))
                return low_x <= point.get("x", 0) <= high_x and low_y <= point.get("y", 0) <= high_y

            traces = []
            for track in self.ipc_board_api.get_tracks():
                if net_name and track.get("net") != net_name:
                    continue

                layer = self._normalize_ipc_layer_name(track.get("layer", ""))
                if layer_filter and layer != layer_filter:
                    continue

                start = track.get("start", {})
                end = track.get("end", {})
                if bbox and not (point_in_bbox(start) or point_in_bbox(end)):
                    continue

                start_with_unit = {**start, "unit": "mm"}
                end_with_unit = {**end, "unit": "mm"}
                dx = end.get("x", 0) - start.get("x", 0)
                dy = end.get("y", 0) - start.get("y", 0)
                traces.append(
                    {
                        "uuid": track.get("id", ""),
                        "net": track.get("net", ""),
                        "netCode": track.get("netCode", 0),
                        "layer": layer,
                        "width": track.get("width", 0),
                        "start": start_with_unit,
                        "end": end_with_unit,
                        "length": (dx**2 + dy**2) ** 0.5,
                    }
                )

            result = {"success": True, "traceCount": len(traces), "traces": traces}

            if include_vias:
                vias = []
                for via in self.ipc_board_api.get_vias():
                    if net_name and via.get("net") != net_name:
                        continue
                    position = via.get("position", {})
                    if bbox and not point_in_bbox(position):
                        continue
                    vias.append(
                        {
                            "uuid": via.get("id", ""),
                            "position": {**position, "unit": "mm"},
                            "net": via.get("net", ""),
                            "netCode": via.get("netCode", 0),
                            "diameter": via.get("diameter", 0),
                            "drill": via.get("drill", 0),
                        }
                    )
                result["viaCount"] = len(vias)
                result["vias"] = vias

            return result
        except Exception as e:
            logger.error(f"IPC query_traces error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_get_nets_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for get_nets_list - gets nets with real-time data"""
        try:
            nets = self.ipc_board_api.get_nets()

            return {"success": True, "nets": nets, "count": len(nets)}
        except Exception as e:
            logger.error(f"IPC get_nets_list error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_add_board_outline(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for add_board_outline - adds board edge with real-time UI update.
        Rounded rectangles are delegated to the SWIG path because the IPC BoardSegment
        type cannot represent arcs; the SWIG path writes directly to the .kicad_pcb file
        and correctly generates PCB_SHAPE arcs for rounded corners.
        """
        shape = params.get("shape", "rectangle")
        if shape in ("rounded_rectangle", "rectangle"):
            # IPC path only supports straight segments from a points list,
            # but Claude sends rectangle/rounded_rectangle as shape+width+height.
            # Fall back to the SWIG path which correctly handles both shapes.
            logger.info(f"_ipc_add_board_outline: delegating {shape} to SWIG path")
            return self.board_commands.add_board_outline(params)

        try:
            from kipy.board_types import BoardSegment
            from kipy.geometry import Vector2
            from kipy.proto.board.board_types_pb2 import BoardLayer
            from kipy.util.units import from_mm

            board = self.ipc_board_api._get_board()

            # Unwrap nested params (Claude sends {"shape":..., "params":{...}})
            inner = params.get("params", params)
            points = inner.get("points", params.get("points", []))
            width = inner.get("width", params.get("width", 0.1))

            if len(points) < 2:
                return {
                    "success": False,
                    "message": "At least 2 points required for board outline",
                }

            commit = board.begin_commit()
            lines_created = 0

            # Create line segments connecting the points
            for i in range(len(points)):
                start = points[i]
                end = points[(i + 1) % len(points)]  # Wrap around to close the outline

                segment = BoardSegment()
                segment.start = Vector2.from_xy(
                    from_mm(start.get("x", 0)), from_mm(start.get("y", 0))
                )
                segment.end = Vector2.from_xy(from_mm(end.get("x", 0)), from_mm(end.get("y", 0)))
                segment.layer = BoardLayer.BL_Edge_Cuts
                segment.attributes.stroke.width = from_mm(width)

                board.create_items(segment)
                lines_created += 1

            board.push_commit(commit, "Added board outline")

            return {
                "success": True,
                "message": f"Added board outline with {lines_created} segments (visible in KiCAD UI)",
                "segments": lines_created,
            }
        except Exception as e:
            logger.error(f"IPC add_board_outline error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_add_mounting_hole(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for add_mounting_hole - adds mounting hole with real-time UI update"""
        try:
            from kipy.board_types import BoardCircle
            from kipy.geometry import Vector2
            from kipy.proto.board.board_types_pb2 import BoardLayer
            from kipy.util.units import from_mm

            board = self.ipc_board_api._get_board()

            x = params.get("x", 0)
            y = params.get("y", 0)
            diameter = params.get("diameter", 3.2)  # M3 hole default

            commit = board.begin_commit()

            # Create circle on Edge.Cuts layer for the hole
            circle = BoardCircle()
            circle.center = Vector2.from_xy(from_mm(x), from_mm(y))
            circle.radius = from_mm(diameter / 2)
            circle.layer = BoardLayer.BL_Edge_Cuts
            circle.attributes.stroke.width = from_mm(0.1)

            board.create_items(circle)
            board.push_commit(commit, f"Added mounting hole at ({x}, {y})")

            return {
                "success": True,
                "message": f"Added mounting hole at ({x}, {y}) mm (visible in KiCAD UI)",
                "hole": {"position": {"x": x, "y": y}, "diameter": diameter},
            }
        except Exception as e:
            logger.error(f"IPC add_mounting_hole error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_get_layer_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for get_layer_list - gets enabled layers"""
        try:
            layers = self.ipc_board_api.get_enabled_layers()

            return {"success": True, "layers": layers, "count": len(layers)}
        except Exception as e:
            logger.error(f"IPC get_layer_list error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_rotate_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for rotate_component - rotates component with real-time UI update"""
        try:
            reference = params.get("reference", params.get("componentId", ""))
            angle = params.get("angle", params.get("rotation", 90))

            # Get current component to find its position
            components = self.ipc_board_api.list_components()
            target = None
            for comp in components:
                if comp.get("reference") == reference:
                    target = comp
                    break

            if not target:
                return {"success": False, "message": f"Component {reference} not found"}

            # Use angle as absolute rotation (matches schema description)
            new_rotation = angle % 360

            # Use move_component with new rotation (position stays the same)
            success = self.ipc_board_api.move_component(
                reference=reference,
                x=target.get("position", {}).get("x", 0),
                y=target.get("position", {}).get("y", 0),
                rotation=new_rotation,
            )

            return {
                "success": success,
                "message": (
                    f"Rotated component {reference} by {angle}° (visible in KiCAD UI)"
                    if success
                    else "Failed to rotate component"
                ),
                "newRotation": new_rotation,
            }
        except Exception as e:
            logger.error(f"IPC rotate_component error: {e}")
            return {"success": False, "message": str(e)}

    def _ipc_get_component_properties(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """IPC handler for get_component_properties - gets detailed component info"""
        try:
            reference = params.get("reference", params.get("componentId", ""))

            components = self.ipc_board_api.list_components()
            target = None
            for comp in components:
                if comp.get("reference") == reference:
                    target = comp
                    break

            if not target:
                return {"success": False, "message": f"Component {reference} not found"}

            # If IPC didn't provide bounding box, try SWIG backend as fallback
            if not target.get("boundingBox") and self.board:
                try:
                    swig_result = self.component_commands.get_component_properties(params)
                    if swig_result.get("success"):
                        swig_comp = swig_result.get("component", {})
                        target["boundingBox"] = swig_comp.get("boundingBox")
                        target["courtyard"] = swig_comp.get("courtyard")
                except Exception:
                    pass

            return {"success": True, "component": target}
        except Exception as e:
            logger.error(f"IPC get_component_properties error: {e}")
            return {"success": False, "message": str(e)}

    # =========================================================================
    # Legacy IPC command handlers (explicit ipc_* commands)

    def _handle_warmup(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Force full pcbnew/wxApp initialisation.

        On macOS the wxApp singleton is created lazily on the first
        pcbnew operation that needs it (not on ``import pcbnew``).
        That first call can take 55-65 s outside the KiCad GUI, which
        exceeds the 30 s default MCP-client tool-call timeout.

        This handler is called by the TypeScript server during startup
        (with a 120 s timeout) so the cost is paid before any user
        tools are registered with the MCP client.
        """
        import time

        start = time.monotonic()
        try:
            # pcbnew.BOARD() triggers wxApp creation on macOS.
            # GetBuildVersion() alone is too cheap — it doesn't
            # force the wxWidgets event loop to materialise.
            board = pcbnew.BOARD()
            del board
            ver = pcbnew.GetBuildVersion()
            elapsed = time.monotonic() - start
            logger.info(f"Warm-up complete: pcbnew {ver} ({elapsed:.1f}s)")
            return {"success": True, "version": ver, "elapsed_s": round(elapsed, 1)}
        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.error(f"Warm-up failed after {elapsed:.1f}s: {exc}")
            return {"success": False, "message": str(exc), "elapsed_s": round(elapsed, 1)}

    # =========================================================================

    # Backend-info / backend-state handlers live in handlers/ui.py.
    def _handle_get_backend_info(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import ui as _ui

        return _ui.handle_get_backend_info(self, params)

    def _handle_get_backend_state(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import ui as _ui

        return _ui.handle_get_backend_state(self, params)

    # IPC-specific handlers live in handlers/ipc.py.
    def _handle_ipc_add_track(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import ipc as _ipc

        return _ipc.handle_ipc_add_track(self, params)

    def _handle_ipc_add_via(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import ipc as _ipc

        return _ipc.handle_ipc_add_via(self, params)

    def _handle_ipc_add_text(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import ipc as _ipc

        return _ipc.handle_ipc_add_text(self, params)

    def _handle_ipc_list_components(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import ipc as _ipc

        return _ipc.handle_ipc_list_components(self, params)

    def _handle_ipc_get_tracks(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import ipc as _ipc

        return _ipc.handle_ipc_get_tracks(self, params)

    def _handle_ipc_get_vias(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import ipc as _ipc

        return _ipc.handle_ipc_get_vias(self, params)

    def _handle_ipc_save_board(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import ipc as _ipc

        return _ipc.handle_ipc_save_board(self, params)

    # JLCPCB API handlers

    # JLCPCB + datasheet handlers live in handlers/jlcpcb.py and
    # handlers/datasheet.py.

    def _handle_download_jlcpcb_database(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import jlcpcb as _jlc

        return _jlc.handle_download_jlcpcb_database(self, params)

    def _handle_search_jlcpcb_parts(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import jlcpcb as _jlc

        return _jlc.handle_search_jlcpcb_parts(self, params)

    def _handle_get_jlcpcb_part(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import jlcpcb as _jlc

        return _jlc.handle_get_jlcpcb_part(self, params)

    def _handle_get_jlcpcb_database_stats(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import jlcpcb as _jlc

        return _jlc.handle_get_jlcpcb_database_stats(self, params)

    def _handle_suggest_jlcpcb_alternatives(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import jlcpcb as _jlc

        return _jlc.handle_suggest_jlcpcb_alternatives(self, params)

    def _handle_enrich_datasheets(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import datasheet as _ds

        return _ds.handle_enrich_datasheets(self, params)

    def _handle_get_datasheet_url(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from handlers import datasheet as _ds

        return _ds.handle_get_datasheet_url(self, params)


def _write_response(response_fd: Any, response: Any) -> None:
    """Write a JSON response to the original stdout fd.

    All response output goes through this function so that stray C-level
    writes from pcbnew (warnings, diagnostics) never corrupt the JSON
    framing seen by the TypeScript host.
    """
    payload = json.dumps(response) + "\n"
    os.write(response_fd, payload.encode("utf-8"))


def main() -> None:
    """Main entry point"""
    # --- Redirect stdout so pcbnew C++ noise never reaches the TS host ---
    # Save the real stdout fd for our exclusive JSON response channel.
    _response_fd = os.dup(1)
    # Point fd 1 (C-level stdout) at stderr so that any printf / std::cout
    # output from pcbnew or other C extensions is visible in logs but does
    # NOT corrupt the JSON stream the TypeScript side is parsing.
    os.dup2(2, 1)
    # Also redirect Python-level stdout to stderr for the same reason.
    sys.stdout = sys.stderr

    logger.info("Starting KiCAD interface...")
    interface = KiCADInterface()
    # Signal to the TypeScript server that the stdin loop is live.
    _write_response(_response_fd, {"type": "ready"})

    try:
        logger.info("Processing commands from stdin...")
        # Process commands from stdin
        for line in sys.stdin:
            try:
                # Parse command
                logger.debug(f"Received input: {line.strip()}")
                command_data = json.loads(line)

                # Check if this is JSON-RPC 2.0 format
                if "jsonrpc" in command_data and command_data["jsonrpc"] == "2.0":
                    logger.info("Detected JSON-RPC 2.0 format message")
                    method = command_data.get("method")
                    params = command_data.get("params", {})
                    request_id = command_data.get("id")

                    # Handle MCP protocol methods
                    if method == "initialize":
                        logger.info("Handling MCP initialize")
                        response = {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": {
                                "protocolVersion": "2025-06-18",
                                "capabilities": {
                                    "tools": {"listChanged": True},
                                    "resources": {
                                        "subscribe": False,
                                        "listChanged": True,
                                    },
                                },
                                "serverInfo": {
                                    "name": "kicad-mcp-server",
                                    "title": "KiCAD PCB Design Assistant",
                                    "version": "2.1.0-alpha",
                                },
                                "instructions": "AI-assisted PCB design with KiCAD. Use tools to create projects, design boards, place components, route traces, and export manufacturing files.",
                            },
                        }
                    elif method == "tools/list":
                        logger.info("Handling MCP tools/list")
                        # Return list of available tools with proper schemas
                        tools = []
                        for cmd_name in interface.command_routes.keys():
                            if cmd_name in TOOL_SCHEMAS:
                                # Enrich the existing schema with IPC annotation data
                                # (adds description/blocking hints where the schema lacks them)
                                tool_def = _annotation_loader.enrich_schema(
                                    cmd_name, TOOL_SCHEMAS[cmd_name]
                                )
                                tools.append(tool_def)
                            else:
                                # Build a best-effort schema from IPC annotations
                                ann_desc = _annotation_loader.description(cmd_name)
                                if ann_desc:
                                    logger.debug(f"Using IPC annotation for tool: {cmd_name}")
                                else:
                                    logger.warning(f"No schema or annotation for tool: {cmd_name}")
                                tools.append(
                                    _annotation_loader.enrich_schema(
                                        cmd_name,
                                        {
                                            "name": cmd_name,
                                            "description": ann_desc or f"KiCAD command: {cmd_name}",
                                            "inputSchema": {
                                                "type": "object",
                                                "properties": {},
                                            },
                                        },
                                    )
                                )

                        logger.info(f"Returning {len(tools)} tools")
                        response = {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": {"tools": tools},
                        }
                    elif method == "tools/call":
                        logger.info("Handling MCP tools/call")
                        tool_name = params.get("name")
                        tool_params = params.get("arguments", {})

                        # Execute the command
                        result = interface.handle_command(tool_name, tool_params)

                        response = {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": {"content": [{"type": "text", "text": json.dumps(result)}]},
                        }
                    elif method == "resources/list":
                        logger.info("Handling MCP resources/list")
                        # Return list of available resources
                        response = {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": {"resources": RESOURCE_DEFINITIONS},
                        }
                    elif method == "resources/read":
                        logger.info("Handling MCP resources/read")
                        resource_uri = params.get("uri")

                        if not resource_uri:
                            response = {
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "error": {
                                    "code": -32602,
                                    "message": "Missing required parameter: uri",
                                },
                            }
                        else:
                            # Read the resource
                            resource_data = handle_resource_read(resource_uri, interface)

                            response = {
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "result": resource_data,
                            }
                    else:
                        logger.error(f"Unknown JSON-RPC method: {method}")
                        response = {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {
                                "code": -32601,
                                "message": f"Method not found: {method}",
                            },
                        }
                else:
                    # Handle legacy custom format
                    logger.info("Detected custom format message")
                    command = command_data.get("command")
                    params = command_data.get("params", {})

                    if not command:
                        logger.error("Missing command field")
                        response = {
                            "success": False,
                            "message": "Missing command",
                            "errorDetails": "The command field is required",
                        }
                    else:
                        # Handle command
                        response = interface.handle_command(command, params)

                # Send response via the clean fd (immune to pcbnew stdout noise)
                logger.debug(f"Sending response: {response}")
                _write_response(_response_fd, response)

            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON input: {str(e)}")
                response = {
                    "success": False,
                    "message": "Invalid JSON input",
                    "errorDetails": str(e),
                }
                _write_response(_response_fd, response)

    except KeyboardInterrupt:
        logger.info("KiCAD interface stopped")
        sys.exit(0)

    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}\n{traceback.format_exc()}")
        sys.exit(1)


if __name__ == "__main__":
    main()
