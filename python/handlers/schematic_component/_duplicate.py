"""duplicate_schematic_component handler (S13).

Clone an existing placed schematic symbol — same lib symbol, value, footprint,
custom properties (LCSC/MPN/…), and unit structure — at an offset or explicit
position, auto-assigning the next free reference of the same prefix.

Reuses the existing placement code path (``handle_add_schematic_component`` —
which carries the wave-1 footprint inheritance, grid snap, multi-unit and
page-awareness handling) plus ``handle_edit_schematic_component`` for copying
custom properties, rather than re-implementing symbol injection.

See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List

from commands.schematic_locks import serialize_on_param

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.schematic_component")

# Fields set by add_schematic_component (Value/Footprint) or renamed
# (Reference); everything else on the source symbol is copied verbatim.
_ADD_HANDLED_FIELDS = {"Reference", "Value", "Footprint"}
_DEFAULT_OFFSET_MM = {"x": 10.0, "y": 0.0}


def _collect_references(content: str) -> List[str]:
    """Every placed-symbol Reference value in the schematic text."""
    lib_pos = content.find("(lib_symbols")
    lib_end = content.find("\n  )", lib_pos) if lib_pos >= 0 else -1
    refs: List[str] = []
    for m in re.finditer(r'\(property\s+"Reference"\s+"([^"]+)"', content):
        # Skip the template Reference inside a lib_symbols definition.
        if lib_pos >= 0 and lib_end > lib_pos and lib_pos < m.start() < lib_end:
            continue
        refs.append(m.group(1))
    return refs


def _next_free_reference(references: List[str], prefix: str) -> str:
    """Lowest ``prefix<N>`` not already used (e.g. R -> R3 when R1/R2 exist)."""
    used = set()
    pat = re.compile(r"^" + re.escape(prefix) + r"(\d+)$")
    for r in references:
        m = pat.match(r)
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return f"{prefix}{n}"


@serialize_on_param("schematicPath")
def handle_duplicate_schematic_component(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Duplicate a placed schematic symbol at an offset or explicit position."""
    logger.info("Duplicating schematic component")
    try:
        from commands.pin_locator import PinLocator

        from ._placement import handle_add_schematic_component
        from ._properties import handle_edit_schematic_component, handle_get_schematic_component

        schematic_path = params.get("schematicPath")
        reference = params.get("reference")
        new_reference = params.get("newReference")
        offset = params.get("offset")
        position = params.get("position")

        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}
        if not reference:
            return {"success": False, "message": "reference is required"}

        sch_file = Path(schematic_path)
        if not sch_file.exists():
            return {"success": False, "message": f"Schematic not found: {schematic_path}"}

        # Read the source symbol (position + all field values).
        src = handle_get_schematic_component(
            iface, {"schematicPath": schematic_path, "reference": reference}
        )
        if not src.get("success"):
            return {
                "success": False,
                "message": src.get("message", f"Source component '{reference}' not found"),
            }
        src_pos = src.get("position") or {"x": 0.0, "y": 0.0, "angle": 0.0}
        fields: Dict[str, Any] = src.get("fields") or {}

        # Resolve the source lib_id → library:type for the placement call.
        lib_id = PinLocator()._get_lib_id(sch_file, reference)
        if not lib_id:
            return {
                "success": False,
                "message": f"Could not resolve the library symbol of '{reference}'",
            }
        if ":" in lib_id:
            library, comp_type = lib_id.split(":", 1)
        else:
            library, comp_type = "Device", lib_id

        content = sch_file.read_text(encoding="utf-8")
        existing_refs = _collect_references(content)

        # Determine the new reference: honor an explicit one (rejecting a
        # collision), else auto-assign the next free ref of the same prefix.
        if new_reference:
            if new_reference in existing_refs:
                return {
                    "success": False,
                    "message": (
                        f"Reference '{new_reference}' already exists in the schematic; "
                        f"choose another or omit newReference to auto-assign."
                    ),
                    "errorCode": "REFERENCE_EXISTS",
                }
        else:
            prefix_match = re.match(r"^([^0-9]+)", reference)
            prefix = prefix_match.group(1) if prefix_match else reference
            new_reference = _next_free_reference(existing_refs, prefix)

        # Determine placement: explicit position wins, else source + offset.
        if position and isinstance(position, dict):
            new_x = float(position.get("x", src_pos.get("x", 0.0)))
            new_y = float(position.get("y", src_pos.get("y", 0.0)))
        else:
            off = offset if isinstance(offset, dict) else _DEFAULT_OFFSET_MM
            new_x = float(src_pos.get("x", 0.0)) + float(off.get("x", _DEFAULT_OFFSET_MM["x"]))
            new_y = float(src_pos.get("y", 0.0)) + float(off.get("y", _DEFAULT_OFFSET_MM["y"]))

        # Preserve the source's unit structure: place every unit when the
        # source is a multi-unit part with more than one unit on the sheet.
        place_all = False
        source_unit = 1
        try:
            info = PinLocator().get_unit_placement(sch_file, reference)
        except Exception:
            info = None
        if info and info.get("is_multi_unit"):
            placed = info.get("placed_units") or [1]
            if len(placed) > 1:
                place_all = True
            else:
                source_unit = placed[0]

        value = fields.get("Value", {}).get("value", comp_type)
        footprint = fields.get("Footprint", {}).get("value", "")

        component: Dict[str, Any] = {
            "library": library,
            "type": comp_type,
            "reference": new_reference,
            "value": value,
            "footprint": footprint,
            "x": new_x,
            "y": new_y,
            "unit": source_unit,
            "placeAllUnits": place_all,
        }
        add_res = handle_add_schematic_component(
            iface,
            {
                "schematicPath": schematic_path,
                "component": component,
                "placeAllUnits": place_all,
                # A duplicate targets an explicit spot next to the original;
                # snapping would nudge it off the requested offset.
                "snapToGrid": params.get("snapToGrid", False),
            },
        )
        if not add_res.get("success"):
            return {
                "success": False,
                "message": f"Failed to place duplicate: {add_res.get('message', 'unknown error')}",
            }

        # Copy every source field the placement call didn't already set
        # (Datasheet + custom sourcing properties like MPN / LCSC).
        extra_props = {
            name: fld.get("value", "")
            for name, fld in fields.items()
            if name not in _ADD_HANDLED_FIELDS
        }
        copied_properties: List[str] = []
        if extra_props:
            edit_res = handle_edit_schematic_component(
                iface,
                {
                    "schematicPath": schematic_path,
                    "reference": new_reference,
                    "properties": extra_props,
                },
            )
            if edit_res.get("success"):
                copied_properties = sorted(extra_props)
            else:
                logger.warning(
                    "duplicate_schematic_component: placed %s but failed to copy "
                    "properties %s: %s",
                    new_reference,
                    sorted(extra_props),
                    edit_res.get("message"),
                )

        new_pos = add_res.get("position") or {"x": new_x, "y": new_y}
        response: Dict[str, Any] = {
            "success": True,
            "reference": new_reference,
            "sourceReference": reference,
            "symbol_source": lib_id,
            "position": new_pos,
            "footprint": add_res.get("footprint", footprint),
            "value": value,
            "copiedProperties": copied_properties,
            "message": (
                f"Duplicated {reference} -> {new_reference} at "
                f"({new_pos.get('x')}, {new_pos.get('y')})"
            ),
        }
        # Propagate page-awareness fields from the placement call (S9).
        for key in ("pageSize", "snap", "offPageWarning", "units", "unitPositions", "offPageUnits"):
            if key in add_res:
                response[key] = add_res[key]
        return response

    except Exception as e:
        logger.error(f"Error duplicating schematic component: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}
