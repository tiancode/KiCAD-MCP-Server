"""
JLCPCB parts-database handlers — download, search, get part info, stats,
suggest alternatives.  All depend on `iface.jlcpcb_parts` and
`iface.jlcsearch_client` which the KiCADInterface lifecycle owns.
"""

from __future__ import annotations

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
            parts, progress_callback=lambda _curr, _total, msg: logger.info(msg)
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
        mpn = params.get("mpn")
        in_stock = params.get("in_stock", True)
        limit = params.get("limit", 20)

        if library_type == "All":
            library_type = None

        result = iface.jlcpcb_parts.search_parts_meta(
            query=query,
            category=category,
            package=package,
            library_type=library_type,
            manufacturer=manufacturer,
            mpn=mpn,
            in_stock=in_stock,
            limit=limit,
        )
        parts = result["parts"]

        for part in parts:
            part["price_breaks"] = iface.jlcpcb_parts.normalize_price_breaks(part.get("price_json"))

        return {
            "success": True,
            "parts": parts,
            "count": len(parts),
            "match_mode": result.get("match_mode"),
            "fuzzy": result.get("fuzzy", False),
            "out_of_stock_only": result.get("out_of_stock_only", False),
            "warnings": result.get("warnings", []),
        }
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


def handle_download_jlcpcb_datasheet(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Download a JLCPCB/LCSC part's datasheet PDF to disk."""
    try:
        lcsc_number = params.get("lcsc_number") or params.get("lcsc")
        if not lcsc_number:
            return {"success": False, "message": "Missing lcsc_number parameter (e.g. C25804)"}

        return iface.jlcpcb_parts.download_datasheet(
            lcsc_number,
            output_dir=params.get("output_dir"),
            overwrite=bool(params.get("overwrite", False)),
        )
    except Exception as e:
        logger.error(f"Error downloading datasheet: {e}", exc_info=True)
        return {"success": False, "message": f"Failed to download datasheet: {str(e)}"}


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
            part["price_breaks"] = iface.jlcpcb_parts.normalize_price_breaks(part.get("price_json"))

        return {
            "success": True,
            "alternatives": alternatives,
            "reference_price": reference_price,
        }
    except Exception as e:
        logger.error(f"Error suggesting alternatives: {e}", exc_info=True)
        return {"success": False, "message": f"Failed to suggest alternatives: {str(e)}"}


def handle_import_jlcpcb_symbol(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Generate a KiCAD symbol + footprint for an LCSC/JLCPCB part.

    Closes the gap where the other JLCPCB tools only return database metadata:
    uses easyeda2kicad to fetch the part by LCSC number and writes a real
    .kicad_sym + .pretty into the shared cache library, registered under the
    nickname "easyeda" so add_schematic_component can place it. Does NOT need
    iface.jlcpcb_parts — it talks to EasyEDA directly via the LCSC id.
    """
    logger.info("Importing JLCPCB/LCSC symbol via easyeda2kicad")
    try:
        from commands import easyeda_import

        lcsc_number = params.get("lcsc_number") or params.get("lcsc")
        if not lcsc_number:
            return {"success": False, "message": "Missing lcsc_number parameter (e.g. C7593)"}
        overwrite = bool(params.get("forceRefresh", False))

        try:
            return easyeda_import.import_lcsc_part(lcsc_number, overwrite=overwrite)
        except (easyeda_import.EasyEdaImportError, ValueError) as e:
            return {"success": False, "message": str(e)}
    except Exception as e:
        import traceback

        logger.error(f"Error importing JLCPCB symbol: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"Failed to import symbol: {str(e)}",
            "errorDetails": traceback.format_exc(),
        }


def handle_import_jlcpcb_symbols(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Batch-import a list of LCSC/JLCPCB parts as KiCAD symbols + footprints.

    Pre-warms the shared "easyeda" cache for a whole BOM in one call so the
    symbols are ready before placement. Each id is imported independently —
    one bad id never aborts the rest, and already-cached parts are skipped
    without a network call.
    """
    logger.info("Batch-importing JLCPCB/LCSC symbols via easyeda2kicad")
    try:
        from commands import easyeda_import

        lcsc_numbers = params.get("lcsc_numbers") or params.get("lcsc")
        if isinstance(lcsc_numbers, str):
            lcsc_numbers = [lcsc_numbers]
        if not lcsc_numbers or not isinstance(lcsc_numbers, list):
            return {
                "success": False,
                "message": "Provide lcsc_numbers as a non-empty list (e.g. ['C7593', 'C12087'])",
            }
        overwrite = bool(params.get("forceRefresh", False))

        return easyeda_import.import_lcsc_parts(lcsc_numbers, overwrite=overwrite)
    except Exception as e:
        import traceback

        logger.error(f"Error batch-importing JLCPCB symbols: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"Failed to import symbols: {str(e)}",
            "errorDetails": traceback.format_exc(),
        }


def handle_check_bom_availability(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Check every BOM line of the loaded board against the local JLCPCB catalog.

    Groups footprints by (value, footprint), resolves each line by its LCSC
    field when present (exact) or by value+package search otherwise, and
    reports stock, unit price at the required quantity, and per-board cost.
    Requires the local parts database (run download_jlcpcb_database first).
    """
    logger.info("Checking BOM availability against JLCPCB catalog")
    try:
        from commands.bom_check import evaluate_bom_lines, group_bom

        if not iface.board:
            return {
                "success": False,
                "message": "No board is loaded",
                "errorDetails": "Load or create a board first",
            }

        stats = iface.jlcpcb_parts.get_database_stats()
        if not stats or not stats.get("total_components"):
            return {
                "success": False,
                "message": "Local JLCPCB parts database is empty",
                "hint": "Run download_jlcpcb_database first, then retry.",
            }

        board_qty = int(params.get("boardQty", 1))
        components = []
        for module in iface.board.GetFootprints():
            comp: Dict[str, Any] = {
                "reference": module.GetReference(),
                "value": module.GetValue(),
                "footprint": module.GetFPID().GetUniStringLibId(),
            }
            # LCSC part number lives in a footprint field (KiCad 8+) or a
            # legacy property, under a few common names.
            for field_name in ("LCSC", "LCSC Part", "LCSC Part #", "JLCPCB Part"):
                value = None
                if hasattr(module, "GetFieldByName"):
                    field = module.GetFieldByName(field_name)
                    if field is not None and hasattr(field, "GetText"):
                        value = field.GetText()
                if not value and hasattr(module, "GetPropertyNative"):
                    value = module.GetPropertyNative(field_name)
                if isinstance(value, str) and value.strip():
                    comp["lcsc"] = value.strip()
                    break
            components.append(comp)

        if not components:
            return {"success": False, "message": "Board has no footprints to check"}

        lines = group_bom(components)
        report = evaluate_bom_lines(
            lines,
            lookup_lcsc=iface.jlcpcb_parts.get_part_info,
            search=lambda **kw: iface.jlcpcb_parts.search_parts_meta(**kw),
            board_qty=board_qty,
        )
        return {"success": True, **report}
    except Exception as e:  # API boundary; bucket: catch + return
        logger.error(f"Error checking BOM availability: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"Failed to check BOM availability: {str(e)}",
        }
