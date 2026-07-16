"""Wire, connection and no-connect handlers.

Split out of the former handlers/schematic_wire.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

from commands.connection_schematic import ConnectionManager

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.schematic_wire")


def handle_delete_schematic_wire(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Delete a wire from the schematic matching start/end points"""
    logger.info("Deleting schematic wire")
    try:
        schematic_path = params.get("schematicPath")
        start = params.get("start", {})
        end = params.get("end", {})

        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}

        from commands.wire_manager import WireManager

        start_point = [start.get("x", 0), start.get("y", 0)]
        end_point = [end.get("x", 0), end.get("y", 0)]

        # Sweep ALL coincident wires (a stray duplicate overlapping pair is
        # removed in one call) and report how many were cleared (S5).
        removed = WireManager.delete_wires(Path(schematic_path), start_point, end_point)
        if removed:
            msg = f"Deleted {removed} wire(s)"
            if removed > 1:
                msg += " (swept coincident duplicates)"
            return {"success": True, "removed": removed, "message": msg}
        else:
            return {"success": False, "message": "No matching wire found"}

    except Exception as e:
        logger.error(f"Error deleting schematic wire: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_connect_passthrough(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Connect all pins of source connector to matching pins of target connector"""
    logger.info("Connecting passthrough between two connectors")
    try:
        schematic_path = params.get("schematicPath")
        source_ref = params.get("sourceRef")
        target_ref = params.get("targetRef")
        net_prefix = params.get("netPrefix", "PIN")
        pin_offset = int(params.get("pinOffset", 0))

        if not all([schematic_path, source_ref, target_ref]):
            return {
                "success": False,
                "message": "Missing required parameters: schematicPath, sourceRef, targetRef",
            }

        result = ConnectionManager.connect_passthrough(
            Path(schematic_path), source_ref, target_ref, net_prefix, pin_offset
        )

        # Also assign nets to PCB pads for each successfully connected pin
        pcb_assigned = 0
        if iface.board:
            import re as _re

            for conn_info in result.get("connected", []):
                # Expected format: "{src_ref}/{pin} <-> {tgt_ref}/{pin} [{net}]"
                try:
                    parts = conn_info.split(" <-> ")
                    if len(parts) != 2:
                        continue
                    src_part = parts[0]
                    rest = parts[1]
                    bracket_match = _re.search(r"\[(.+)\]", rest)
                    tgt_part = rest.split(" [")[0] if " [" in rest else rest
                    net_name = bracket_match.group(1) if bracket_match else None
                    if not net_name:
                        continue

                    src_ref_pin = src_part.split("/")
                    tgt_ref_pin = tgt_part.split("/")
                    if len(src_ref_pin) == 2 and iface._assign_net_to_pad(
                        src_ref_pin[0], src_ref_pin[1], net_name
                    ):
                        pcb_assigned += 1
                    if len(tgt_ref_pin) == 2 and iface._assign_net_to_pad(
                        tgt_ref_pin[0], tgt_ref_pin[1], net_name
                    ):
                        pcb_assigned += 1
                except Exception as parse_err:
                    logger.debug(
                        f"Could not parse passthrough result for PCB assignment: {parse_err}"
                    )

        n_ok = len(result["connected"])
        n_fail = len(result["failed"])
        msg = f"Passthrough complete: {n_ok} connected, {n_fail} failed"
        if pcb_assigned:
            msg += f" ({pcb_assigned} PCB pads updated)"
        return {
            "success": n_fail == 0,
            "message": msg,
            "connected": result["connected"],
            "failed": result["failed"],
        }
    except Exception as e:
        logger.error(f"Error in connect_passthrough: {str(e)}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_connect_to_net(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Connect a component pin to a named net using wire stub and label,
    and also assign the net to the corresponding pad on the PCB board so
    that save_project persists the net (pcbnew.SaveBoard only writes nets
    that are referenced by at least one board element).
    """
    logger.info("Connecting component pin to net")
    try:
        schematic_path = params.get("schematicPath")
        component_ref = params.get("componentRef")
        pin_name = params.get("pinName")
        net_name = params.get("netName")

        if not all([schematic_path, component_ref, pin_name, net_name]):
            return {"success": False, "message": "Missing required parameters"}

        # Use ConnectionManager with new WireManager integration.  Forward the
        # A1 override: when a DIFFERENT component's pin is coincident with the
        # target pin, connect_to_net refuses by default (kind:'coincident_pin')
        # rather than silently capture that foreign pin onto the net;
        # allowCoincidentPin=true connects anyway.
        allow_coincident_pin = bool(params.get("allowCoincidentPin", False))
        result = ConnectionManager.connect_to_net(
            Path(schematic_path),
            component_ref,
            pin_name,
            net_name,
            allow_coincident_pin=allow_coincident_pin,
        )

        # Also assign the net to the pad on the PCB board
        if iface.board and isinstance(result, dict) and result.get("success"):
            try:
                if iface._assign_net_to_pad(component_ref, pin_name, net_name):
                    msg = result.get("message", "")
                    result["message"] = (msg + " (PCB pad also updated)").strip()
            except Exception as pcb_err:
                logger.warning(f"Could not assign net to PCB pad: {pcb_err}")

        return result
    except Exception as e:
        logger.error(f"Error connecting to net: {str(e)}")
        import traceback

        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": str(e),
            "errorDetails": traceback.format_exc(),
        }


def handle_add_no_connect(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Add a no-connect flag (X marker) to an unconnected pin in the schematic."""
    logger.info("Adding no-connect flag to schematic")
    try:
        from commands.pin_locator import PinLocator
        from commands.wire_manager import WireManager

        schematic_path = params.get("schematicPath")
        position = params.get("position")
        component_ref = params.get("componentRef")
        pin_number = params.get("pinNumber")

        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}

        # Snap to pin endpoint when componentRef + pinNumber are provided
        snapped_to_pin = None
        if component_ref and pin_number is not None:
            locator = PinLocator()
            pin_loc = locator.get_pin_location(Path(schematic_path), component_ref, str(pin_number))
            if pin_loc is None:
                # S10: say whether the COMPONENT or the PIN is the missing one.
                diag = locator.diagnose_missing_pin(
                    Path(schematic_path), component_ref, str(pin_number)
                )
                return {
                    "success": False,
                    "message": locator.format_missing_pin_error(
                        component_ref, str(pin_number), diag
                    ),
                }
            position = pin_loc
            snapped_to_pin = {"component": component_ref, "pin": str(pin_number)}
        elif position is None:
            return {
                "success": False,
                "message": "Provide either position [x, y] or componentRef + pinNumber",
            }

        success = WireManager.add_no_connect(Path(schematic_path), position)
        if success:
            result = {
                "success": True,
                "message": f"Added no-connect flag at {position}",
                "actual_position": position,
            }
            if snapped_to_pin:
                result["snapped_to_pin"] = snapped_to_pin
            return result
        else:
            return {"success": False, "message": "Failed to add no-connect flag"}

    except Exception as e:
        import traceback

        logger.error(f"Error adding no-connect: {e}")
        return {
            "success": False,
            "message": str(e),
            "errorDetails": traceback.format_exc(),
        }


def handle_delete_no_connect(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Delete a no-connect flag from the schematic (inverse of add_no_connect).

    A no-connect flag carries no name, so it is matched by position. Pass
    componentRef + pinNumber to snap onto the exact pin endpoint (the same
    way add_no_connect places it), or position [x, y] directly.
    """
    logger.info("Deleting no-connect flag from schematic")
    try:
        from commands.pin_locator import PinLocator
        from commands.wire_manager import WireManager

        schematic_path = params.get("schematicPath")
        position = params.get("position")
        component_ref = params.get("componentRef")
        pin_number = params.get("pinNumber")
        tolerance = float(params.get("tolerance", 0.5))

        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}

        snapped_to_pin = None
        if component_ref and pin_number is not None:
            locator = PinLocator()
            pin_loc = locator.get_pin_location(Path(schematic_path), component_ref, str(pin_number))
            if pin_loc is None:
                # S10: say whether the COMPONENT or the PIN is the missing one.
                diag = locator.diagnose_missing_pin(
                    Path(schematic_path), component_ref, str(pin_number)
                )
                return {
                    "success": False,
                    "message": locator.format_missing_pin_error(
                        component_ref, str(pin_number), diag
                    ),
                }
            position = pin_loc
            snapped_to_pin = {"component": component_ref, "pin": str(pin_number)}
        elif position is None:
            return {
                "success": False,
                "message": "Provide either position [x, y] or componentRef + pinNumber",
            }

        # Accept either [x, y] or {x, y}
        if isinstance(position, dict):
            position = [position.get("x", 0), position.get("y", 0)]

        deleted = WireManager.delete_no_connect(Path(schematic_path), position, tolerance)
        if deleted:
            result = {
                "success": True,
                "message": f"Deleted no-connect flag at {position}",
                "position": position,
            }
            if snapped_to_pin:
                result["snapped_to_pin"] = snapped_to_pin
            return result
        return {"success": False, "message": f"No no-connect flag found near {position}"}

    except Exception as e:
        import traceback

        logger.error(f"Error deleting no-connect: {e}")
        return {
            "success": False,
            "message": str(e),
            "errorDetails": traceback.format_exc(),
        }


def handle_add_schematic_wire(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Add a wire to a schematic using WireManager, with optional pin snapping"""
    logger.info("Adding wire to schematic")
    try:
        from commands.wire_manager import WireManager

        schematic_path = params.get("schematicPath")
        points = params.get("waypoints")
        properties = params.get("properties", {})
        snap_to_pins = params.get("snapToPins", True)
        snap_tolerance = params.get("snapTolerance", 1.0)

        if not schematic_path:
            return {"success": False, "message": "Schematic path is required"}
        if not points or len(points) < 2:
            return {
                "success": False,
                "message": "At least 2 waypoints are required",
            }

        # Make a mutable copy of points
        points = [list(p) for p in points]

        # Pin snapping: adjust first and last endpoints to nearest pin
        snapped_info = []
        if snap_to_pins:
            from commands.pin_locator import PinLocator

            locator = PinLocator()
            sch_path = Path(schematic_path)

            # Load schematic to iterate all symbols
            from skip import Schematic as SkipSchematic

            sch = SkipSchematic(str(sch_path))

            # Collect all pin locations: list of (ref, pin_num, [x, y])
            all_pins = []
            for symbol in sch.symbol:
                if not hasattr(symbol.property, "Reference"):
                    continue
                ref = symbol.property.Reference.value
                if ref.startswith("_TEMPLATE"):
                    continue
                pin_locs = locator.get_all_symbol_pins(sch_path, ref)
                for pin_num, coords in pin_locs.items():
                    all_pins.append((ref, pin_num, coords))

            def find_nearest_pin(point: Any, tolerance: Any) -> Any:
                """Find the nearest pin within tolerance of a point."""
                best = None
                best_dist = tolerance
                for ref, pin_num, coords in all_pins:
                    dx = point[0] - coords[0]
                    dy = point[1] - coords[1]
                    dist = (dx * dx + dy * dy) ** 0.5
                    if dist <= best_dist:
                        best_dist = dist
                        best = (ref, pin_num, coords)
                return best

            # Snap first endpoint
            match = find_nearest_pin(points[0], snap_tolerance)
            if match:
                ref, pin_num, coords = match
                logger.info(f"Snapped start point {points[0]} -> {coords} (pin {ref}/{pin_num})")
                snapped_info.append(
                    f"start snapped to {ref}/{pin_num} at [{coords[0]}, {coords[1]}]"
                )
                points[0] = list(coords)

            # Snap last endpoint
            match = find_nearest_pin(points[-1], snap_tolerance)
            if match:
                ref, pin_num, coords = match
                logger.info(f"Snapped end point {points[-1]} -> {coords} (pin {ref}/{pin_num})")
                snapped_info.append(f"end snapped to {ref}/{pin_num} at [{coords[0]}, {coords[1]}]")
                points[-1] = list(coords)

        # Extract wire properties
        stroke_width = properties.get("stroke_width", 0)
        stroke_type = properties.get("stroke_type", "default")

        # Use WireManager for S-expression manipulation
        if len(points) == 2:
            success = WireManager.add_wire(
                Path(schematic_path),
                points[0],
                points[1],
                stroke_width=stroke_width,
                stroke_type=stroke_type,
            )
        else:
            success = WireManager.add_polyline_wire(
                Path(schematic_path),
                points,
                stroke_width=stroke_width,
                stroke_type=stroke_type,
            )

        if success:
            message = "Wire added successfully"
            if snapped_info:
                message += "; " + "; ".join(snapped_info)
            return {"success": True, "message": message}
        else:
            return {"success": False, "message": "Failed to add wire"}
    except Exception as e:
        logger.error(f"Error adding wire to schematic: {str(e)}")
        import traceback

        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": str(e),
            "errorDetails": traceback.format_exc(),
        }
