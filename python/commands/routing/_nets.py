"""Net listing / query / netclass commands for RoutingCommands.

Split out of the former monolithic commands/routing.py."""

import logging
import math
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple

import pcbnew

from ._helpers import _point_to_segment_distance_nm, _refuse_with_obstacles

logger = logging.getLogger("kicad_interface")


class NetMixin:
    def get_nets_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get a list of all nets in the PCB"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            nets = []
            netinfo = self.board.GetNetInfo()
            for net_code in range(netinfo.GetNetCount()):
                net = netinfo.GetNetItem(net_code)
                if net:
                    nets.append(
                        {
                            "name": net.GetNetname(),
                            "code": net.GetNetCode(),
                            "class": net.GetNetClassName(),
                        }
                    )

            from utils.pagination import paginate

            nets, page = paginate(nets, params)
            return {"success": True, "nets": nets, **page}

        except Exception as e:
            logger.error(f"Error getting nets list: {str(e)}")
            return {
                "success": False,
                "message": "Failed to get nets list",
                "errorDetails": str(e),
            }

    def query_traces(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Query traces by net, layer, or bounding box"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            # Get filter parameters
            net_name = params.get("net")
            layer = params.get("layer")
            bbox = params.get("boundingBox")  # {x1, y1, x2, y2, unit}
            include_vias = params.get("includeVias", False)

            scale = 1000000  # nm to mm conversion factor
            traces = []
            vias = []

            # Process tracks
            for track in list(self.board.Tracks()):
                try:
                    # Check if it's a via
                    is_via = track.Type() == pcbnew.PCB_VIA_T

                    if is_via and not include_vias:
                        continue

                    # Filter by net
                    if net_name and track.GetNetname() != net_name:
                        continue

                    # Filter by layer (only for tracks, not vias)
                    if layer and not is_via:
                        layer_id = self.board.GetLayerID(layer)
                        if track.GetLayer() != layer_id:
                            continue

                    # Filter by bounding box
                    if bbox:
                        bbox_unit = bbox.get("unit", "mm")
                        bbox_scale = (
                            scale
                            if bbox_unit == "mm"
                            else (25400 if bbox_unit == "mil" else 25400000)
                        )
                        x1 = int(bbox.get("x1", 0) * bbox_scale)
                        y1 = int(bbox.get("y1", 0) * bbox_scale)
                        x2 = int(bbox.get("x2", 0) * bbox_scale)
                        y2 = int(bbox.get("y2", 0) * bbox_scale)

                        if is_via:
                            pos = track.GetPosition()
                            if not (x1 <= pos.x <= x2 and y1 <= pos.y <= y2):
                                continue
                        else:
                            start = track.GetStart()
                            end = track.GetEnd()
                            # Check if either endpoint is within bbox
                            start_in = x1 <= start.x <= x2 and y1 <= start.y <= y2
                            end_in = x1 <= end.x <= x2 and y1 <= end.y <= y2
                            if not (start_in or end_in):
                                continue

                    if is_via:
                        pos = track.GetPosition()
                        vias.append(
                            {
                                "uuid": track.m_Uuid.AsString(),
                                "position": {
                                    "x": pos.x / scale,
                                    "y": pos.y / scale,
                                    "unit": "mm",
                                },
                                "net": track.GetNetname(),
                                "netCode": track.GetNetCode(),
                                "diameter": track.GetWidth() / scale,
                                "drill": track.GetDrillValue() / scale,
                            }
                        )
                    else:
                        start = track.GetStart()
                        end = track.GetEnd()
                        traces.append(
                            {
                                "uuid": track.m_Uuid.AsString(),
                                "net": track.GetNetname(),
                                "netCode": track.GetNetCode(),
                                "layer": self.board.GetLayerName(track.GetLayer()),
                                "width": track.GetWidth() / scale,
                                "start": {
                                    "x": start.x / scale,
                                    "y": start.y / scale,
                                    "unit": "mm",
                                },
                                "end": {
                                    "x": end.x / scale,
                                    "y": end.y / scale,
                                    "unit": "mm",
                                },
                                "length": track.GetLength() / scale,
                            }
                        )
                except Exception as track_err:
                    logger.warning(f"Skipping invalid track object: {track_err}")
                    continue

            from utils.pagination import paginate

            traces, page = paginate(traces, params)
            result = {"success": True, "traceCount": page["total"], "traces": traces, **page}

            if include_vias:
                result["viaCount"] = len(vias)
                result["vias"] = vias

            return result

        except Exception as e:
            logger.error(f"Error querying traces: {str(e)}")
            return {
                "success": False,
                "message": "Failed to query traces",
                "errorDetails": str(e),
            }

    def create_netclass(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new net class with specified properties"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            name = params.get("name")
            clearance = params.get("clearance")
            # Schema exposes "traceWidth"; older callers may send "trackWidth". Accept both.
            track_width = params.get("traceWidth", params.get("trackWidth"))
            via_diameter = params.get("viaDiameter")
            via_drill = params.get("viaDrill")
            uvia_diameter = params.get("uviaDiameter")
            uvia_drill = params.get("uviaDrill")
            diff_pair_width = params.get("diffPairWidth")
            diff_pair_gap = params.get("diffPairGap")
            nets = params.get("nets", [])

            if not name:
                return {
                    "success": False,
                    "message": "Missing netclass name",
                    "errorDetails": "name parameter is required",
                }

            # Get net classes — KiCad 6/7 returns NETCLASSES with .Find/.Add;
            # KiCad 9/10 returns a netclasses_map (SWIG-wrapped std::map) that is dict-like.
            net_classes = self.board.GetNetClasses()

            existing = None
            if hasattr(net_classes, "Find"):
                existing = net_classes.Find(name)
            else:
                try:
                    if name in net_classes:
                        existing = net_classes[name]
                except Exception:
                    existing = None

            if existing is None:
                netclass = pcbnew.NETCLASS(name)
                if hasattr(net_classes, "Add"):
                    net_classes.Add(netclass)
                else:
                    net_classes[name] = netclass
            else:
                netclass = existing

            # Set properties
            scale = 1000000  # mm to nm

            # Defensive setters — KiCad 10's NETCLASS dropped some legacy mutators.
            def _safe_set(method_name, value):
                if value is None:
                    return
                method = getattr(netclass, method_name, None)
                if method is None:
                    return
                try:
                    method(int(value * scale))
                except Exception:
                    pass

            _safe_set("SetClearance", clearance)
            _safe_set("SetTrackWidth", track_width)
            _safe_set("SetViaDiameter", via_diameter)
            _safe_set("SetViaDrill", via_drill)
            _safe_set("SetMicroViaDiameter", uvia_diameter)
            _safe_set("SetMicroViaDrill", uvia_drill)
            _safe_set("SetDiffPairWidth", diff_pair_width)
            _safe_set("SetDiffPairGap", diff_pair_gap)

            # Add nets to net class
            netinfo = self.board.GetNetInfo()
            nets_map = netinfo.NetsByName()
            for net_name in nets:
                if nets_map.has_key(net_name):
                    net = nets_map[net_name]
                    net.SetClass(netclass)

            # Defensive accessors — KiCad 10's NETCLASS dropped some legacy getters.
            def _safe_get(method_name):
                method = getattr(netclass, method_name, None)
                if method is None:
                    return None
                try:
                    return method() / scale
                except Exception:
                    return None

            return {
                "success": True,
                "message": f"Created net class: {name}",
                "netClass": {
                    "name": name,
                    "clearance": _safe_get("GetClearance"),
                    "trackWidth": _safe_get("GetTrackWidth"),
                    "viaDiameter": _safe_get("GetViaDiameter"),
                    "viaDrill": _safe_get("GetViaDrill"),
                    "uviaDiameter": _safe_get("GetMicroViaDiameter"),
                    "uviaDrill": _safe_get("GetMicroViaDrill"),
                    "diffPairWidth": _safe_get("GetDiffPairWidth"),
                    "diffPairGap": _safe_get("GetDiffPairGap"),
                    "nets": nets,
                },
            }

        except Exception as e:
            logger.error(f"Error creating net class: {str(e)}")
            return {
                "success": False,
                "message": "Failed to create net class",
                "errorDetails": str(e),
            }
