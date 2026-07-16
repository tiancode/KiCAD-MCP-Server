"""Hierarchical-sheet authoring handlers.

Thin parameter-validation wrappers over commands/hierarchy_sheet.py, which
owns the s-expression manipulation (and its own refusal messages).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger("kicad_interface")


def handle_create_hierarchical_sheet(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Create a hierarchical sheet in a parent schematic (and its child file)."""
    logger.info("Creating hierarchical sheet")
    try:
        from commands.hierarchy_sheet import add_sheet_pin, create_hierarchical_sheet

        schematic_path = params.get("schematicPath")
        sheet_name = params.get("sheetName")
        child_filename = params.get("childFilename")
        if not schematic_path or not sheet_name or not child_filename:
            return {
                "success": False,
                "message": "schematicPath, sheetName and childFilename are required",
            }
        position = params.get("position") or {}
        size = params.get("size") or {}
        result = create_hierarchical_sheet(
            schematic_path,
            sheet_name=sheet_name,
            child_filename=child_filename,
            position=(float(position.get("x", 100.0)), float(position.get("y", 50.0))),
            size=(float(size.get("width", 50.0)), float(size.get("height", 40.0))),
            create_child=bool(params.get("createChild", True)),
        )
        if not result.get("success"):
            return result

        # Optional one-call pin authoring: auto-stacked sheet pins with the
        # matching hierarchical label written into the child schematic.
        # (Positioned pin placement without child labels stays with the
        # existing add_sheet_pin tool.)
        pins_created = []
        pin_errors = []
        for pin in params.get("pins") or []:
            pin_result = add_sheet_pin(
                schematic_path,
                sheet_name=sheet_name,
                pin_name=pin.get("name", ""),
                shape=pin.get("shape", "bidirectional"),
                side=pin.get("side", "left"),
                add_child_label=bool(pin.get("addChildLabel", True)),
            )
            if pin_result.get("success"):
                pins_created.append(pin_result.get("pin"))
            else:
                pin_errors.append({"pin": pin.get("name"), "message": pin_result.get("message")})
        if pins_created:
            result["pins"] = pins_created
        if pin_errors:
            result["pinErrors"] = pin_errors
            # A8: the documented auto-pin feature failed for at least one pin, so
            # the call did NOT do what was asked — report success:false with a
            # dedicated errorCode while preserving partial info (the sheet box was
            # still inserted, and any pins that did succeed are listed).
            result["success"] = False
            result["errorCode"] = "SHEET_PINS_FAILED"
            detail = "; ".join(f"{e.get('pin')}: {e.get('message')}" for e in pin_errors)
            result["message"] = (
                f"Sheet '{sheet_name}' was created, but "
                f"{len(pin_errors)} of {len(pin_errors) + len(pins_created)} requested "
                f"sheet pin(s) could not be added: {detail}"
            )
        return result
    except Exception as e:  # API boundary; bucket: catch + return
        logger.error(f"Error creating hierarchical sheet: {e}", exc_info=True)
        return {"success": False, "message": f"Failed to create hierarchical sheet: {e}"}
