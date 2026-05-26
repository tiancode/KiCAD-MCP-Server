"""
Symbol-creator handlers — create / delete / list / register custom
symbols in .kicad_sym libraries.  Thin wrappers around
`commands.symbol_creator.SymbolCreator`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

from commands.symbol_creator import SymbolCreator

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


def handle_create_symbol(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new symbol in a .kicad_sym library."""
    logger.info(f"create_symbol: {params.get('name')} in {params.get('libraryPath')}")
    try:
        creator = SymbolCreator()
        return creator.create_symbol(
            library_path=params.get("libraryPath", ""),
            name=params.get("name", ""),
            reference_prefix=params.get("referencePrefix", "U"),
            description=params.get("description", ""),
            keywords=params.get("keywords", ""),
            datasheet=params.get("datasheet", "~"),
            footprint=params.get("footprint", ""),
            in_bom=params.get("inBom", True),
            on_board=params.get("onBoard", True),
            pins=params.get("pins", []),
            rectangles=params.get("rectangles", []),
            polylines=params.get("polylines", []),
            overwrite=params.get("overwrite", False),
        )
    except Exception as e:
        logger.error(f"create_symbol error: {e}")
        return {"success": False, "error": str(e)}


def handle_delete_symbol(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Delete a symbol from a .kicad_sym library."""
    logger.info(f"delete_symbol: {params.get('name')} from {params.get('libraryPath')}")
    try:
        creator = SymbolCreator()
        return creator.delete_symbol(
            library_path=params.get("libraryPath", ""),
            name=params.get("name", ""),
        )
    except Exception as e:
        logger.error(f"delete_symbol error: {e}")
        return {"success": False, "error": str(e)}


def handle_list_symbols_in_library(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """List all symbols in a .kicad_sym file."""
    logger.info(f"list_symbols_in_library: {params.get('libraryPath')}")
    try:
        creator = SymbolCreator()
        return creator.list_symbols(
            library_path=params.get("libraryPath", ""),
        )
    except Exception as e:
        logger.error(f"list_symbols_in_library error: {e}")
        return {"success": False, "error": str(e)}


def handle_register_symbol_library(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Register a .kicad_sym library in KiCAD's sym-lib-table."""
    logger.info(f"register_symbol_library: {params.get('libraryPath')}")
    try:
        creator = SymbolCreator()
        return creator.register_symbol_library(
            library_path=params.get("libraryPath", ""),
            library_name=params.get("libraryName"),
            description=params.get("description", ""),
            scope=params.get("scope", "project"),
            project_path=params.get("projectPath"),
        )
    except Exception as e:
        logger.error(f"register_symbol_library error: {e}")
        return {"success": False, "error": str(e)}
