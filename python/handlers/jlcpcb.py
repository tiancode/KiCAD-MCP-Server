"""
JLCPCB parts-database handlers — download, search, get part info, stats,
suggest alternatives.  All depend on `iface.jlcpcb_parts` and
`iface.jlcsearch_client` which the KiCADInterface lifecycle owns.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


def handle_download_jlcpcb_database(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Download JLCPCB parts database from JLCSearch API."""
    try:
        force = params.get("force", False)

        stats = iface.jlcpcb_parts.get_database_stats()
        if stats["total_parts"] > 0 and not force:
            return {
                "success": False,
                "message": "Database already exists. Use force=true to re-download.",
                "stats": stats,
            }

        logger.info("Downloading JLCPCB parts database from JLCSearch...")

        # Download parts from JLCSearch public API (no auth required)
        parts = iface.jlcsearch_client.download_all_components(
            callback=lambda total, msg: logger.info(f"{msg}")
        )

        logger.info(f"Importing {len(parts)} parts into database...")
        iface.jlcpcb_parts.import_jlcsearch_parts(
            parts, progress_callback=lambda curr, total, msg: logger.info(msg)
        )

        stats = iface.jlcpcb_parts.get_database_stats()
        db_size_mb = os.path.getsize(iface.jlcpcb_parts.db_path) / (1024 * 1024)

        return {
            "success": True,
            "total_parts": stats["total_parts"],
            "basic_parts": stats["basic_parts"],
            "extended_parts": stats["extended_parts"],
            "db_size_mb": round(db_size_mb, 2),
            "db_path": stats["db_path"],
        }
    except Exception as e:
        logger.error(f"Error downloading JLCPCB database: {e}", exc_info=True)
        return {"success": False, "message": f"Failed to download database: {str(e)}"}


def handle_search_jlcpcb_parts(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Search JLCPCB parts database."""
    try:
        query = params.get("query")
        category = params.get("category")
        package = params.get("package")
        library_type = params.get("library_type", "All")
        manufacturer = params.get("manufacturer")
        in_stock = params.get("in_stock", True)
        limit = params.get("limit", 20)

        if library_type == "All":
            library_type = None

        parts = iface.jlcpcb_parts.search_parts(
            query=query,
            category=category,
            package=package,
            library_type=library_type,
            manufacturer=manufacturer,
            in_stock=in_stock,
            limit=limit,
        )

        for part in parts:
            if part.get("price_json"):
                try:
                    part["price_breaks"] = json.loads(part["price_json"])
                except json.JSONDecodeError:
                    part["price_breaks"] = []

        return {"success": True, "parts": parts, "count": len(parts)}
    except Exception as e:
        logger.error(f"Error searching JLCPCB parts: {e}", exc_info=True)
        return {"success": False, "message": f"Search failed: {str(e)}"}


def handle_get_jlcpcb_part(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Get detailed information for a specific JLCPCB part."""
    try:
        lcsc_number = params.get("lcsc_number")
        if not lcsc_number:
            return {"success": False, "message": "Missing lcsc_number parameter"}

        part = iface.jlcpcb_parts.get_part_info(lcsc_number)
        if not part:
            return {"success": False, "message": f"Part not found: {lcsc_number}"}

        footprints = iface.jlcpcb_parts.map_package_to_footprint(part.get("package", ""))
        return {"success": True, "part": part, "footprints": footprints}
    except Exception as e:
        logger.error(f"Error getting JLCPCB part: {e}", exc_info=True)
        return {"success": False, "message": f"Failed to get part info: {str(e)}"}


def handle_get_jlcpcb_database_stats(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Get statistics about JLCPCB database."""
    try:
        stats = iface.jlcpcb_parts.get_database_stats()
        return {"success": True, "stats": stats}
    except Exception as e:
        logger.error(f"Error getting database stats: {e}", exc_info=True)
        return {"success": False, "message": f"Failed to get stats: {str(e)}"}


def handle_suggest_jlcpcb_alternatives(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Suggest alternative JLCPCB parts."""
    try:
        lcsc_number = params.get("lcsc_number")
        limit = params.get("limit", 5)

        if not lcsc_number:
            return {"success": False, "message": "Missing lcsc_number parameter"}

        # Get original part for price comparison
        original_part = iface.jlcpcb_parts.get_part_info(lcsc_number)
        reference_price = None
        if original_part and original_part.get("price_breaks"):
            try:
                reference_price = float(original_part["price_breaks"][0].get("price", 0))
            except (ValueError, TypeError, KeyError, IndexError):
                pass

        alternatives = iface.jlcpcb_parts.suggest_alternatives(lcsc_number, limit)

        for part in alternatives:
            if part.get("price_json"):
                try:
                    part["price_breaks"] = json.loads(part["price_json"])
                except json.JSONDecodeError:
                    part["price_breaks"] = []

        return {
            "success": True,
            "alternatives": alternatives,
            "reference_price": reference_price,
        }
    except Exception as e:
        logger.error(f"Error suggesting alternatives: {e}", exc_info=True)
        return {"success": False, "message": f"Failed to suggest alternatives: {str(e)}"}
