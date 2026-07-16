"""Handler for set_symbol_pin_types — repair a symbol's pin electrical types.

Thin wrapper around ``commands.symbol_pin_types``. Picks the surface from the
params: a ``schematicPath`` targets the schematic's embedded lib_symbols copy
(ERC-visible immediately); otherwise a ``symbolId`` / ``libraryPath`` targets
the ``.kicad_sym`` source of truth.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from commands.symbol_pin_types import SymbolPinTypeError

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


def _resolve_library_path(
    library_id: str, project_path: Optional[str]
) -> "tuple[Optional[Path], str]":
    """Resolve a 'nickname:Name' lib id to a (.kicad_sym path, symbol name).

    Uses the sym-lib-table via DynamicSymbolLoader; falls back to the shared
    easyeda cache library for the 'easyeda' nickname when the table lookup
    fails (e.g. a freshly-imported part before the table is re-read).
    """
    nickname, _, symbol_name = library_id.partition(":")
    if not symbol_name:
        return None, library_id  # no colon — caller reports the error

    from commands.dynamic_symbol_loader import DynamicSymbolLoader

    loader = DynamicSymbolLoader(project_path=Path(project_path) if project_path else None)
    lib_path = loader.find_library_file(nickname)
    if lib_path is None and nickname == "easyeda":
        from commands import easyeda_import

        if easyeda_import.SYMBOL_LIB_PATH.exists():
            lib_path = easyeda_import.SYMBOL_LIB_PATH
    return lib_path, symbol_name


def handle_set_symbol_pin_types(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Rewrite the electrical types of pins on a library or embedded symbol."""
    logger.info("set_symbol_pin_types")
    try:
        from commands import symbol_pin_types as spt

        pin_types = params.get("pinTypes") or params.get("pin_types")
        if not isinstance(pin_types, dict) or not pin_types:
            return {
                "success": False,
                "message": "pinTypes must be a non-empty object mapping pin "
                "number-or-name to an electrical type, e.g. "
                '{"VDD": "power_in", "PA0": "bidirectional"}',
            }

        lookup = spt.normalize_mapping(pin_types)
        invalid = spt.invalid_types(lookup)
        if invalid:
            return {
                "success": False,
                "message": (
                    "Invalid electrical type(s): "
                    + ", ".join(f"{k}={v}" for k, v in sorted(invalid.items()))
                    + ". Valid types: "
                    + ", ".join(sorted(spt.VALID_PIN_TYPES))
                ),
                "invalid_types": invalid,
            }

        schematic_path = params.get("schematicPath") or params.get("schematic_path")
        symbol_id = params.get("symbolId") or params.get("libraryId") or params.get("lib_id")
        library_path = params.get("libraryPath") or params.get("library_path")
        symbol_name = params.get("symbolName") or params.get("symbol_name")
        reference = params.get("reference")
        project_path = params.get("projectPath") or params.get("project_path")

        # --- Schematic-embedded surface -----------------------------------
        if schematic_path:
            sch = Path(schematic_path)
            if not sch.exists():
                return {"success": False, "message": f"Schematic not found: {sch}"}
            lib_id = symbol_id
            if not lib_id and reference:
                content = sch.read_text(encoding="utf-8")
                lib_id = spt.find_reference_lib_id(content, reference)
                if not lib_id:
                    return {
                        "success": False,
                        "message": f"No placed component with reference {reference!r} "
                        f"found in {sch.name}",
                    }
            if not lib_id:
                return {
                    "success": False,
                    "message": "For a schematic edit, pass 'reference' (a placed "
                    "designator) or 'symbolId' (the full 'Library:Name' id).",
                }
            return spt.apply_to_schematic(sch, lib_id, lookup)

        # --- Library-file surface -----------------------------------------
        if library_path and symbol_name:
            return spt.apply_to_library(Path(library_path), str(symbol_name), lookup)

        if symbol_id:
            if ":" not in symbol_id:
                return {
                    "success": False,
                    "message": f"symbolId {symbol_id!r} must be 'Library:Name' "
                    "(e.g. 'easyeda:RDA5807M'). To target a file directly pass "
                    "libraryPath + symbolName.",
                }
            lib_path, sym_name = _resolve_library_path(symbol_id, project_path)
            if lib_path is None:
                nickname = symbol_id.split(":", 1)[0]
                return {
                    "success": False,
                    "message": f"Could not resolve library {nickname!r} to a "
                    ".kicad_sym file via the sym-lib-table. Pass libraryPath "
                    "explicitly, or register the library first.",
                }
            return spt.apply_to_library(lib_path, sym_name, lookup)

        return {
            "success": False,
            "message": "Nothing to target. Provide one of: schematicPath (+ "
            "reference or symbolId), symbolId ('Library:Name'), or libraryPath "
            "+ symbolName.",
        }
    except SymbolPinTypeError as e:
        return {"success": False, "message": str(e)}
    except Exception as e:  # API boundary
        import traceback

        logger.error(f"set_symbol_pin_types error: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"Failed to set pin types: {e}",
            "errorDetails": traceback.format_exc(),
        }
