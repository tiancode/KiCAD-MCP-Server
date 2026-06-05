"""Copper zone / pour commands for RoutingCommands.

Split out of the former monolithic commands/routing.py."""

import logging
import math
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple

import pcbnew

from ._helpers import _point_to_segment_distance_nm, _refuse_with_obstacles

logger = logging.getLogger("kicad_interface")


class ZoneMixin:
    def query_zones(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Query copper zones (filled pours) by net, layer, or bounding box.

        Returns one entry per zone with its net, layers, priority, fill state,
        and bounding box. Useful for auditing power planes / GND pours that
        ``query_traces`` does not report (zones are PCB_ZONE_T, not tracks).
        """
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            net_name = params.get("net")
            layer = params.get("layer")
            bbox = params.get("boundingBox")

            scale = 1000000  # nm -> mm
            target_layer_id = None
            if layer:
                target_layer_id = self.board.GetLayerID(layer)

            bbox_box = None
            if bbox:
                bbox_unit = bbox.get("unit", "mm")
                bbox_scale = scale if bbox_unit == "mm" else 25400000
                bbox_box = (
                    int(bbox.get("x1", 0) * bbox_scale),
                    int(bbox.get("y1", 0) * bbox_scale),
                    int(bbox.get("x2", 0) * bbox_scale),
                    int(bbox.get("y2", 0) * bbox_scale),
                )

            zones_out = []
            for zone in list(self.board.Zones()):
                try:
                    z_net = zone.GetNetname()
                    if net_name and z_net != net_name:
                        continue

                    # A zone can span multiple copper layers; collect them.
                    layer_names = []
                    try:
                        layer_set = zone.GetLayerSet()
                        seq = (
                            layer_set.CuStack()
                            if hasattr(layer_set, "CuStack")
                            else layer_set.Seq()
                        )
                        for lid in seq:
                            layer_names.append(self.board.GetLayerName(lid))
                    except Exception:
                        layer_names = [self.board.GetLayerName(zone.GetLayer())]

                    if target_layer_id is not None:
                        if target_layer_id not in [self.board.GetLayerID(n) for n in layer_names]:
                            continue

                    bb = zone.GetBoundingBox()
                    bb_x1, bb_y1 = bb.GetLeft(), bb.GetTop()
                    bb_x2, bb_y2 = bb.GetRight(), bb.GetBottom()

                    if bbox_box is not None:
                        x1, y1, x2, y2 = bbox_box
                        # Reject if no overlap with filter bbox.
                        if bb_x2 < x1 or bb_x1 > x2 or bb_y2 < y1 or bb_y1 > y2:
                            continue

                    entry = {
                        "uuid": zone.m_Uuid.AsString(),
                        "net": z_net,
                        "netCode": zone.GetNetCode(),
                        "layers": layer_names,
                        "priority": (
                            zone.GetAssignedPriority()
                            if hasattr(zone, "GetAssignedPriority")
                            else 0
                        ),
                        "isFilled": bool(zone.IsFilled()),
                        "minThickness": zone.GetMinThickness() / scale,
                        "boundingBox": {
                            "x1": bb_x1 / scale,
                            "y1": bb_y1 / scale,
                            "x2": bb_x2 / scale,
                            "y2": bb_y2 / scale,
                            "unit": "mm",
                        },
                    }
                    # Area is only available when zone is filled.
                    try:
                        entry["filledArea"] = zone.GetFilledArea() / (scale * scale)
                    except Exception:
                        pass

                    zones_out.append(entry)
                except Exception as zone_err:
                    logger.warning(f"Skipping invalid zone object: {zone_err}")
                    continue

            return {
                "success": True,
                "zoneCount": len(zones_out),
                "zones": zones_out,
            }

        except Exception as e:
            logger.error(f"Error querying zones: {str(e)}")
            return {
                "success": False,
                "message": "Failed to query zones",
                "errorDetails": str(e),
            }

    def add_copper_pour(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add a copper pour (zone) to the PCB"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            layer = params.get("layer", "F.Cu")
            net = params.get("net")
            clearance = params.get("clearance")
            min_width = params.get("minWidth", 0.2)
            points = params.get("outline", params.get("points", []))
            priority = params.get("priority", 0)
            fill_type = params.get("fillType", "solid")  # solid or hatched

            # If no outline provided, use board outline
            if not points or len(points) < 3:
                board_box = self.board.GetBoardEdgesBoundingBox()
                if board_box.GetWidth() > 0 and board_box.GetHeight() > 0:
                    scale = 1000000  # nm to mm
                    x1 = board_box.GetX() / scale
                    y1 = board_box.GetY() / scale
                    x2 = (board_box.GetX() + board_box.GetWidth()) / scale
                    y2 = (board_box.GetY() + board_box.GetHeight()) / scale

                    # Detect corner radius from Edge.Cuts arcs so the zone rectangle
                    # stays inside the rounded board corners (avoids zone visually
                    # extending outside Edge.Cuts before refill)
                    corner_radius = 0.0
                    edge_layer_id = self.board.GetLayerID("Edge.Cuts")
                    for item in self.board.GetDrawings():
                        if item.GetLayer() == edge_layer_id and item.GetClass() == "PCB_ARC":
                            r = item.GetRadius() / scale
                            if r > corner_radius:
                                corner_radius = r
                    # Inset the zone rectangle by the corner radius so its corners
                    # lie on the straight portions of the board edge.
                    inset = corner_radius
                    points = [
                        {"x": x1 + inset, "y": y1 + inset},
                        {"x": x2 - inset, "y": y1 + inset},
                        {"x": x2 - inset, "y": y2 - inset},
                        {"x": x1 + inset, "y": y2 - inset},
                    ]
                else:
                    return {
                        "success": False,
                        "message": "Missing outline",
                        "errorDetails": "Provide an outline array or add a board outline first",
                    }

            # Get layer ID
            layer_id = self.board.GetLayerID(layer)
            if layer_id < 0:
                return {
                    "success": False,
                    "message": "Invalid layer",
                    "errorDetails": f"Layer '{layer}' does not exist",
                }

            # Create zone
            zone = pcbnew.ZONE(self.board)
            zone.SetLayer(layer_id)

            # Set net if provided
            if net:
                netinfo = self.board.GetNetInfo()
                nets_map = netinfo.NetsByName()
                if nets_map.has_key(net):
                    net_obj = nets_map[net]
                    zone.SetNet(net_obj)

            # Set zone properties
            scale = 1000000  # mm to nm
            zone.SetAssignedPriority(priority)

            if clearance is not None:
                zone.SetLocalClearance(int(clearance * scale))

            zone.SetMinThickness(int(min_width * scale))

            # Set fill type
            if fill_type == "hatched":
                zone.SetFillMode(pcbnew.ZONE_FILL_MODE_HATCH_PATTERN)
            else:
                zone.SetFillMode(pcbnew.ZONE_FILL_MODE_POLYGONS)

            # Create outline
            outline = zone.Outline()
            outline.NewOutline()  # Create a new outline contour first

            # Add points to outline
            for point in points:
                scale = (
                    1000000
                    if point.get("unit", "mm") == "mm"
                    else (25400 if point.get("unit", "mm") == "mil" else 25400000)
                )
                x_nm = int(point["x"] * scale)
                y_nm = int(point["y"] * scale)
                outline.Append(pcbnew.VECTOR2I(x_nm, y_nm))  # Add point to outline

            # Add zone to board
            self.board.Add(zone)

            # Fill zone
            # Note: Zone filling can cause issues with SWIG API
            # Comment out for now - zones will be filled when board is saved/opened in KiCAD
            # filler = pcbnew.ZONE_FILLER(self.board)
            # filler.Fill(self.board.Zones())

            return {
                "success": True,
                "message": "Added copper pour",
                "pour": {
                    "layer": layer,
                    "net": net,
                    "clearance": clearance,
                    "minWidth": min_width,
                    "priority": priority,
                    "fillType": fill_type,
                    "pointCount": len(points),
                },
            }

        except Exception as e:
            logger.error(f"Error adding copper pour: {str(e)}")
            return {
                "success": False,
                "message": "Failed to add copper pour",
                "errorDetails": str(e),
            }
