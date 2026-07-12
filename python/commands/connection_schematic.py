import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from skip import Schematic

logger = logging.getLogger(__name__)

# Import new wire and pin managers
try:
    from commands.pin_locator import PinLocator
    from commands.wire_manager import WireManager

    WIRE_MANAGER_AVAILABLE = True
except ImportError:
    logger.warning("WireManager/PinLocator not available")
    WIRE_MANAGER_AVAILABLE = False


class ConnectionManager:
    """Manage connections between components in schematics"""

    # Initialize pin locator (class variable, shared across instances)
    _pin_locator = None

    @classmethod
    def get_pin_locator(cls) -> Any:
        """Get or create pin locator instance"""
        if cls._pin_locator is None and WIRE_MANAGER_AVAILABLE:
            cls._pin_locator = PinLocator()
        return cls._pin_locator

    @staticmethod
    def _lookup_lib_value(schematic_path: Path, component_ref: str) -> tuple:
        """Return ``(lib_id, value)`` for the placed symbol ``component_ref``.

        Used to detect power *ports* (lib_id ``power:*`` / ref ``#PWR…``) so
        connecting one to a net named by its own Value can be short-circuited
        (F4). Returns ``(None, None)`` when the symbol or file can't be read.
        """
        try:
            sch = Schematic(str(schematic_path))
        except Exception as e:  # missing/unparseable file — caller falls back
            logger.debug(f"_lookup_lib_value: could not load {schematic_path}: {e}")
            return None, None
        for symbol in getattr(sch, "symbol", []):
            try:
                if not hasattr(symbol, "property") or not hasattr(symbol.property, "Reference"):
                    continue
                if symbol.property.Reference.value.rstrip("_") != component_ref:
                    continue
                lib_id = symbol.lib_id.value if hasattr(symbol, "lib_id") else None
                value = symbol.property.Value.value if hasattr(symbol.property, "Value") else None
                return lib_id, value
            except AttributeError:
                continue
        return None, None

    @staticmethod
    def connect_to_net(
        schematic_path: Path, component_ref: str, pin_name: str, net_name: str
    ) -> Dict[str, Any]:
        """
        Connect a component pin to a named net using a wire stub and label.

        Args:
            schematic_path: Path to .kicad_sch file
            component_ref: Reference designator (e.g., "U1", "U1_")
            pin_name: Pin name/number
            net_name: Name of the net to connect to (e.g., "VCC", "GND", "SIGNAL_1")

        Returns:
            Dict with keys:
              success        – bool
              pin_location   – [x, y] exact pin endpoint used (present on success)
              label_location – [x, y] where the net label was placed (present on success)
              wire_stub      – [[x1,y1],[x2,y2]] the wire segment added (present on success)
              message        – human-readable status
        """
        try:
            if not WIRE_MANAGER_AVAILABLE:
                logger.error("WireManager/PinLocator not available")
                return {"success": False, "message": "WireManager/PinLocator not available"}

            locator = ConnectionManager.get_pin_locator()
            if not locator:
                logger.error("Pin locator unavailable")
                return {"success": False, "message": "Pin locator unavailable"}

            # Power-symbol short-circuit (F4): a power PORT (#PWR…, lib_id
            # "power:*") already joins the net named by its Value and self-labels
            # its own pin, so a matching-name connection is a redundant double
            # label — skip the wire+label entirely. A mismatched name is almost
            # certainly a mistake (the pin then carries both names); still wire
            # it but warn. PWR_FLAG (#FLG) is not a named port and never matches.
            lib_id, value = ConnectionManager._lookup_lib_value(schematic_path, component_ref)
            power_warning: Optional[str] = None
            if (
                component_ref.startswith("#PWR")
                and not component_ref.startswith("#FLG")
                and str(lib_id or "").startswith("power:")
            ):
                if value is not None and net_name == value:
                    logger.info(
                        f"Skipping redundant connect_to_net on power symbol "
                        f"{component_ref} (its Value '{value}' already names the net)"
                    )
                    return {
                        "success": True,
                        "already_connected": True,
                        "skipped_label": True,
                        "power_symbol": {"ref": component_ref, "value": value},
                        "message": (
                            f"{component_ref} is a power symbol; its pin already "
                            f"joins net '{value}' via its Value, so no wire/label "
                            f"was added. Power symbols self-label their pin."
                        ),
                    }
                power_warning = (
                    f"{component_ref} is a power symbol already driving net "
                    f"'{value}' via its Value; connecting it to '{net_name}' will "
                    f"not rename that net — its pin ends up on BOTH nets. This is "
                    f"almost certainly a mistake."
                )

            # Get pin location using PinLocator
            pin_loc = locator.get_pin_location(schematic_path, component_ref, pin_name)
            if not pin_loc:
                msg = f"Could not locate pin {component_ref}/{pin_name}"
                logger.error(msg)
                return {"success": False, "message": msg}

            # Add a small wire stub from the pin (2.54mm = 0.1 inch, standard grid
            # spacing). get_pin_angle returns the OUTWARD direction (away from the
            # symbol body) in the (cos θ, -sin θ) screen convention used below, so
            # the stub — and the label at its far end — always land clear of the
            # symbol, for every pin orientation under any rotation/mirror.
            try:
                pin_angle_deg = locator.get_pin_angle(schematic_path, component_ref, pin_name) or 0
            except Exception as e:
                logger.warning(
                    f"Could not get pin angle for {component_ref}/{pin_name}, defaulting to 0: {e}"
                )
                pin_angle_deg = 0
            import math as _math

            angle_rad = _math.radians(pin_angle_deg)
            stub_end = [
                round(pin_loc[0] + 2.54 * _math.cos(angle_rad), 4),
                round(pin_loc[1] - 2.54 * _math.sin(angle_rad), 4),
            ]

            # Orient the label text so it reads outward from the pin rather than
            # back over the symbol body. The outward angle is already ~0/90/180/270
            # for axis-aligned pins; snap to a KiCad label orientation (WireManager
            # picks the matching horizontal justify from this).
            label_orientation = int(round(pin_angle_deg / 90.0) * 90) % 360

            # Create wire stub using WireManager
            wire_success = WireManager.add_wire(schematic_path, pin_loc, stub_end)
            if not wire_success:
                msg = "Failed to create wire stub for net connection"
                logger.error(msg)
                return {"success": False, "message": msg}

            # Add label at the end of the stub using WireManager
            label_success = WireManager.add_label(
                schematic_path,
                net_name,
                stub_end,
                label_type="label",
                orientation=label_orientation,
            )
            if not label_success:
                msg = f"Failed to add net label '{net_name}'"
                logger.error(msg)
                return {"success": False, "message": msg}

            logger.info(f"Connected {component_ref}/{pin_name} to net '{net_name}'")
            result: Dict[str, Any] = {
                "success": True,
                "message": f"Connected {component_ref}/{pin_name} to net '{net_name}'",
                "pin_location": pin_loc,
                "label_location": stub_end,
                "wire_stub": [pin_loc, stub_end],
            }
            if power_warning:
                result["warnings"] = [power_warning]
            return result

        except Exception as e:  # API boundary; bucket: catch + return
            logger.exception(f"Error connecting to net: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return {"success": False, "message": str(e)}

    @staticmethod
    def connect_passthrough(
        schematic_path: Path,
        source_ref: str,
        target_ref: str,
        net_prefix: str = "PIN",
        pin_offset: int = 0,
    ) -> Dict[str, List[str]]:
        """
        Connect all pins of source_ref to matching pins of target_ref via shared net labels.
        Useful for passthrough adapters: J1 pin N <-> J2 pin N on net {net_prefix}_{N}.

        Args:
            schematic_path: Path to .kicad_sch file
            source_ref: Reference of the first connector (e.g., "J1")
            target_ref: Reference of the second connector (e.g., "J2")
            net_prefix: Prefix for generated net names (default: "PIN" -> PIN_1, PIN_2, ...)
            pin_offset: Add this value to the pin number when building the net name (default 0)

        Returns:
            dict with 'connected' list and 'failed' list
        """
        if not WIRE_MANAGER_AVAILABLE:
            logger.error("WireManager/PinLocator not available")
            return {"connected": [], "failed": ["WireManager unavailable"]}

        locator = ConnectionManager.get_pin_locator()
        if not locator:
            return {"connected": [], "failed": ["PinLocator unavailable"]}

        # Get all pins of source and target
        src_pins = locator.get_all_symbol_pins(schematic_path, source_ref) or {}
        tgt_pins = locator.get_all_symbol_pins(schematic_path, target_ref) or {}

        if not src_pins:
            return {"connected": [], "failed": [f"No pins found on {source_ref}"]}
        if not tgt_pins:
            return {"connected": [], "failed": [f"No pins found on {target_ref}"]}

        connected = []
        failed = []

        for pin_num in sorted(src_pins.keys(), key=lambda x: int(x) if x.isdigit() else 0):
            try:
                net_name = (
                    f"{net_prefix}_{int(pin_num) + pin_offset}"
                    if pin_num.isdigit()
                    else f"{net_prefix}_{pin_num}"
                )

                res_src = ConnectionManager.connect_to_net(
                    schematic_path, source_ref, pin_num, net_name
                )
                if not res_src.get("success"):
                    failed.append(f"{source_ref}/{pin_num}")
                    continue

                if pin_num in tgt_pins:
                    res_tgt = ConnectionManager.connect_to_net(
                        schematic_path, target_ref, pin_num, net_name
                    )
                    if not res_tgt.get("success"):
                        failed.append(f"{target_ref}/{pin_num}")
                        continue
                else:
                    failed.append(f"{target_ref}/{pin_num} (pin not found)")
                    continue

                connected.append(f"{source_ref}/{pin_num} <-> {target_ref}/{pin_num} [{net_name}]")
            except Exception as e:
                failed.append(f"{source_ref}/{pin_num}: {e}")

        logger.info(f"connect_passthrough: {len(connected)} connected, {len(failed)} failed")
        return {"connected": connected, "failed": failed}

    @staticmethod
    def get_net_connections(
        schematic: Schematic, net_name: str, schematic_path: Optional[Path] = None
    ) -> List[Dict]:
        """
        Get all connections for a named net using wire graph analysis

        Args:
            schematic: Schematic object
            net_name: Name of the net to query
            schematic_path: Optional path to schematic file (enables accurate pin matching)

        Returns:
            List of connections: [{"component": ref, "pin": pin_name}, ...]
        """
        try:
            from commands.pin_locator import PinLocator

            connections = []
            tolerance = 0.5  # 0.5mm tolerance for point coincidence (grid spacing consideration)

            def points_coincide(p1: Any, p2: Any) -> bool:
                """Check if two points are the same (within tolerance)"""
                if not p1 or not p2:
                    return False
                dx = abs(p1[0] - p2[0])
                dy = abs(p1[1] - p2[1])
                return dx < tolerance and dy < tolerance

            # 1. Find all labels with this net name
            if not hasattr(schematic, "label"):
                logger.warning("Schematic has no labels")
                return connections

            net_label_positions = []
            for label in schematic.label:
                if hasattr(label, "value") and label.value == net_name:
                    if hasattr(label, "at") and hasattr(label.at, "value"):
                        pos = label.at.value
                        net_label_positions.append([float(pos[0]), float(pos[1])])

            if not net_label_positions:
                logger.info(f"No labels found for net '{net_name}'")
                return connections

            logger.debug(f"Found {len(net_label_positions)} labels for net '{net_name}'")

            # 2. Find all wires connected to these label positions.
            # A missing wire attribute is fine — all_match_points will still
            # include label positions, so label-at-pin connections are detected.
            connected_wire_points: set[tuple[float, float]] = set()
            if not hasattr(schematic, "wire"):
                logger.debug("Schematic has no wires — will match labels to pins directly")

            for wire in (schematic.wire if hasattr(schematic, "wire") else []):
                if hasattr(wire, "pts") and hasattr(wire.pts, "xy"):
                    # Get all points in this wire (polyline)
                    wire_points = []
                    for point in wire.pts.xy:
                        if hasattr(point, "value"):
                            wire_points.append([float(point.value[0]), float(point.value[1])])

                    # Check if any wire point touches a label
                    wire_connected = False
                    for wire_pt in wire_points:
                        for label_pt in net_label_positions:
                            if points_coincide(wire_pt, label_pt):
                                wire_connected = True
                                break
                        if wire_connected:
                            break

                    # If this wire is connected to the net, add all its points
                    if wire_connected:
                        for pt in wire_points:
                            connected_wire_points.add((pt[0], pt[1]))

            # Build match points: union of wire endpoints AND label positions.
            # This handles the valid KiCad style where a net label is placed
            # directly at a pin endpoint with no wire segment in between.
            all_match_points = connected_wire_points | {(p[0], p[1]) for p in net_label_positions}

            if not all_match_points:
                logger.debug(f"No connection points found for net '{net_name}'")
                return connections

            logger.debug(
                f"Found {len(connected_wire_points)} wire points, "
                f"{len(net_label_positions)} direct label positions, "
                f"{len(all_match_points)} total match points for net '{net_name}'"
            )

            # 3. Find component pins at wire endpoints
            if not hasattr(schematic, "symbol"):
                logger.warning("Schematic has no symbols")
                return connections

            # Create pin locator for accurate pin matching (if schematic_path available)
            locator = None
            if schematic_path and WIRE_MANAGER_AVAILABLE:
                locator = PinLocator()

            for symbol in schematic.symbol:
                # Skip template symbols
                if not hasattr(symbol.property, "Reference"):
                    continue

                ref = symbol.property.Reference.value
                if ref.startswith("_TEMPLATE"):
                    continue

                # Get lib_id for pin location lookup
                lib_id = symbol.lib_id.value if hasattr(symbol, "lib_id") else None
                if not lib_id:
                    continue

                # If we have PinLocator and schematic_path, do accurate pin matching
                if locator and schematic_path:
                    try:
                        # Get all pins for this symbol
                        pins = locator.get_symbol_pins(schematic_path, lib_id)
                        if not pins:
                            continue

                        # Check each pin
                        for pin_num, pin_data in pins.items():
                            # Get pin location
                            pin_loc = locator.get_pin_location(schematic_path, ref, pin_num)
                            if not pin_loc:
                                continue

                            # Check if pin coincides with any match point
                            for wire_pt_tup in all_match_points:
                                if points_coincide(pin_loc, list(wire_pt_tup)):
                                    connections.append({"component": ref, "pin": pin_num})
                                    break  # Pin found, no need to check more wire points

                    except Exception as e:
                        logger.warning(f"Error matching pins for {ref}: {e}")
                        # Fall back to proximity matching

                # Fallback: proximity-based matching if no PinLocator
                if not locator or not schematic_path:
                    symbol_pos = symbol.at.value if hasattr(symbol, "at") else None
                    if not symbol_pos:
                        continue

                    symbol_x = float(symbol_pos[0])
                    symbol_y = float(symbol_pos[1])

                    # Check if symbol is near any match point (within 10mm)
                    for wire_pt_tup in all_match_points:
                        dist = (
                            (symbol_x - wire_pt_tup[0]) ** 2 + (symbol_y - wire_pt_tup[1]) ** 2
                        ) ** 0.5
                        if dist < 10.0:  # 10mm proximity threshold
                            connections.append({"component": ref, "pin": "unknown"})
                            break  # Only add once per component

            logger.info(f"Found {len(connections)} connections for net '{net_name}'")
            return connections

        except Exception as e:  # API boundary; bucket: catch + return
            logger.exception(f"Error getting net connections: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return []
