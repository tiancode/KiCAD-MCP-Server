"""
Datasheet enrichment handlers — populate schematic Datasheet fields from
LCSC part numbers, fetch the canonical LCSC datasheet URL.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

from commands.datasheet_manager import DatasheetManager

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


def handle_enrich_datasheets(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich schematic Datasheet fields from LCSC numbers."""
    try:
        schematic_path = params.get("schematic_path")
        if not schematic_path:
            return {"success": False, "message": "Missing schematic_path parameter"}
        dry_run = params.get("dry_run", False)
        manager = DatasheetManager()
        return manager.enrich_schematic(Path(schematic_path), dry_run=dry_run)
    except Exception as e:
        logger.error(f"Error enriching datasheets: {e}", exc_info=True)
        return {"success": False, "message": f"Failed to enrich datasheets: {str(e)}"}


def handle_get_datasheet_url(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Return LCSC datasheet and product URLs for a part number."""
    try:
        lcsc = params.get("lcsc", "")
        if not lcsc:
            return {"success": False, "message": "Missing lcsc parameter"}
        manager = DatasheetManager()
        datasheet_url = manager.get_datasheet_url(lcsc)
        product_url = manager.get_product_url(lcsc)
        if not datasheet_url:
            return {"success": False, "message": f"Invalid LCSC number: {lcsc}"}
        norm = manager._normalize_lcsc(lcsc)
        return {
            "success": True,
            "lcsc": norm,
            "datasheet_url": datasheet_url,
            "product_url": product_url,
        }
    except Exception as e:
        logger.error(f"Error getting datasheet URL: {e}", exc_info=True)
        return {"success": False, "message": f"Failed to get datasheet URL: {str(e)}"}
