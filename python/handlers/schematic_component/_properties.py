"""Component get / property get-set-remove / edit handlers.

Split out of the former handlers/schematic_component.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Tuple

import sexpdata
from commands.schematic import SchematicManager
from commands.schematic_locks import atomic_write_text, serialize_on_param
from commands.wire_manager import WireManager

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.schematic_component")


def handle_get_schematic_component(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Return full component info: position and all field values with their (at x y angle) positions."""
    logger.info("Getting schematic component info")
    try:
        import re
        from pathlib import Path

        schematic_path = params.get("schematicPath")
        reference = params.get("reference")

        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}
        if not reference:
            return {"success": False, "message": "reference is required"}

        sch_file = Path(schematic_path)
        if not sch_file.exists():
            return {
                "success": False,
                "message": f"Schematic not found: {schematic_path}",
            }

        with open(sch_file, "r", encoding="utf-8") as f:
            content = f.read()

        def find_matching_paren(s: str, start: int) -> int:
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

        # Skip lib_symbols section
        lib_sym_pos = content.find("(lib_symbols")
        lib_sym_end = find_matching_paren(content, lib_sym_pos) if lib_sym_pos >= 0 else -1

        # Find the placed symbol block for this reference. KiCAD may emit
        # the children of (symbol ...) in different orders — most commonly
        # `(symbol (lib_id "..."))`, but symbols whose library entry has
        # been rescued / customised carry an extra `(lib_name "...")` first
        # (`(symbol (lib_name "...") (lib_id "..."))`). Match `(symbol\s+(`
        # — any opening paren — to handle both. The lib_symbols range check
        # below excludes library-definition symbols, which use the
        # `(symbol "name" ...)` form (quoted string, not paren).
        block_start = block_end = None
        search_start = 0
        pattern = re.compile(r"\(symbol\s+\(")
        while True:
            m = pattern.search(content, search_start)
            if not m:
                break
            pos = m.start()
            if lib_sym_pos >= 0 and lib_sym_pos <= pos <= lib_sym_end:
                search_start = lib_sym_end + 1
                continue
            end = find_matching_paren(content, pos)
            if end < 0:
                search_start = pos + 1
                continue
            block_text = content[pos : end + 1]
            if re.search(
                r'\(property\s+"Reference"\s+"' + re.escape(reference) + r'"',
                block_text,
            ):
                block_start, block_end = pos, end
                break
            search_start = end + 1

        if block_start is None or block_end is None:
            return {
                "success": False,
                "message": f"Component '{reference}' not found in schematic",
            }

        block_text = content[block_start : block_end + 1]

        # Extract component position: the first (at x y angle) inside the
        # symbol block. KiCAD always writes the symbol's own (at) before
        # any (property ...) child blocks, so the first match is the
        # symbol origin regardless of the (lib_name)/(lib_id) ordering.
        comp_at = re.search(
            r"\(at\s+([\d\.\-]+)\s+([\d\.\-]+)\s+([\d\.\-]+)\s*\)",
            block_text,
        )
        if comp_at:
            comp_pos = {
                "x": float(comp_at.group(1)),
                "y": float(comp_at.group(2)),
                "angle": float(comp_at.group(3)),
            }
        else:
            comp_pos = None

        # Extract all properties with their at positions
        prop_pattern = re.compile(
            r'\(property\s+"([^"]*)"\s+"([^"]*)"\s+\(at\s+([\d\.\-]+)\s+([\d\.\-]+)\s+([\d\.\-]+)\s*\)'
        )
        fields = {}
        for m in prop_pattern.finditer(block_text):
            name, value, x, y, angle = (
                m.group(1),
                m.group(2),
                m.group(3),
                m.group(4),
                m.group(5),
            )
            fields[name] = {
                "value": value,
                "x": float(x),
                "y": float(y),
                "angle": float(angle),
            }

        return {
            "success": True,
            "reference": reference,
            "position": comp_pos,
            "fields": fields,
        }

    except Exception as e:
        logger.error(f"Error getting schematic component: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_remove_schematic_component_property(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Remove a single custom property from a placed schematic symbol.

    Built-in fields (Reference, Value, Footprint, Datasheet) cannot be
    removed — use `edit_schematic_component` to clear them instead.
    """
    logger.info("Removing schematic component property")
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return {"success": False, "message": "name is required"}
    return handle_edit_schematic_component(
        iface,
        {
            "schematicPath": params.get("schematicPath"),
            "reference": params.get("reference"),
            "removeProperties": [name],
        },
    )


def handle_set_schematic_component_property(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Add or update a single property on a placed schematic symbol.

    Convenience wrapper around `edit_schematic_component` for the very common
    case of setting one BOM/sourcing field at a time. The property is created
    if it does not already exist, otherwise its value (and optionally its
    position / visibility) is updated in place.
    """
    logger.info("Setting schematic component property")
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return {"success": False, "message": "name is required"}
    if "value" not in params:
        return {"success": False, "message": "value is required"}

    spec: Dict[str, Any] = {"value": params["value"]}
    for key in ("x", "y", "angle", "hide", "fontSize"):
        if params.get(key) is not None:
            spec[key] = params[key]

    return handle_edit_schematic_component(
        iface,
        {
            "schematicPath": params.get("schematicPath"),
            "reference": params.get("reference"),
            "properties": {name: spec},
        },
    )


@serialize_on_param("schematicPath")
def handle_edit_schematic_component(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Update properties of a placed symbol in a schematic.

    Supports updating the standard fields (footprint / value / reference rename),
    repositioning field labels, and managing **arbitrary custom properties**
    (MPN, Manufacturer, Distributor part numbers, Voltage, Dielectric, Tolerance,
    LCSC, etc.) used by BOM/CPL exporters and JLCPCB / Digi-Key sourcing.

    Uses text-based in-place editing — preserves position, UUID, and all
    unrelated fields.
    """
    logger.info("Editing schematic component")
    try:
        import re
        from pathlib import Path

        schematic_path = params.get("schematicPath")
        reference = params.get("reference")
        new_footprint = params.get("footprint")
        new_value = params.get("value")
        new_reference = params.get("newReference")
        # dict: {"Reference": {"x": 1, "y": 2, "angle": 0}}
        field_positions = params.get("fieldPositions")
        # dict: {"MPN": "RC0603FR-0710KL"}  OR  {"MPN": {"value": "...", "hide": true}}
        properties = params.get("properties")
        # list[str]: ["OldField"] — protected built-ins are rejected
        remove_properties = params.get("removeProperties")

        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}
        if not reference:
            return {"success": False, "message": "reference is required"}
        if not any(
            [
                new_footprint is not None,
                new_value is not None,
                new_reference is not None,
                field_positions is not None,
                properties is not None,
                remove_properties is not None,
            ]
        ):
            return {
                "success": False,
                "message": (
                    "At least one of footprint, value, newReference, fieldPositions, "
                    "properties, or removeProperties must be provided"
                ),
            }

        # Reject removal attempts targeting protected built-in fields up-front
        if remove_properties:
            blocked = [n for n in remove_properties if n in iface._PROTECTED_PROPERTY_FIELDS]
            if blocked:
                return {
                    "success": False,
                    "message": (
                        f"Cannot remove built-in field(s) {blocked}: use the dedicated "
                        "value/footprint/newReference parameters or set the value to ''"
                    ),
                }

        sch_file = Path(schematic_path)
        if not sch_file.exists():
            return {
                "success": False,
                "message": f"Schematic not found: {schematic_path}",
            }

        with open(sch_file, "r", encoding="utf-8") as f:
            content = f.read()

        # Skip lib_symbols section
        lib_sym_pos = content.find("(lib_symbols")
        lib_sym_end = iface._find_matching_paren(content, lib_sym_pos) if lib_sym_pos >= 0 else -1

        # Find placed symbol blocks that match the reference. KiCAD may
        # serialise the children of (symbol ...) in different orders —
        # `(symbol (lib_id "..."))` is the common case but rescued or
        # locally-customised symbols carry an extra `(lib_name "...")`
        # before the lib_id: `(symbol (lib_name "...") (lib_id "..."))`.
        # Match any opening paren after `(symbol`; the lib_symbols range
        # check below excludes library-definition symbols, which use the
        # `(symbol "name" ...)` form (quoted string, not paren).
        block_start = block_end = None
        search_start = 0
        pattern = re.compile(r"\(symbol\s+\(")
        while True:
            m = pattern.search(content, search_start)
            if not m:
                break
            pos = m.start()
            # Skip if inside lib_symbols section
            if lib_sym_pos >= 0 and lib_sym_pos <= pos <= lib_sym_end:
                search_start = lib_sym_end + 1
                continue
            end = iface._find_matching_paren(content, pos)
            if end < 0:
                search_start = pos + 1
                continue
            block_text = content[pos : end + 1]
            if re.search(
                r'\(property\s+"Reference"\s+"' + re.escape(reference) + r'"',
                block_text,
            ):
                block_start, block_end = pos, end
                break
            search_start = end + 1

        if block_start is None or block_end is None:
            return {
                "success": False,
                "message": f"Component '{reference}' not found in schematic",
            }

        # Apply property replacements within the found block
        block_text = content[block_start : block_end + 1]

        # Determine the parent symbol position so that newly-added properties
        # default to a sensible location (anchored near the component).
        # KiCAD always emits the symbol's own (at x y angle) before any
        # (property ...) child blocks, so the FIRST (at ...) inside the
        # symbol block is the symbol origin regardless of whether
        # (lib_name ...) precedes (lib_id ...).
        comp_at = re.search(
            r"\(at\s+([\d\.\-]+)\s+([\d\.\-]+)",
            block_text,
        )
        comp_origin: Tuple[float, float] = (
            (float(comp_at.group(1)), float(comp_at.group(2))) if comp_at else (0.0, 0.0)
        )

        if new_footprint is not None:
            escaped_fp = iface._escape_sexpr_string(str(new_footprint))
            block_text = re.sub(
                r'(\(property\s+"Footprint"\s+)"[^"]*"',
                rf'\1"{escaped_fp}"',
                block_text,
            )
        if new_value is not None:
            escaped_v = iface._escape_sexpr_string(str(new_value))
            block_text = re.sub(
                r'(\(property\s+"Value"\s+)"[^"]*"',
                rf'\1"{escaped_v}"',
                block_text,
            )
        if new_reference is not None:
            escaped_r = iface._escape_sexpr_string(str(new_reference))
            block_text = re.sub(
                r'(\(property\s+"Reference"\s+)"[^"]*"',
                rf'\1"{escaped_r}"',
                block_text,
            )
            # Also update the (reference "...") leaves inside the symbol's
            # (instances) → (project) → (path) subtree. KiCad reads those
            # entries — not the (property "Reference" ...) field — when
            # generating netlists and syncing the PCB via "Update PCB from
            # Schematic", so leaving them stale produces a silent
            # reference mismatch where eeschema shows the new ref but ERC
            # / netlist export / PCB sync all use the old one. See #126.
            instances_pos = block_text.find("(instances")
            if instances_pos >= 0:
                instances_end = iface._find_matching_paren(block_text, instances_pos)
                if instances_end >= 0:
                    instances_block = block_text[instances_pos : instances_end + 1]
                    updated_instances = re.sub(
                        r'(\(reference\s+)"' + re.escape(reference) + r'"',
                        rf'\1"{escaped_r}"',
                        instances_block,
                    )
                    block_text = (
                        block_text[:instances_pos]
                        + updated_instances
                        + block_text[instances_end + 1 :]
                    )
        if field_positions is not None:
            for field_name, pos in field_positions.items():
                x = pos.get("x", 0)
                y = pos.get("y", 0)
                angle = pos.get("angle", 0)
                block_text = re.sub(
                    r'(\(property\s+"'
                    + re.escape(field_name)
                    + r'"\s+"[^"]*"\s+)\(at\s+[\d\.\-]+\s+[\d\.\-]+\s+[\d\.\-]+\s*\)',
                    rf"\1(at {x} {y} {angle})",
                    block_text,
                )

        properties_added: Dict[str, Any] = {}
        properties_updated: Dict[str, Any] = {}
        if properties:
            if not isinstance(properties, dict):
                return {
                    "success": False,
                    "message": "properties must be a dict mapping property name -> value or spec",
                }
            for name, spec in properties.items():
                if not isinstance(name, str) or not name:
                    return {
                        "success": False,
                        "message": f"Invalid property name: {name!r}",
                    }
                # Normalise scalar values to a spec dict with just {"value": ...}
                if not isinstance(spec, dict):
                    spec = {"value": spec}
                try:
                    block_text, action = iface._set_property_in_block(
                        block_text, name, spec, comp_origin
                    )
                except ValueError as ve:
                    return {"success": False, "message": str(ve)}
                target = properties_added if action == "added" else properties_updated
                target[name] = spec.get("value")

        properties_removed: list = []
        if remove_properties:
            if not isinstance(remove_properties, list):
                return {
                    "success": False,
                    "message": "removeProperties must be a list of property names",
                }
            for name in remove_properties:
                block_text, removed = iface._remove_property_from_block(block_text, name)
                if removed:
                    properties_removed.append(name)

        content = content[:block_start] + block_text + content[block_end + 1 :]

        atomic_write_text(sch_file, content)

        changes: Dict[str, Any] = {
            k: v
            for k, v in {
                "footprint": new_footprint,
                "value": new_value,
                "reference": new_reference,
            }.items()
            if v is not None
        }
        if field_positions is not None:
            changes["fieldPositions"] = field_positions
        if properties_added:
            changes["propertiesAdded"] = properties_added
        if properties_updated:
            changes["propertiesUpdated"] = properties_updated
        if properties_removed:
            changes["propertiesRemoved"] = properties_removed

        logger.info(f"Edited schematic component {reference}: {changes}")
        return {"success": True, "reference": reference, "updated": changes}

    except Exception as e:
        logger.error(f"Error editing schematic component: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}
