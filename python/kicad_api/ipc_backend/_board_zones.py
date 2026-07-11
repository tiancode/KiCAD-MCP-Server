"""IPCBoardAPI copper-zone operations.

Split out of the former monolithic kicad_api/ipc_backend.py.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("kicad_interface")


class _ZoneMixin:
    def add_zone(
        self,
        points: List[Dict[str, float]],
        layer: str = "F.Cu",
        net_name: Optional[str] = None,
        clearance: float = 0.5,
        min_thickness: float = 0.25,
        priority: int = 0,
        fill_mode: str = "solid",
        name: str = "",
    ) -> bool:
        """
        Add a copper pour zone to the board.

        The zone appears immediately in the KiCAD UI.

        Args:
            points: List of points defining the zone outline, e.g. [{"x": 0, "y": 0}, ...]
            layer: Layer name (F.Cu, B.Cu, etc.)
            net_name: Net to connect the zone to (e.g., "GND")
            clearance: Clearance from other copper in mm
            min_thickness: Minimum copper thickness in mm
            priority: Zone priority (higher = fills first)
            fill_mode: "solid" or "hatched"
            name: Optional zone name
        """
        try:
            from kipy.board_types import Zone, ZoneType
            from kipy.geometry import PolyLine, PolyLineNode
            from kipy.proto.board.board_types_pb2 import BoardLayer, ZoneFillMode
            from kipy.util.units import from_mm

            board = self._get_board()

            if len(points) < 3:
                logger.error("Zone requires at least 3 points")
                return False

            # Create zone
            zone = Zone()
            zone.type = ZoneType.ZT_COPPER

            # Set layer
            layer_map = {
                "F.Cu": BoardLayer.BL_F_Cu,
                "B.Cu": BoardLayer.BL_B_Cu,
                "In1.Cu": BoardLayer.BL_In1_Cu,
                "In2.Cu": BoardLayer.BL_In2_Cu,
                "In3.Cu": BoardLayer.BL_In3_Cu,
                "In4.Cu": BoardLayer.BL_In4_Cu,
            }
            zone.layers = [layer_map.get(layer, BoardLayer.BL_F_Cu)]

            # Set net if specified
            if net_name:
                nets = board.get_nets()
                for net in nets:
                    if net.name == net_name:
                        zone.net = net
                        break

            # Set zone properties
            zone.clearance = from_mm(clearance)
            zone.min_thickness = from_mm(min_thickness)
            zone.priority = priority

            if name:
                zone.name = name

            # Set fill mode.  kipy 10 made Zone.fill_mode getter-only, so
            # assign the underlying proto enum directly (the old
            # `zone.fill_mode = ...` raised "property has no setter" and
            # every copper pour silently failed).
            zone._proto.copper_settings.fill_mode = (
                ZoneFillMode.ZFM_HATCHED if fill_mode == "hatched" else ZoneFillMode.ZFM_SOLID
            )

            # Create outline polyline
            outline = PolyLine()
            outline.closed = True

            for point in points:
                x = point.get("x", 0)
                y = point.get("y", 0)
                node = PolyLineNode.from_xy(from_mm(x), from_mm(y))
                outline.append(node)

            # Set the outline on the zone
            # Note: Zone outline is set via the proto directly since kipy
            # doesn't expose a direct setter for creating new zones
            zone._proto.outline.polygons.add()
            zone._proto.outline.polygons[0].outline.CopyFrom(outline._proto)

            self._apply_create(board, zone, f"Added copper zone on {layer}")

            self._notify(
                "zone_added",
                {"layer": layer, "net": net_name, "points": len(points), "priority": priority},
            )

            logger.info(f"Added zone on {layer} with {len(points)} points")
            return True

        except Exception as e:
            logger.error(f"Failed to add zone: {e}")
            return False

    def get_zones(self) -> List[Dict[str, Any]]:
        """Get all zones on the board."""
        try:
            board = self._get_board()
            zones = board.get_zones()

            result = []
            for zone in zones:
                try:
                    result.append(
                        {
                            "name": zone.name if hasattr(zone, "name") else "",
                            "net": zone.net.name if zone.net else "",
                            "priority": zone.priority if hasattr(zone, "priority") else 0,
                            "layers": (
                                [str(l) for l in zone.layers] if hasattr(zone, "layers") else []
                            ),
                            "filled": zone.filled if hasattr(zone, "filled") else False,
                            "id": str(zone.id) if hasattr(zone, "id") else "",
                        }
                    )
                except Exception as e:
                    logger.warning(f"Error processing zone: {e}")
                    continue

            return result

        except Exception as e:
            logger.error(f"Failed to get zones: {e}")
            return []

    def refill_zones(self) -> bool:
        """Refill all copper pour zones."""
        try:
            board = self._get_board()
            board.refill_zones()
            self._notify("zones_refilled", {})
            return True
        except Exception as e:
            logger.error(f"Failed to refill zones: {e}")
            return False
