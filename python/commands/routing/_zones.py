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

    # ------------------------------------------------------------------
    # Zone selection shared by edit_copper_pour / delete_copper_pour
    # ------------------------------------------------------------------

    def _zone_brief(self, zone: Any) -> Dict[str, Any]:
        """Small identifying summary of a zone for disambiguation lists."""
        try:
            layer_name = self.board.GetLayerName(zone.GetLayer())
        except Exception:
            layer_name = None
        return {
            "uuid": zone.m_Uuid.AsString(),
            "net": zone.GetNetname(),
            "layer": layer_name,
            "isFilled": bool(zone.IsFilled()),
        }

    def _find_zones(
        self, uuid: Optional[str], net: Optional[str], layer: Optional[str]
    ) -> Tuple[List[Any], Optional[Dict[str, Any]]]:
        """Resolve zones by uuid (exact) or net/layer filters.

        Returns (matches, error_response). error_response is None on success.
        """
        if not self.board:
            return [], {
                "success": False,
                "message": "No board is loaded",
                "errorDetails": "Load or create a board first",
            }

        zones = list(self.board.Zones())
        if uuid:
            matches = [z for z in zones if z.m_Uuid.AsString() == uuid]
            if not matches:
                return [], {
                    "success": False,
                    "message": f"No zone with uuid {uuid}",
                    "errorDetails": "Call query_zones to list zone uuids",
                    "zones": [self._zone_brief(z) for z in zones],
                }
            return matches, None

        matches = zones
        if net is not None:
            matches = [z for z in matches if z.GetNetname() == net]
        if layer is not None:
            layer_id = self.board.GetLayerID(layer)
            matches = [z for z in matches if z.GetLayer() == layer_id]
        if not matches:
            return [], {
                "success": False,
                "message": "No zone matched the given net/layer filters",
                "errorDetails": "Call query_zones to list zones",
                "zones": [self._zone_brief(z) for z in zones],
            }
        return matches, None

    _PAD_CONNECTION_ATTRS = {
        "solid": "ZONE_CONNECTION_FULL",
        "thermal": "ZONE_CONNECTION_THERMAL",
        "none": "ZONE_CONNECTION_NONE",
        "thru_hole_only": "ZONE_CONNECTION_THT_THERMAL",
    }

    def edit_copper_pour(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Edit properties of an existing copper pour (zone).

        Select the zone by ``uuid`` (preferred, from query_zones) or by
        ``net``/``layer`` filters that must match exactly one zone.  Any of
        the optional property params then overwrite that zone's settings.
        The fill is marked stale — call refill_zones afterwards.
        """
        try:
            matches, err = self._find_zones(
                params.get("uuid"), params.get("net"), params.get("layer")
            )
            if err:
                return err
            if len(matches) > 1:
                return {
                    "success": False,
                    "message": (
                        f"{len(matches)} zones matched — refine with uuid "
                        "(from query_zones) or a net+layer pair"
                    ),
                    "zones": [self._zone_brief(z) for z in matches],
                }
            zone = matches[0]
            scale = 1000000  # mm -> nm
            changed: List[str] = []

            new_net = params.get("newNet")
            if new_net is not None:
                nets_map = self.board.GetNetInfo().NetsByName()
                if not nets_map.has_key(new_net):
                    return {
                        "success": False,
                        "message": f"Net '{new_net}' does not exist on the board",
                    }
                zone.SetNet(nets_map[new_net])
                changed.append("net")

            new_layer = params.get("newLayer")
            if new_layer is not None:
                layer_id = self.board.GetLayerID(new_layer)
                if layer_id < 0:
                    return {
                        "success": False,
                        "message": f"Layer '{new_layer}' does not exist",
                    }
                zone.SetLayer(layer_id)
                changed.append("layer")

            if params.get("clearance") is not None:
                zone.SetLocalClearance(int(params["clearance"] * scale))
                changed.append("clearance")

            if params.get("minWidth") is not None:
                zone.SetMinThickness(int(params["minWidth"] * scale))
                changed.append("minWidth")

            if params.get("priority") is not None:
                zone.SetAssignedPriority(int(params["priority"]))
                changed.append("priority")

            fill_type = params.get("fillType")
            if fill_type is not None:
                if fill_type == "hatched":
                    zone.SetFillMode(pcbnew.ZONE_FILL_MODE_HATCH_PATTERN)
                else:
                    zone.SetFillMode(pcbnew.ZONE_FILL_MODE_POLYGONS)
                changed.append("fillType")

            pad_connection = params.get("padConnection")
            if pad_connection is not None:
                attr = self._PAD_CONNECTION_ATTRS.get(pad_connection)
                const = getattr(pcbnew, attr, None) if attr else None
                if const is None:
                    return {
                        "success": False,
                        "message": (
                            f"Unknown padConnection '{pad_connection}' — use one of "
                            f"{sorted(self._PAD_CONNECTION_ATTRS)}"
                        ),
                    }
                zone.SetPadConnection(const)
                changed.append("padConnection")

            if params.get("thermalGap") is not None:
                zone.SetThermalReliefGap(int(params["thermalGap"] * scale))
                changed.append("thermalGap")

            if params.get("thermalBridgeWidth") is not None:
                zone.SetThermalReliefSpokeWidth(int(params["thermalBridgeWidth"] * scale))
                changed.append("thermalBridgeWidth")

            points = params.get("outline")
            if points:
                if len(points) < 3:
                    return {
                        "success": False,
                        "message": "outline needs at least 3 points",
                    }
                outline = zone.Outline()
                outline.RemoveAllContours()
                outline.NewOutline()
                for point in points:
                    unit_scale = (
                        1000000
                        if point.get("unit", "mm") == "mm"
                        else (25400 if point.get("unit") == "mil" else 25400000)
                    )
                    outline.Append(
                        pcbnew.VECTOR2I(int(point["x"] * unit_scale), int(point["y"] * unit_scale))
                    )
                changed.append("outline")

            if not changed:
                return {
                    "success": False,
                    "message": (
                        "No editable property given — pass one of newNet, newLayer, "
                        "clearance, minWidth, priority, fillType, padConnection, "
                        "thermalGap, thermalBridgeWidth, outline"
                    ),
                    "zone": self._zone_brief(zone),
                }

            # The stored fill no longer reflects the zone settings.
            try:
                zone.UnFill()
            except Exception:
                try:
                    zone.SetIsFilled(False)
                except Exception:
                    pass

            return {
                "success": True,
                "message": f"Edited copper pour ({', '.join(changed)})",
                "changed": changed,
                "zone": self._zone_brief(zone),
                "refillStatus": (
                    "fill marked stale — call refill_zones (or let KiCad refill "
                    "on open) before export_gerber"
                ),
            }

        except Exception as e:
            logger.error(f"Error editing copper pour: {str(e)}")
            return {
                "success": False,
                "message": "Failed to edit copper pour",
                "errorDetails": str(e),
            }

    def delete_copper_pour(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delete copper pour(s).

        Select by ``uuid`` (single zone) or ``net``/``layer`` filters.  When
        the filters match several zones, pass ``all=true`` to delete every
        match — otherwise the call is refused with the candidate list.
        """
        try:
            matches, err = self._find_zones(
                params.get("uuid"), params.get("net"), params.get("layer")
            )
            if err:
                return err
            if len(matches) > 1 and not bool(params.get("all", False)):
                return {
                    "success": False,
                    "message": (
                        f"{len(matches)} zones matched — pass all=true to delete "
                        "every match, or refine with uuid (from query_zones)"
                    ),
                    "zones": [self._zone_brief(z) for z in matches],
                }

            deleted = [self._zone_brief(z) for z in matches]
            for zone in matches:
                self.board.Remove(zone)

            return {
                "success": True,
                "message": f"Deleted {len(deleted)} copper pour(s)",
                "deleted": deleted,
            }

        except Exception as e:
            logger.error(f"Error deleting copper pour: {str(e)}")
            return {
                "success": False,
                "message": "Failed to delete copper pour",
                "errorDetails": str(e),
            }
