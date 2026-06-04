#!/usr/bin/env python3
"""
KiCAD Python Interface Script for Model Context Protocol

This script handles communication between the MCP TypeScript server
and KiCAD's Python API (pcbnew). It receives commands via stdin as
JSON and returns responses via stdout also as JSON.
"""

import json
import logging
import os
import sys
import time
import traceback
from logging.handlers import RotatingFileHandler
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

from annotations import AnnotationLoader
from board_persistence import BoardPersistenceMixin
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
#
# KICAD_MCP_LOG_DIR overrides the directory (set to "" to disable file
# logging entirely). In production there is exactly one Python subprocess
# writing this file, so RotatingFileHandler's rollover is single-writer and
# safe; the test suite points this elsewhere (see conftest) so concurrent
# pytest processes neither race on rollover nor pollute the real log.
_log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
_log_level = getattr(logging, _log_level_name, logging.INFO)
_log_format = "%(asctime)s [%(levelname)s] %(message)s"
_log_dir_override = os.environ.get("KICAD_MCP_LOG_DIR")
try:
    if _log_dir_override is not None and _log_dir_override == "":
        raise OSError("file logging disabled via KICAD_MCP_LOG_DIR=''")
    log_dir = _log_dir_override or os.path.join(os.path.expanduser("~"), ".kicad-mcp", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "kicad_interface.log")
    # Rotate so the log can never grow unbounded. A plain FileHandler here once
    # let a hot-path warning balloon the file to ~700 MB; cap at 20 MB x 5
    # backups (~100 MB ceiling) so any future log storm self-trims.
    logging.basicConfig(
        level=_log_level,
        format=_log_format,
        handlers=[
            RotatingFileHandler(
                log_file,
                maxBytes=20 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
        ],
        force=True,  # override any prior basicConfig (e.g. by upstream imports)
    )
except (OSError, PermissionError):
    logging.basicConfig(
        level=_log_level,
        format=_log_format,
        force=True,
    )
logger = logging.getLogger("kicad_interface")

# Silence kicad-skip's benign per-element chatter. It logs a WARNING
# ("Passed key  -- can't parsy") from NamedElementCollection._cleanse_key for
# every embedded lib_symbol pin whose name is empty ('' rather than '~') — a
# normal occurrence for stock symbols (e.g. Device:R). Each schematic load
# re-parses lib_symbols, so across reloads this once filled the log with
# thousands of identical lines. We never use skip's named-pin index (pin
# lookups go through commands/pin_locator.py), so this is pure noise; keep
# ERROR so real skip failures still surface.
logging.getLogger("skip").setLevel(logging.ERROR)

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

from utils.kicad_process import (  # noqa: F401  (check_and_launch_kicad exposed for tests/mock.patch)
    KiCADProcessManager,
    check_and_launch_kicad,
)

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

# Always make pcbnew available at module scope.
#
# The original logic imported pcbnew only when the SWIG backend was selected,
# which left ``kicad_interface.pcbnew`` undefined whenever the IPC backend
# succeeded.  Under unittest.mock that meant ``patch("kicad_interface.pcbnew")``
# in any subsequent test raised AttributeError — which is what produces the 18
# "pre-existing" pollution failures the audit surfaced.
#
# Importing pcbnew is cheap by itself; the wxApp init cost only triggers on
# ``pcbnew.GetBuildVersion()``, which still lives in the warm-up handler.
try:
    import pcbnew  # type: ignore
except ImportError:
    pcbnew = None  # type: ignore[assignment]

# Fall back to SWIG backend if IPC not available
if not USE_IPC_BACKEND and KICAD_BACKEND != "ipc":
    # SWIG backend selected.  pcbnew was already imported above (or set to None).
    # If the import failed, this is fatal — emit the same diagnostic the
    # original sys.exit(1) path produced, then exit.
    if pcbnew is not None:
        logger.info(f"Successfully imported pcbnew module from: {pcbnew.__file__}")
        # Deferred — GetBuildVersion() triggers 55-65 s wxApp init on macOS.
        # The _warmup handler pays this cost during startup (not on first tool call).
        logger.warning("Using SWIG backend - changes require manual reload in KiCAD UI")
    else:
        logger.error("Failed to import pcbnew module")
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
            "errorDetails": f"pcbnew module not on PYTHONPATH\n\n{help_message}\n\nPython sys.path:\n{chr(10).join(sys.path)}",
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
    # Some names look unused but are intentional: this block fail-fast-validates
    # every command module at import, and exposes classes as kicad_interface.<Name>
    # attributes that tests mock.patch (SchematicManager, ConnectionManager). Keep.
    from commands.board import BoardCommands
    from commands.component import ComponentCommands
    from commands.component_schematic import ComponentManager
    from commands.connection_schematic import ConnectionManager  # noqa: F401
    from commands.datasheet_manager import DatasheetManager
    from commands.design_rules import DesignRuleCommands
    from commands.export import ExportCommands
    from commands.footprint import FootprintCreator
    from commands.freerouting import FreeroutingCommands
    from commands.jlcpcb_parts import JLCPCBPartsManager
    from commands.library import (
        LibraryCommands,
        get_library_manager,
    )
    from commands.library_schematic import LibraryManager as SchematicLibraryManager
    from commands.library_symbol import SymbolLibraryCommands, SymbolLibraryManager
    from commands.project import ProjectCommands
    from commands.routing import RoutingCommands
    from commands.schematic import SchematicManager  # noqa: F401
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


class KiCADInterface(BoardPersistenceMixin):
    """Main interface class to handle KiCAD operations"""

    # ----- Trampoline dispatch -----
    # _handle_<command>(params) calls go through __getattr__ below,
    # which looks up the command name in _HANDLER_MAP and forwards to
    # the appropriate function in python/handlers/<module>.py.
    # Adding a new handler module: add its mapping here.
    _HANDLER_MAP: "Dict[str, str]" = {
        "get_origin": "board_meta",
        "get_title_block_info": "board_meta",
        "set_origin": "board_meta",
        "set_title_block_info": "board_meta",
        "import_svg_logo": "board",
        "place_component": "board",
        "enrich_datasheets": "datasheet",
        "get_datasheet_url": "datasheet",
        "create_footprint": "footprint",
        "edit_footprint_pad": "footprint",
        "list_footprint_libraries": "footprint",
        "register_footprint_library": "footprint",
        "ipc_add_text": "ipc",
        "ipc_add_track": "ipc",
        "ipc_add_via": "ipc",
        "ipc_get_tracks": "ipc",
        "ipc_get_vias": "ipc",
        "ipc_list_components": "ipc",
        "ipc_save_board": "ipc",
        "download_jlcpcb_database": "jlcpcb",
        "download_jlcpcb_datasheet": "jlcpcb",
        "get_jlcpcb_database_stats": "jlcpcb",
        "get_jlcpcb_part": "jlcpcb",
        "import_jlcpcb_symbol": "jlcpcb",
        "import_jlcpcb_symbols": "jlcpcb",
        "search_jlcpcb_parts": "jlcpcb",
        "suggest_jlcpcb_alternatives": "jlcpcb",
        "create_project": "project",
        "open_project": "project",
        "snapshot_project": "project",
        "refill_zones": "routing",
        "add_schematic_component": "schematic_component",
        "annotate_schematic": "schematic_component",
        "delete_schematic_component": "schematic_component",
        "edit_schematic_component": "schematic_component",
        "get_schematic_component": "schematic_component",
        "move_schematic_component": "schematic_component",
        "refresh_schematic_lib_symbols": "schematic_component",
        "remove_schematic_component_property": "schematic_component",
        "rotate_schematic_component": "schematic_component",
        "set_schematic_component_property": "schematic_component",
        "create_schematic": "schematic_io",
        "export_netlist": "schematic_io",
        "export_schematic_pdf": "schematic_io",
        "export_schematic_svg": "schematic_io",
        "generate_netlist": "schematic_io",
        "load_schematic": "schematic_io",
        "run_erc": "schematic_io",
        "sync_schematic_to_board": "schematic_io",
        "add_schematic_text": "schematic_query",
        "check_wire_collisions": "schematic_query",
        "find_unconnected_pins": "schematic_query",
        "get_net_at_point": "schematic_query",
        "get_net_connections": "schematic_query",
        "get_schematic_pin_locations": "schematic_query",
        "get_wire_connections": "schematic_query",
        "list_schematic_components": "schematic_query",
        "list_schematic_labels": "schematic_query",
        "list_schematic_libraries": "schematic_query",
        "list_schematic_nets": "schematic_query",
        "list_schematic_texts": "schematic_query",
        "list_schematic_wires": "schematic_query",
        "find_orphaned_wires": "schematic_view",
        "find_overlapping_elements": "schematic_view",
        "find_wires_crossing_symbols": "schematic_view",
        "get_elements_in_region": "schematic_view",
        "get_schematic_view": "schematic_view",
        "get_schematic_view_region": "schematic_view",
        "list_floating_labels": "schematic_view",
        "snap_to_grid": "schematic_view",
        "add_no_connect": "schematic_wire",
        "add_schematic_hierarchical_label": "schematic_wire",
        "add_schematic_net_label": "schematic_wire",
        "add_schematic_sheet": "schematic_wire",
        "add_schematic_wire": "schematic_wire",
        "add_sheet_pin": "schematic_wire",
        "connect_passthrough": "schematic_wire",
        "connect_to_net": "schematic_wire",
        "delete_no_connect": "schematic_wire",
        "delete_schematic_net_label": "schematic_wire",
        "delete_schematic_wire": "schematic_wire",
        "edit_schematic_net_label": "schematic_wire",
        "move_schematic_net_label": "schematic_wire",
        "create_symbol": "symbol_creator",
        "delete_symbol": "symbol_creator",
        "list_symbols_in_library": "symbol_creator",
        "register_symbol_library": "symbol_creator",
        "check_kicad_ui": "ui",
        "get_backend_info": "ui",
        "get_backend_state": "ui",
        "launch_kicad_ui": "ui",
        "reconcile_backends": "ui",
        "run_action": "ui",
        "add_to_selection": "selection",
        "clear_selection": "selection",
        "get_selection": "selection",
        "hit_test": "selection",
        "interactive_move": "selection",
        "remove_from_selection": "selection",
        "add_arc": "shapes",
        "add_circle": "shapes",
        "add_polygon": "shapes",
        "add_rectangle": "shapes",
        "add_segment": "shapes",
        "begin_transaction": "transactions",
        "commit_transaction": "transactions",
        "get_transaction_status": "transactions",
        "rollback_transaction": "transactions",
        "get_schematic_overview": "overview",
        "get_pcb_overview": "overview",
    }

    def __getattr__(self, name: str):
        """Generate _handle_<command> and _ipc_<command> shims on demand.

        Every MCP tool used to have its own ~4-line trampoline method on
        KiCADInterface that imported the matching handlers/<module>.py and
        called handle_<command>(self, params).  80 of them added up to
        ~320 lines of boilerplate that drifted easily.  Replaced with a
        single dispatcher driven by _HANDLER_MAP.

        The same trampoline now also covers the IPC fast-path handlers
        that used to live inline as _ipc_<cmd> methods on this class.
        They now sit in handlers/ipc_fastpath.py as handle_<cmd>; the
        IPC_CAPABLE_COMMANDS dispatch in handle_command still references
        them by their old "_ipc_<cmd>" name so this shim keeps the dispatch
        site unchanged.

        The dispatcher preserves the call surface tests rely on:
            iface._handle_check_kicad_ui({})
            iface._ipc_place_component({...})
        still work exactly as before.
        """
        if name.startswith("_handle_"):
            cmd = name[len("_handle_") :]
            module_name = type(self)._HANDLER_MAP.get(cmd)
            if module_name is not None:
                from importlib import import_module

                module = import_module(f"handlers.{module_name}")
                handler = getattr(module, f"handle_{cmd}")
                # Return a callable bound to this iface instance so the
                # dispatch table can store the result of attribute access
                # the same way it stores bound methods.
                return lambda params, _h=handler: _h(self, params)
        if name.startswith("_ipc_"):
            cmd = name[len("_ipc_") :]
            from importlib import import_module

            module = import_module("handlers.ipc_fastpath")
            handler = getattr(module, f"handle_{cmd}", None)
            if handler is not None:
                return lambda params, _h=handler: _h(self, params)
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

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
        # Cross-backend conflict tracking.  The SWIG and IPC paths each
        # carry their own copy of the board (SWIG in-memory + on-disk file
        # vs. KiCad's UI memory accessed over IPC), and writes from one
        # silently invalidate the other.  These two flags let us refuse
        # cross-backend writes until the user reconciles them.
        self._ipc_writes_pending = False  # IPC mutated KiCad memory; disk stale
        self._swig_writes_landed = False  # SWIG wrote disk; KiCad memory stale
        self._ipc_change_callback_registered = False
        self.use_ipc = USE_IPC_BACKEND
        self.ipc_backend = ipc_backend
        # Typed Any: a kipy-backed BoardAPI when IPC is live, else None. The
        # backend is accessed dynamically across many handlers; Any keeps mypy
        # from forcing Optional-narrowing at every call site.
        self.ipc_board_api: Any = None

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

        # Initialize footprint library manager (process-wide cached instance)
        self.footprint_library = get_library_manager()

        # Initialize command handlers
        self.project_commands = ProjectCommands(self.board)
        self.board_commands = BoardCommands(self.board)
        self.component_commands = ComponentCommands(self.board, self.footprint_library)
        self.routing_commands = RoutingCommands(self.board)
        self.freerouting_commands = FreeroutingCommands(
            self.board, signature_callback=self._record_board_signature
        )
        self.design_rule_commands = DesignRuleCommands(self.board)
        self.export_commands = ExportCommands(self.board)
        self.library_commands = LibraryCommands(self.footprint_library)
        self._current_project_path: Optional[Path] = None  # set when boardPath is known

        # Initialize symbol library manager (for searching local KiCad symbol libraries)
        self.symbol_library_commands = SymbolLibraryCommands()

        # Initialize JLCPCB parts integration (public JLCSearch API, no auth required)
        from commands.jlcsearch import JLCSearchClient

        self.jlcsearch_client = JLCSearchClient()
        self.jlcpcb_parts = JLCPCBPartsManager()

        # Schematic-related classes don't need board reference
        # as they operate directly on schematic files

        # Command routing dictionary.
        #
        # Only commands that dispatch directly to a *_commands handler class
        # (no per-tool shim) are listed here.  Commands whose handler lives in
        # python/handlers/<module>.py are NOT listed — they're auto-injected
        # below from _HANDLER_MAP, which keeps the two sources of truth from
        # drifting apart.  Adding such a tool now means touching _HANDLER_MAP
        # plus the matching handlers/<module>.py, not three places.
        self.command_routes = {
            # Project commands
            "save_project": self.project_commands.save_project,
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
            "add_copper_pour": self._add_copper_pour_with_optional_refill,
            # ``add_zone`` is the same operation under a different MCP name
            # (the TS schema exposes both for historical reasons).  Route to
            # the shared SWIG impl so removing the schema later is a one-line
            # change and callers don't get an "Unknown command" surprise.
            "add_zone": self._add_copper_pour_with_optional_refill,
            "route_differential_pair": self.routing_commands.route_differential_pair,
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
            "export_position_file": self.export_commands.export_position_file,
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
            "refresh_symbol_libraries": self.symbol_library_commands.refresh_symbol_libraries,
            # Internal warm-up (pays wxApp init cost during startup).
            # _handle_warmup is a real method on this class, not synthesised
            # from _HANDLER_MAP, so it has to be registered explicitly.
            "_warmup": self._handle_warmup,
            # Freerouting autoroute commands
            "autoroute": self.freerouting_commands.autoroute,
            "export_dsn": self.freerouting_commands.export_dsn,
            "import_ses": self.freerouting_commands.import_ses,
            "check_freerouting": self.freerouting_commands.check_freerouting,
        }

        # Auto-inject handler-module dispatchers.  Each _HANDLER_MAP entry
        # points at a python/handlers/<module>.py whose handle_<cmd>(iface,
        # params) is reached via the __getattr__ trampoline above, so
        # ``getattr(self, "_handle_<cmd>")`` resolves to a bound handler.
        # setdefault means anything explicitly listed in command_routes
        # above wins, in case a future tool needs a custom override.
        for _cmd in self._HANDLER_MAP:
            self.command_routes.setdefault(_cmd, getattr(self, f"_handle_{_cmd}"))

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
        # MCP-name alias of add_copper_pour — both names go through the
        # same IPC fast-path so the schema isn't a dispatch-time landmine.
        "add_zone": "_ipc_add_copper_pour",
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
        "get_component_pads": "_ipc_get_component_pads",
        # Save command
        "save_project": "_ipc_save_project",
    }

    def _refresh_ipc_board_api(self) -> bool:
        """Refresh the IPC board API after KiCAD or a board becomes available."""
        ipc_backend = getattr(self, "ipc_backend", None)
        if not ipc_backend or not ipc_backend.is_connected():
            self.ipc_board_api = None
            return False

        try:
            self.ipc_board_api = ipc_backend.get_board()
        except Exception as e:
            logger.warning(f"Connected to KiCAD IPC, but no board API is available yet: {e}")
            self.ipc_board_api = None
            return False
        # Hook the dirty-tracker once per IPCBackend.  IPCBoardAPI forwards
        # every mutation through IPCBackend._notify_change, so a single
        # registration covers re-creations of the board API on reconnect.
        # ``__new__``-instantiated test interfaces skip __init__, so default
        # the flag via getattr.
        if not getattr(self, "_ipc_change_callback_registered", False):
            try:
                ipc_backend.register_change_callback(self._on_ipc_change)
                self._ipc_change_callback_registered = True
            except Exception as e:
                logger.debug(f"Could not register IPC change callback: {e}")
        return True

    def _on_ipc_change(self, change_type: str, details: Dict[str, Any]) -> None:
        """Track which side has unsaved/uncommitted writes for the conflict gate.

        IPCBoardAPI fires this for every mutation (component/track/zone/etc.)
        and for ``save`` events when KiCad writes to disk.  We use it to
        keep ``_ipc_writes_pending`` accurate so SWIG mutations can refuse
        to run on a stale disk (and lose the IPC changes when they save).
        """
        if change_type == "save":
            self._ipc_writes_pending = False
            # After IPC save, disk reflects KiCad memory — refresh the
            # signature so SWIG auto-save doesn't see it as 'changed
            # externally' and refuse legitimate follow-up writes.
            try:
                self._record_board_signature()
            except Exception:
                pass
            return
        # Selection state and action_invoked don't change board content; they
        # leave both backends consistent.
        if change_type in {
            "selection_cleared",
            "selection_added",
            "selection_removed",
            "action_invoked",
        }:
            return
        self._ipc_writes_pending = True

    def _cross_backend_conflict(self, *, attempting: str) -> Optional[Dict[str, Any]]:
        """Refuse cross-backend writes that would silently lose data.

        ``attempting`` is ``"ipc"`` (the caller is about to write through
        the IPC API) or ``"swig"`` (about to mutate the SWIG board).  When
        the other side has uncommitted writes the dispatcher returns a
        structured response with ``needs_reconcile: True`` and a concrete
        ``direction`` so agents can either call ``reconcile_backends`` or
        prompt the user with the manual recovery steps.

        The two cases:
        - SWIG wrote disk → KiCad memory is stale.  ``ipc_save_board``
          would overwrite the SWIG content with KiCad's old data; no IPC
          mutation can proceed safely.  Direction: ``swig_to_ipc``.
          ``reconcile_backends`` fixes this automatically via
          ``board.revert()`` (reload KiCad from disk), or the user can
          File → Revert manually.
        - IPC has unsaved changes → SWIG mutations would read stale disk
          and the auto-save would lose the IPC changes.  Direction:
          ``ipc_to_swig``.  ``reconcile_backends`` can do this
          automatically (ipc_save_board + SWIG reload).
        """
        if attempting == "ipc" and getattr(self, "_swig_writes_landed", False):
            return {
                "success": False,
                "needs_reconcile": True,
                "direction": "swig_to_ipc",
                "message": (
                    "SWIG wrote new content to disk that KiCad's in-memory "
                    "state doesn't include. Saving via IPC now would "
                    "overwrite those changes with KiCad's stale copy. Call "
                    "`reconcile_backends` (direction=swig_to_ipc) to reload "
                    "KiCad from disk (via board.revert()), or reload manually "
                    "in KiCad (File → Revert from saved); further IPC work is "
                    "safe after that."
                ),
            }
        if attempting == "swig" and getattr(self, "_ipc_writes_pending", False):
            return {
                "success": False,
                "needs_reconcile": True,
                "direction": "ipc_to_swig",
                "message": (
                    "IPC has unsaved changes in KiCad memory that the .kicad_pcb "
                    "file on disk doesn't include. A SWIG mutation here would "
                    "read the stale disk content and its auto-save would "
                    "overwrite the IPC changes. Call `reconcile_backends` "
                    "(direction=ipc_to_swig) to flush IPC to disk and reload "
                    "the SWIG board, then retry."
                ),
            }
        return None

    def _annotate_stale_vs_disk(self, result: Dict[str, Any]) -> None:
        """Flag an IPC read whose live KiCad-memory data is older than disk.

        When SWIG has landed writes to the .kicad_pcb that the running KiCad
        instance hasn't reloaded (``_swig_writes_landed``), an IPC query reads
        KiCad's stale in-memory board.  The read is safe — it can't lose data
        — so the gate lets it through; but returning a clean-looking value
        (e.g. ``componentCount: 0`` right after ``sync_schematic_to_board``
        wrote 359 footprints to disk) is a footgun.  Attach a targeted,
        on-demand hint — not a blanket backend banner — so the caller knows
        disk is ahead and how to resync.
        """
        result["staleVsDisk"] = True
        result["staleHint"] = (
            "Read from KiCad's live in-memory board, which is OLDER than the "
            ".kicad_pcb on disk: a SWIG-path write (e.g. sync_schematic_to_board) "
            "landed content KiCad hasn't reloaded. Call `reconcile_backends` "
            "(direction=swig_to_ipc) to reload KiCad from disk (via "
            "board.revert()), or reload manually in KiCad (File → Revert from "
            "saved)."
        )

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

    @staticmethod
    def _pcb_editor_gate_reason() -> str:
        return (
            "KiCAD has no .kicad_pcb document open over IPC. "
            "The MCP server does NOT auto-launch the PCB editor — ask the user "
            "to open the board in KiCAD (project manager → PCB icon, or open the "
            ".kicad_pcb file directly), wait for them to confirm it's open, then "
            "retry. Do not work around this by falling back to file-only edits."
        )

    def _pcb_editor_gate_response(self, command: Optional[str] = None) -> Dict[str, Any]:
        """Build a structured 'PCB editor not open' response for IPC board ops.

        Surfaced as ``success: False`` with ``needs_pcb_editor: True`` so an
        agent can detect this specific recoverable state and prompt the user
        instead of falling back to silent file-only mutations.  ``command`` is
        optional because handler-level gates don't always have the MCP command
        name in scope; ``handle_command`` passes it for a more pointed message.
        """
        label = f"'{command}'" if command else "This IPC board operation"
        return {
            "success": False,
            "needs_pcb_editor": True,
            "command": command,
            "message": f"{label} requires the PCB editor: " + self._pcb_editor_gate_reason(),
        }

    def _ipc_has_open_board_document(self) -> bool:
        """Whether KiCad has a ``.kicad_pcb`` document open via IPC.

        The pcbnew binary can be alive as a kiway worker even with no
        editor frame visible — ``KiCADProcessManager.is_pcb_editor_running()``
        then returns True and the old gate let calls through, but kipy
        had no document to act on so every board op silently returned
        empty data (``get_board_info`` → 0×0, ``move_component`` →
        'Failed to move component').

        Ask the IPC server directly: if no doc with a ``.kicad_pcb``
        path/type comes back, the editor isn't usefully open.
        """
        ipc_backend = getattr(self, "ipc_backend", None)
        if ipc_backend is None or not ipc_backend.is_connected():
            return False
        kicad = getattr(ipc_backend, "_kicad", None)
        if kicad is None:
            return False
        # kipy 10's get_open_documents(doc_type) REQUIRES the arg; the old
        # no-arg call raised TypeError, was swallowed here, and the gate
        # then claimed "no board open" even with the PCB editor open.
        from kicad_api.ipc_backend import has_open_pcb_document

        try:
            return has_open_pcb_document(kicad)
        except Exception as e:
            logger.debug(f"has_open_pcb_document failed: {e}")
            return False

    def require_ipc_board_op(self, *, allow_launch: bool = True) -> Dict[str, Any]:
        """Gate for handler-level IPC board ops.

        Returns one of four shapes:
          - ``{}`` when IPC, the PCB editor frame, and the cross-backend
            sync state are all clean.
          - The ``_pcb_editor_gate_response`` shape (``needs_pcb_editor: True``)
            when the editor frame is closed.  Detected via reason-string
            equality on the canonical gate text from ``ensure_ipc`` rather
            than re-probing the process list — that avoids both a TOCTOU
            race (editor opened/closed between the two checks) and the
            mis-classification where ``KICAD_BACKEND=swig`` plus a bare
            project manager was reported as 'open the PCB editor'.
          - The ``_cross_backend_conflict`` shape
            (``needs_reconcile: True``) when SWIG has landed writes that
            KiCad memory doesn't include — an IPC write would either fail
            (kipy) or, on save, silently overwrite the SWIG content.
          - ``{"success": False, "_ipc_reason": <raw reason>}`` for all
            other IPC-unavailable cases.  Handlers wrap that raw reason
            with their own domain-specific envelope so error messages
            don't end up doubly-prefixed.
        """
        ok, reason = self.ensure_ipc(allow_launch=allow_launch, require_pcb_editor=True)
        if not ok:
            if reason == self._pcb_editor_gate_reason():
                return self._pcb_editor_gate_response()
            return {"success": False, "_ipc_reason": reason}
        conflict = self._cross_backend_conflict(attempting="ipc")
        if conflict is not None:
            return conflict
        return {}

    def ensure_ipc(
        self,
        *,
        allow_launch: bool = True,
        timeout_s: float = 30.0,
        require_pcb_editor: bool = True,
    ) -> Tuple[bool, str]:
        """Make IPC available for the calling handler, auto-launching KiCAD if needed.

        Sequence:
          1. Already connected → return immediately.
          2. KiCAD is running but we never connected → try to attach.
          3. KiCAD not running and ``allow_launch`` (gated by KICAD_AUTO_LAUNCH
             ≠ "false") → launch the project manager and poll for the socket.

        With ``require_pcb_editor=True`` (the default for board-level handlers)
        a connected IPC is still rejected when ``pcbnew`` isn't a running
        process, because the project manager hosts the IPC server on its own
        and board mutations against that bare server fail cryptically or
        silently mutate a closed document.  Frame-agnostic callers like
        ``run_action`` opt out via ``require_pcb_editor=False``.

        Returns ``(True, "")`` on success, ``(False, reason)`` otherwise. The
        reason text is meant to be surfaced to the agent so it can decide
        whether to retry or fall back.
        """
        if KICAD_BACKEND == "swig":
            return (False, "KICAD_BACKEND=swig; IPC is disabled by configuration")

        def _connected() -> bool:
            return bool(self.use_ipc and self.ipc_board_api)

        def _check_editor_gate() -> Optional[Tuple[bool, str]]:
            if not require_pcb_editor:
                return None
            # Ground truth: kipy.get_open_documents() must list a .kicad_pcb
            # doc.  The process-existence proxy lies when pcbnew is alive
            # as a kiway worker without a board loaded.
            if self._ipc_has_open_board_document():
                return None
            return (False, self._pcb_editor_gate_reason())

        # Already connected?
        if _connected():
            return _check_editor_gate() or (True, "")
        if self._try_enable_ipc_backend(force=True):
            if _connected():
                return _check_editor_gate() or (True, "")

        # Honor KICAD_AUTO_LAUNCH=false as an explicit opt-out.
        env_optout = os.environ.get("KICAD_AUTO_LAUNCH", "").strip().lower() == "false"
        if not allow_launch or env_optout:
            if KiCADProcessManager.is_running():
                return (
                    False,
                    "KiCAD is running but the IPC API server is not reachable. "
                    "Enable it in Preferences > Plugins > Enable IPC API Server.",
                )
            return (
                False,
                "KiCAD is not running and auto-launch is disabled "
                "(KICAD_AUTO_LAUNCH=false). Start KiCAD manually or call launch_kicad_ui.",
            )

        # Launch KiCAD and poll for the socket.
        if not KiCADProcessManager.is_running():
            logger.info("Auto-launching KiCAD UI to satisfy IPC requirement")
            launched = KiCADProcessManager.launch(wait_for_start=True)
            if not launched:
                return (
                    False,
                    "KiCAD executable not found or failed to launch. "
                    "Install KiCAD or set its location on PATH.",
                )

        deadline = time.monotonic() + max(1.0, timeout_s)
        while time.monotonic() < deadline:
            if self._try_enable_ipc_backend(force=True):
                if _connected():
                    return _check_editor_gate() or (True, "")
            time.sleep(0.5)

        return (
            False,
            f"KiCAD launched but the IPC API server did not become reachable "
            f"within {int(timeout_s)}s. Open Preferences > Plugins > Enable "
            f"IPC API Server and try again.",
        )

    # Commands that require IPC; surfaced via get_backend_info so agents
    # can tell which tools are unavailable on the current backend without
    # trial-and-error.
    IPC_REQUIRED_COMMANDS: Tuple[str, ...] = (
        # Real-time IPC mutations
        "ipc_add_track",
        "ipc_add_via",
        "ipc_add_text",
        "ipc_list_components",
        "ipc_get_tracks",
        "ipc_get_vias",
        "ipc_save_board",
        # Board metadata (origins + title block)
        "get_origin",
        "set_origin",
        "get_title_block_info",
        "set_title_block_info",
        # IPC selection + transactions
        "get_selection",
        "clear_selection",
        "add_to_selection",
        "remove_from_selection",
        "hit_test",
        "begin_transaction",
        "commit_transaction",
        "rollback_transaction",
        "get_transaction_status",
        # Graphic primitives via kipy
        "add_segment",
        "add_arc",
        "add_circle",
        "add_rectangle",
        "add_polygon",
        # KiCad TOOL_ACTION dispatch
        "run_action",
    )

    def _backend_status(self) -> Dict[str, Any]:
        """Return backend status fields for command responses.

        Includes a ``capabilities`` snapshot and, on the SWIG backend, an
        ``unavailable_tool_count`` so agents see at a glance that some IPC-only
        tools are unreachable.  The full ``unavailable_tools`` list (~26 names)
        is large; only ``get_backend_info`` — the capability-enumeration tool —
        returns it.  Routine status calls (check_kicad_ui, get_backend_state,
        launch_kicad_ui) carry just the count to keep responses small.
        """
        ipc_backend = getattr(self, "ipc_backend", None)
        ipc_connected = ipc_backend.is_connected() if ipc_backend else False
        is_ipc = self.use_ipc and ipc_connected
        status = {
            "backend": "ipc" if is_ipc else "swig",
            "realtime_sync": is_ipc,
            "ipc_connected": ipc_connected,
            "capabilities": {
                "realtime_ui_sync": is_ipc,
                "transactions": is_ipc,
                "selection": is_ipc,
                "board_metadata": is_ipc,
                "run_action": is_ipc,
                "graphic_primitives": is_ipc,
            },
        }
        if not is_ipc:
            status["unavailable_tool_count"] = len(self.IPC_REQUIRED_COMMANDS)
        return status

    @staticmethod
    def _normalize_ipc_layer_name(layer: Any) -> str:
        """Convert KiCad IPC layer enum strings to common layer names."""
        layer_name = str(layer)
        if layer_name.startswith("BL_"):
            return layer_name[3:].replace("_", ".")
        return layer_name

    def handle_command(self, command: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Route command to appropriate handler, preferring IPC when available"""
        logger.info(f"Handling command: {command}")
        logger.debug(f"Command parameters: {params}")

        try:
            if command in self.IPC_CAPABLE_COMMANDS:
                self._try_enable_ipc_backend()

            # Footprint/symbol-library queries must see the open project's
            # fp-lib-table / sym-lib-table, not just the global one.  Scope the
            # manager to the current project (works in pure-IPC too — derives
            # the dir from the live board path).
            if command in self._FOOTPRINT_LIBRARY_COMMANDS:
                self._ensure_footprint_library_for_current_project()
            elif command in self._SYMBOL_LIBRARY_COMMANDS:
                self._ensure_symbol_library_for_current_project()

            # Check if we can use IPC for this command (real-time UI sync)
            if self.use_ipc and self.ipc_board_api and command in self.IPC_CAPABLE_COMMANDS:
                # IPC board ops are dispatched directly here (not through
                # ensure_ipc), so the editor-frame gate has to be enforced
                # at the dispatch site too — otherwise a project-manager-only
                # KiCAD would silently route mutations to a closed document.
                # The check is "is there a .kicad_pcb open via IPC", NOT
                # "is pcbnew a running process" — kicad may have pcbnew alive
                # as a kiway worker with no board, in which case the process
                # check falsely passes and every call returns empty data.
                if not self._ipc_has_open_board_document():
                    return self._pcb_editor_gate_response(command)

                # Cross-backend conflict: refuse IPC writes when SWIG has
                # landed content on disk that KiCad memory doesn't include
                # (an IPC save here would overwrite it).  Read-only queries
                # are safe to let through — but when SWIG has landed writes,
                # the live KiCad memory they read is stale vs disk, so flag
                # the result instead of returning a clean-looking value.
                read_is_stale_vs_disk = False
                if command not in self._IPC_READ_ONLY_COMMANDS:
                    conflict = self._cross_backend_conflict(attempting="ipc")
                    if conflict is not None:
                        return conflict
                elif getattr(self, "_swig_writes_landed", False):
                    read_is_stale_vs_disk = True

                ipc_handler_name = self.IPC_CAPABLE_COMMANDS[command]
                ipc_handler = getattr(self, ipc_handler_name, None)

                if ipc_handler:
                    logger.info(f"Using IPC backend for {command} (real-time sync)")
                    result = ipc_handler(params)
                    logger.debug(f"IPC command result: {result}")
                    if read_is_stale_vs_disk and isinstance(result, dict):
                        self._annotate_stale_vs_disk(result)
                    return result

            # Fall back to SWIG-based handler
            if self.use_ipc and command in self.IPC_CAPABLE_COMMANDS:
                logger.warning(
                    f"IPC handler not available for {command}, falling back to SWIG (deprecated)"
                )

            # Get the handler for the command
            handler = self.command_routes.get(command)

            # Cross-backend conflict for SWIG mutations: refuse when IPC has
            # unsaved changes in KiCad memory.  SWIG reads the on-disk file
            # (which doesn't include them) and the auto-save would write
            # back, losing the IPC changes.  Reads + project lifecycle
            # commands fall through; only mutating board ops are gated.
            if handler is not None and command in self._BOARD_MUTATING_COMMANDS:
                conflict = self._cross_backend_conflict(attempting="swig")
                if conflict is not None:
                    return conflict

            if handler:
                # Execute the command
                result = handler(params)
                logger.debug(f"Command result: {result}")

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
                                }
                        self._update_command_handlers()
                        # Record the file's signature so subsequent auto-saves
                        # can detect external modifications and refuse to
                        # overwrite them.
                        self._record_board_signature()
                        self._last_auto_save_status = None
                        # Fresh load → both SWIG and disk are in sync.  The
                        # IPC side is left alone: it might still have
                        # pending changes if KiCad held them through the
                        # reload, and we can't assume otherwise from here.
                        self._swig_writes_landed = False
                    elif command == "save_project":
                        self._record_board_signature()
                        self._last_auto_save_status = None
                        # SWIG save writes the in-memory board to disk;
                        # KiCad memory now diverges from disk if KiCad has
                        # the file open.
                        self._swig_writes_landed = True
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
                        if save_status.get("saved"):
                            # SWIG just landed content on disk; mark the
                            # SWIG→IPC direction dirty so any later IPC
                            # write is gated until KiCad reloads the file.
                            self._swig_writes_landed = True

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
        "add_zone",
        "refill_zones",
        "import_svg_logo",
        "sync_schematic_to_board",
        "connect_passthrough",
        "connect_to_net",
    }

    # IPC commands that only read the board.  Used by the cross-backend
    # conflict gate to let queries through even when SWIG has landed
    # writes that haven't been picked up by KiCad memory — reads can't
    # cause data loss.
    _IPC_READ_ONLY_COMMANDS = frozenset(
        {
            # IPC fast-path queries (subset of IPC_CAPABLE_COMMANDS).
            "query_traces",
            "get_nets_list",
            "get_board_info",
            "get_layer_list",
            "get_component_list",
            "get_component_properties",
            "get_component_pads",
            # Direct IPC queries (handlers/ipc.py).
            "ipc_list_components",
            "ipc_get_tracks",
            "ipc_get_vias",
            # board_meta / selection / transactions queries.
            "get_origin",
            "get_title_block_info",
            "get_selection",
            "hit_test",
            "get_transaction_status",
        }
    )

    def _add_copper_pour_with_optional_refill(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add a copper pour, then optionally refill so gerber export captures it.

        Without refill, the .gbr layer file is generated from an unfilled zone
        and looks blank when fab houses load it — a footgun the user hit in
        practice. autoRefill defaults to True; pass False to keep the legacy
        deferred-fill behaviour (suitable for batch zone setup followed by a
        single explicit refill_zones at the end).
        """
        auto_refill = bool(params.get("autoRefill", True))
        # The routing command itself doesn't know about autoRefill; strip
        # the key before forwarding to avoid spurious "unknown param" noise.
        passthrough = {k: v for k, v in params.items() if k != "autoRefill"}
        result = self.routing_commands.add_copper_pour(passthrough)
        if not result.get("success") or not auto_refill:
            if not auto_refill:
                result["refillStatus"] = (
                    "deferred — zone defined but not filled; "
                    "call refill_zones before export_gerber"
                )
            return result

        # Lazy import to avoid a hard handler-module import cycle.
        from handlers.routing import handle_refill_zones

        # Refill in the existing subprocess-isolated path so a SWIG SIGSEGV
        # can't kill the whole MCP process.
        refill_result = handle_refill_zones(self, {})
        if refill_result.get("success"):
            result["refillStatus"] = "filled"
            result["zoneCount"] = refill_result.get("zoneCount")
        else:
            result.setdefault("warnings", []).append(
                f"Auto-refill failed: {refill_result.get('message', 'unknown')}. "
                f"Zones are defined and will fill when opened in KiCAD (press B)."
            )
            result["refillStatus"] = "deferred_after_failure"
        return result

    def _current_board_path(self) -> Optional[str]:
        """Return the current board file path, if a healthy board is loaded.

        Two backends, two ways to ask the question:

          - SWIG mode: ``self.board.GetFileName()``.
          - IPC mode: ``self.ipc_board_api`` wraps a kipy Board whose
            ``document.board_filename`` carries the path of whatever the
            user has open in the KiCAD UI.  Without this branch
            ``get_backend_state`` would report ``loadedBoard: false`` even
            while a board is plainly open and reachable over IPC.
        """
        # SWIG path
        board = getattr(self, "board", None)
        if board and self._is_board_healthy(board):
            try:
                path = board.GetFileName()
            except Exception:
                path = None
            if path:
                return os.path.abspath(path)

        # IPC path — only meaningful when use_ipc is True and a board API
        # is connected.  IPCBoardAPI wraps a kipy Board; we want the full
        # absolute path, but kipy returns it in two parts:
        #
        #   document.board_filename   "mcp_smoke_test.kicad_pcb"     ← bare name
        #   document.project.path     "/home/.../mcp-pcb-test"        ← directory
        #
        # Stitch them together.  Falling back to os.path.abspath(filename)
        # would resolve against the MCP server's cwd, which is the kicad-mcp
        # checkout, not the user's project directory — that bug surfaced
        # during end-to-end MCP testing and was the reason get_backend_state
        # reported a project under the kicad-mcp repo instead of ~/Desktop/...
        ipc_api = getattr(self, "ipc_board_api", None)
        if ipc_api is not None:
            try:
                board = ipc_api._get_board()  # noqa: SLF001 — private accessor on our own wrapper
                doc = getattr(board, "document", None)
                filename = getattr(doc, "board_filename", None) if doc is not None else None
                if filename:
                    project = getattr(doc, "project", None) if doc is not None else None
                    project_dir = getattr(project, "path", None) if project is not None else None
                    if project_dir and not os.path.isabs(filename):
                        return os.path.abspath(os.path.join(project_dir, filename))
                    return os.path.abspath(filename)
            except Exception:
                pass

        return None

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

    # Footprint-library query commands whose manager must be scoped to the open
    # project (so project ``fp-lib-table`` entries — typically
    # ``${KIPRJMOD}/*.pretty`` — are visible, not just the global table).
    _FOOTPRINT_LIBRARY_COMMANDS = frozenset(
        {
            "list_libraries",
            "list_library_footprints",
            "search_footprints",
            "get_footprint_info",
        }
    )

    # Symbol-library query commands whose manager must be scoped to the open
    # project (so project ``sym-lib-table`` entries are visible).  The handlers
    # already scope from caller params; the dispatch hook adds the pure-IPC,
    # no-param, no-open_project case via the live board path.
    _SYMBOL_LIBRARY_COMMANDS = frozenset(
        {
            "list_symbol_libraries",
            "search_symbols",
            "list_library_symbols",
            "get_symbol_info",
        }
    )

    def _refresh_footprint_library_for_project(self, project_path: Optional[Path]) -> None:
        """Re-scope the footprint library manager to ``project_path`` so the
        project ``fp-lib-table`` is visible to list_libraries /
        list_library_footprints / search_footprints / get_footprint_info — and
        to ``place_component`` (SWIG), which resolves footprints by name through
        the same manager.

        The footprint side mirrors :meth:`_refresh_symbol_library_for_project`
        — without it ``LibraryCommands`` stays pinned to the global-only manager
        built at startup and project-registered ``.pretty`` libs read back as
        empty.  ``get_library_manager`` is process-cached + mtime-invalidated,
        so re-pointing here is cheap.  Every holder of the startup global-only
        manager is re-pointed so the project scope is coherent across them (a
        project-scoped manager is a superset of global, so this never drops a
        global lib).
        """
        if project_path is None:
            return
        try:
            from commands.library import get_library_manager

            manager = get_library_manager(project_path=Path(project_path))
            self.footprint_library = manager
            self.library_commands.library_manager = manager
            # component_commands.place_component resolves footprints via the same
            # manager; keep it in lock-step or project footprints would be
            # listable but not placeable.
            component_commands = getattr(self, "component_commands", None)
            if component_commands is not None:
                component_commands.library_manager = manager
        except Exception as e:
            logger.warning(f"Failed to refresh footprint library for project {project_path}: {e}")

    def _project_dir_for_library_scope(self) -> Optional[Path]:
        """Best-effort project directory for scoping the symbol/footprint library
        managers: the explicitly-opened project if known, else the directory of
        the live board.  The board-path fallback is what covers pure-IPC use,
        where ``open_project`` never ran through the MCP but KiCad has the board
        open and reachable over IPC.
        """
        project_dir = getattr(self, "_current_project_path", None)
        if project_dir is not None:
            return Path(project_dir)
        board_path = self._current_board_path()
        if board_path:
            return Path(board_path).parent
        return None

    def _ensure_footprint_library_for_current_project(self) -> None:
        """Scope the footprint library manager to the open project before a
        footprint-library query.

        open_project / create_project already call
        :meth:`_refresh_footprint_library_for_project`, but in pure-IPC use the
        project dir is only knowable from the live board path — derive it so the
        project's ``.pretty`` libs are still visible.
        """
        project_dir = self._project_dir_for_library_scope()
        if project_dir is not None:
            self._refresh_footprint_library_for_project(project_dir)

    def _ensure_symbol_library_for_current_project(self) -> None:
        """Scope the symbol library manager to the open project before a
        symbol-library query — the pure-IPC counterpart to the params-based
        :meth:`SymbolLibraryCommands._ensure_manager_for`.

        The symbol handlers already derive a project from caller params
        (projectPath / schematicPath / boardPath); this fills the gap where
        none was passed and ``open_project`` never ran, deriving the project
        from the live board path instead.  ``use_project`` is idempotent
        (no-op when already scoped), so calling it per query is cheap, and it
        composes with the handler's own params-based scoping (an explicit param
        path still wins, running after this).
        """
        project_dir = self._project_dir_for_library_scope()
        if project_dir is None:
            return
        try:
            self.symbol_library_commands.use_project(project_dir)
        except Exception as e:
            logger.warning(f"Failed to scope symbol library to project {project_dir}: {e}")

    # Project handlers live in handlers/project.py.
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

    # Footprint + symbol-creator handlers live in handlers/footprint.py and
    # handlers/symbol_creator.py.

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

            # Power-PORT symbols (#PWR): Value property IS the net name
            # ("+3V3", "GND", ...); use pin 1 pos.  Power-FLAG symbols
            # (#FLG) are intentionally skipped — their Value is the
            # literal "PWR_FLAG" which is an ERC marker, NOT a net.
            # Including #FLG used to leak "PWR_FLAG" into ``all_net_names``
            # and ``board.GetNetInfo()`` after ``sync_schematic_to_board``,
            # producing a confusing fake net on every pad list / DRC run.
            # The flag's wire is still electrically traced because the
            # wire-graph BFS below propagates the real net name (from a
            # neighboring #PWR or label) across the flag's anchor point.
            for sym in getattr(sch, "symbol", None) or []:
                try:
                    ref = sym.property.Reference.value
                    if not ref.startswith("#PWR"):
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

    # Grid layout constants for _add_missing_footprints_from_schematic.
    # Pitch defaults assume small SMD packages but adapt up to the largest
    # loaded module's bbox; start offset keeps the grid visually distinct
    # from the (0, 0) page-origin marker.  Units are pcbnew internal nm.
    _NEW_FOOTPRINT_GRID_MIN_PITCH_NM = 15_000_000  # 15 mm floor cell pitch
    _NEW_FOOTPRINT_GRID_PADDING_NM = 5_000_000  # 5 mm gap between cells
    _NEW_FOOTPRINT_GRID_START_NM = 10_000_000  # 10 mm from page origin
    _NEW_FOOTPRINT_GRID_GUTTER_NM = 20_000_000  # 20 mm gap past existing FPs

    @staticmethod
    def _footprint_right_edge_nm(fp: Any) -> Optional[int]:
        """Bounding-box right edge of a footprint in nm, or None on failure.

        Falls back to ``GetPosition().x`` only when ``GetBoundingBox()``
        isn't available (very old SWIG bindings or a corrupt proxy) —
        otherwise we'd miss the case where a large IC anchored at e.g.
        x=50 mm extends 10 mm further right.
        """
        try:
            bbox = fp.GetBoundingBox()
            return int(bbox.GetRight())
        except Exception:
            try:
                return int(fp.GetPosition().x)
            except Exception:
                return None

    def _grid_origin_for_new_footprints(self, existing_fps: List[Any]) -> Tuple[int, int]:
        """Pick a safe (x_nm, y_nm) origin for grid-placing new footprints.

        Empty board → fixed offset from (0, 0).  Otherwise we start
        20 mm past the *rightmost edge of any existing footprint's
        bounding box* (not its anchor — a wide IC anchored at x=50 mm
        whose body extends to x=80 mm would otherwise be overlapped by
        the new grid at x=70 mm).
        """
        if not existing_fps:
            return (
                self._NEW_FOOTPRINT_GRID_START_NM,
                self._NEW_FOOTPRINT_GRID_START_NM,
            )
        max_right: Optional[int] = None
        for fp in existing_fps:
            right = self._footprint_right_edge_nm(fp)
            if right is None:
                continue
            # No floor: an all-negative-X cluster must still produce
            # "20 mm past the rightmost edge" rather than snapping to
            # the page-origin offset.  Treat the *first* reading as the
            # initial max so negative coordinates are honored.
            if max_right is None or right > max_right:
                max_right = right
        if max_right is None:
            # Every footprint failed to report a bbox or position —
            # fall back to the empty-board contract rather than (0, 0).
            return (
                self._NEW_FOOTPRINT_GRID_START_NM,
                self._NEW_FOOTPRINT_GRID_START_NM,
            )
        return (
            max_right + self._NEW_FOOTPRINT_GRID_GUTTER_NM,
            self._NEW_FOOTPRINT_GRID_START_NM,
        )

    def _grid_spacing_for_modules(self, modules: List[Any]) -> Tuple[int, int]:
        """Choose grid cell pitch from the largest loaded module's bbox.

        Hard-coded 15 mm pitch works for small SMD parts but causes
        outright overlap for QFP/BGA/large connectors whose body
        extends past one cell.  We measure each loaded module's bbox
        and use max(min_pitch, largest_bbox + padding) per axis so the
        grid scales with the worst-case footprint.
        """
        max_w = self._NEW_FOOTPRINT_GRID_MIN_PITCH_NM
        max_h = self._NEW_FOOTPRINT_GRID_MIN_PITCH_NM
        for module in modules:
            try:
                bbox = module.GetBoundingBox()
                w = int(bbox.GetWidth()) + self._NEW_FOOTPRINT_GRID_PADDING_NM
                h = int(bbox.GetHeight()) + self._NEW_FOOTPRINT_GRID_PADDING_NM
            except Exception:
                continue
            if w > max_w:
                max_w = w
            if h > max_h:
                max_h = h
        return max_w, max_h

    def _add_missing_footprints_from_schematic(
        self, board: Any, schematic_path: str
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
        """Add footprints to ``board`` for any schematic component not yet present.

        New footprints are laid out in a roughly-square grid
        (columns = ``ceil(sqrt(N))``; cell pitch adapts to the largest
        loaded module's bounding box, minimum 15 mm) instead of stacked
        at the page origin — the old behavior dropped every newly-
        imported footprint at (0, 0), forcing the agent to manually
        move each one before any of them were visible.  Grid origin is
        (10 mm, 10 mm) for empty boards or 20 mm to the right of the
        existing footprints' bounding-box right edge when the board
        already has footprints.

        Power/flag references (``#PWR``, ``#FLG``) are skipped — they
        have no PCB representation.  Duplicate references in the
        extracted netlist (mis-annotated schematic, half-annotated
        symbols ``R?``) are also deduped against each other, not just
        against the board's existing footprints — without this guard a
        schematic with two ``R1`` rows would produce two ``R1``
        footprints on the PCB.

        Returns ``(added, skipped)``.  Each ``added`` entry includes the
        assigned ``position`` (in mm) so callers can surface the layout
        to the user.
        """
        import math
        from pathlib import Path

        from commands.library import get_library_manager

        added: List[Dict[str, Any]] = []
        skipped: List[Dict[str, str]] = []

        components = self._extract_components_from_schematic(schematic_path)
        if not components:
            return added, skipped

        # One pass through GetFootprints — SWIG iterators are usually OK
        # to re-iterate, but materialising once removes the doubt and
        # avoids a second walk to compute the grid origin.
        existing_fps = list(board.GetFootprints())
        existing_refs = {fp.GetReference() for fp in existing_fps}
        project_dir = Path(schematic_path).parent
        library_manager = get_library_manager(project_path=project_dir)

        # First pass: filter + load.  We don't position yet because the
        # column count of the grid depends on how many actually load
        # successfully (skip entries are removed from the count) AND
        # the cell pitch depends on the largest loaded bbox.
        to_place: List[Tuple[Any, Dict[str, str]]] = []
        for comp in components:
            ref = comp["reference"]
            fp_str = comp["footprint"]
            if not ref or ref.startswith("#"):
                # Power flags / global indicators — no PCB footprint expected.
                continue
            if ref in existing_refs:
                # Catches both refs already on the board AND duplicates
                # *within* the components list itself — see the docstring.
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

            to_place.append((module, comp))
            # Reserve the ref *now* so a later duplicate in the same
            # netlist hits the `if ref in existing_refs: continue`
            # branch above instead of being loaded again.
            existing_refs.add(ref)

        # Second pass: lay out in a grid and add to the board.
        if to_place:
            origin_x, origin_y = self._grid_origin_for_new_footprints(existing_fps)
            cols = max(1, int(math.ceil(math.sqrt(len(to_place)))))
            pitch_x, pitch_y = self._grid_spacing_for_modules([m for m, _ in to_place])
            for idx, (module, comp) in enumerate(to_place):
                ref = comp["reference"]
                lib_name, fp_name = comp["footprint"].split(":", 1)
                module.SetReference(ref)
                if comp["value"]:
                    module.SetValue(comp["value"])
                module.SetFPID(pcbnew.LIB_ID(lib_name, fp_name))
                x_nm = origin_x + (idx % cols) * pitch_x
                y_nm = origin_y + (idx // cols) * pitch_y
                module.SetPosition(pcbnew.VECTOR2I(x_nm, y_nm))

                board.Add(module)
                added.append(
                    {
                        "reference": ref,
                        "footprint": comp["footprint"],
                        "position": {
                            "x_mm": round(x_nm / 1_000_000, 3),
                            "y_mm": round(y_nm / 1_000_000, 3),
                        },
                    }
                )

        if added:
            logger.info(f"_add_missing_footprints_from_schematic: added {len(added)} footprints")
        return added, skipped

    # ===================================================================
    # Schematic analysis tools (read-only)
    # ===================================================================

    # IPC fast-path handlers (route_trace, place_component, …) — alternate
    # implementations that mutate via the live IPC API instead of the SWIG
    # pcbnew proxy — live in handlers/ipc_fastpath.py.  IPC_CAPABLE_COMMANDS
    # (above) references them by their historical ``_ipc_<cmd>`` names; the
    # __getattr__ trampoline resolves those to ``handle_<cmd>`` in that
    # module so the dispatch site in handle_command stays unchanged, and
    # tests that poke ``iface._ipc_<cmd>(...)`` directly keep working.
    #
    # Explicit ``ipc_*`` MCP commands (ipc_add_track, …) live in
    # handlers/ipc.py, and UI / backend-state handlers in handlers/ui.py —
    # both reached through the same _HANDLER_MAP + __getattr__ trampoline
    # used by every other tool.

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
    # IPC-specific handlers live in handlers/ipc.py.
    # JLCPCB API handlers

    # JLCPCB + datasheet handlers live in handlers/jlcpcb.py and
    # handlers/datasheet.py.


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
                    request_id = command_data.get("id")

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

                    # Echo the correlation id back when the client supplied one,
                    # so the TS bridge can drop a late response from a command it
                    # already timed out on instead of misattributing it to the
                    # next request.
                    if isinstance(response, dict) and request_id is not None:
                        response["id"] = request_id

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
