"""Sheet-pin and sub-sheet handlers.

Split out of the former handlers/schematic_wire.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.schematic_wire")


def handle_add_sheet_pin(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Add a sheet pin to a sheet block on the parent schematic."""
    logger.info("Adding sheet pin to schematic")
    try:
        from commands.wire_manager import WireManager

        schematic_path = params.get("schematicPath")
        sheet_name = params.get("sheetName")
        pin_name = params.get("pinName")
        # A9: `shape` is the canonical field (aligns with create_hierarchical_sheet
        # and add_schematic_hierarchical_label); `pinType` stays as a deprecated
        # alias. Enum breadth matches the sibling tools (5 KiCad sheet-pin shapes).
        pin_type = params.get("shape") or params.get("pinType") or "bidirectional"
        position = params.get("position")
        orientation = params.get("orientation", 0)

        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}
        if not sheet_name:
            return {"success": False, "message": "sheetName is required"}
        if not pin_name:
            return {"success": False, "message": "pinName is required"}
        if not position or len(position) != 2:
            return {"success": False, "message": "position [x, y] is required"}
        _valid_shapes = ("input", "output", "bidirectional", "tri_state", "passive")
        if pin_type not in _valid_shapes:
            return {
                "success": False,
                "message": f"shape must be one of: {', '.join(_valid_shapes)}",
            }

        sch_file = Path(schematic_path)
        if not sch_file.exists():
            return {
                "success": False,
                "message": f"Schematic not found: {schematic_path}",
            }

        with open(sch_file, "r", encoding="utf-8") as f:
            content = f.read()

        modified, success = WireManager.add_sheet_pin(
            content,
            sheet_name,
            pin_name,
            pin_type,
            position,
            orientation=orientation,
        )

        if not success:
            return {
                "success": False,
                "message": f"Sheet '{sheet_name}' not found in {schematic_path}",
            }

        with open(sch_file, "w", encoding="utf-8") as f:
            f.write(modified)

        return {
            "success": True,
            "message": (f"Added sheet pin '{pin_name}' ({pin_type}) " f"to sheet '{sheet_name}'"),
        }

    except Exception as e:
        logger.error(f"Error adding sheet pin: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_add_schematic_sheet(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Create a hierarchical sheet box on the parent schematic referencing a sub-sheet."""
    logger.info("Adding hierarchical sheet box to schematic")
    try:
        from commands.schematic import SchematicManager
        from commands.wire_manager import WireManager

        schematic_path = params.get("schematicPath")
        sheet_name = params.get("sheetName")
        sheet_file = params.get("sheetFile")
        position = params.get("position")
        size = params.get("size")
        page_number = params.get("pageNumber")
        create_sub_sheet = params.get("createSubSheet", True)

        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}
        if not sheet_name:
            return {"success": False, "message": "sheetName is required"}
        if not sheet_file:
            return {"success": False, "message": "sheetFile is required"}
        if not position or len(position) != 2:
            return {"success": False, "message": "position [x, y] is required"}
        if size is not None and len(size) != 2:
            return {"success": False, "message": "size must be [width, height]"}

        parent = Path(schematic_path)
        if not parent.exists():
            return {"success": False, "message": f"Parent schematic not found: {schematic_path}"}

        # Normalize the Sheetfile to a path relative to the parent's directory —
        # that is what _discover_sub_sheets and kicad-cli resolve against.
        parent_dir = parent.parent
        sf = Path(sheet_file)
        if sf.is_absolute():
            try:
                rel_sheet_file = str(sf.relative_to(parent_dir))
            except ValueError:
                rel_sheet_file = sf.name
            sub_sheet_abspath = sf
        else:
            rel_sheet_file = sheet_file
            sub_sheet_abspath = parent_dir / sheet_file

        created_sub_sheet = False
        if create_sub_sheet and not sub_sheet_abspath.exists():
            # Use the genuinely-empty template — template_with_symbols carries
            # offscreen _TEMPLATE_* placeholder instances that would otherwise
            # show up as phantom LED/C/R parts in the sub-sheet's ERC and BOM.
            SchematicManager.create_schematic(
                sub_sheet_abspath.stem,
                path=str(sub_sheet_abspath.parent),
                template="empty.kicad_sch",
            )
            created_sub_sheet = True

        success, info = WireManager.add_sheet(
            parent,
            sheet_name,
            rel_sheet_file,
            position,
            size=size,
            page_number=str(page_number) if page_number is not None else None,
        )

        if not success:
            return {
                "success": False,
                "message": f"Failed to add sheet: {info.get('error', 'unknown error')}",
            }

        return {
            "success": True,
            "message": (
                f"Added sheet '{sheet_name}' -> {rel_sheet_file} (page {info.get('page')})"
            ),
            "uuid": info.get("uuid"),
            "page": info.get("page"),
            "sheetFile": rel_sheet_file,
            "createdSubSheet": created_sub_sheet,
        }

    except Exception as e:
        logger.error(f"Error adding sheet: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}
