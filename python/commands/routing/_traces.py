"""Trace / net routing commands for RoutingCommands.

Split out of the former monolithic commands/routing.py."""

import logging
import math
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple

import pcbnew

from ._helpers import _point_to_segment_distance_nm, _refuse_with_obstacles

logger = logging.getLogger("kicad_interface")


class TraceMixin:
    def add_net(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add a new net to the PCB"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            name = params.get("name")
            net_class = params.get("class")

            if not name:
                return {
                    "success": False,
                    "message": "Missing net name",
                    "errorDetails": "name parameter is required",
                }

            # Create new net
            netinfo = self.board.GetNetInfo()
            nets_map = netinfo.NetsByName()
            if nets_map.has_key(name):
                net = nets_map[name]
            else:
                net = pcbnew.NETINFO_ITEM(self.board, name)
                self.board.Add(net)

            # Set net class if provided — defensive against KiCad 6/7 vs KiCad 9/10 API.
            if net_class:
                net_classes = self.board.GetNetClasses()
                resolved = None
                if hasattr(net_classes, "Find"):
                    resolved = net_classes.Find(net_class)
                else:
                    try:
                        if net_class in net_classes:
                            resolved = net_classes[net_class]
                    except Exception:
                        resolved = None
                if resolved is not None:
                    net.SetClass(resolved)

            return {
                "success": True,
                "message": f"Added net: {name}",
                "net": {
                    "name": name,
                    "class": net_class if net_class else "Default",
                    "netcode": net.GetNetCode(),
                },
            }

        except Exception as e:
            logger.error(f"Error adding net: {str(e)}")
            return {
                "success": False,
                "message": "Failed to add net",
                "errorDetails": str(e),
            }

    def route_pad_to_pad(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Insert ONE straight trace segment between two component pads.

        Despite the name, this is not an autorouter — it places a single
        straight line (plus a via when the pads sit on different copper
        layers).  When that line crosses a third pad (detected here by
        bbox-vs-segment intersection), the tool refuses to insert the
        trace by default; pass ``force=True`` to opt into the legacy
        "insert anyway, return warnings" behaviour.

        Looks up pad positions automatically.  Convenience wrapper around
        route_trace that eliminates the need for separate
        get_pad_position calls.
        """
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            from_ref = params.get("fromRef")
            from_pad = str(params.get("fromPad", ""))
            to_ref = params.get("toRef")
            to_pad = str(params.get("toPad", ""))
            layer = params.get("layer", "F.Cu")
            width = params.get("width")
            net = params.get("net")  # optional override
            force = bool(params.get("force", False))

            if not from_ref or not from_pad or not to_ref or not to_pad:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "fromRef, fromPad, toRef, toPad are all required",
                }

            scale = 1000000  # nm to mm

            # Find pads
            footprints = {fp.GetReference(): fp for fp in self.board.GetFootprints()}

            for ref in [from_ref, to_ref]:
                if ref not in footprints:
                    return {
                        "success": False,
                        "message": f"Component not found: {ref}",
                        "errorDetails": f"'{ref}' does not exist on the board",
                    }

            def find_pad(ref: str, pad_num: str) -> Any:
                fp = footprints[ref]
                for pad in fp.Pads():
                    if pad.GetNumber() == pad_num:
                        return pad
                return None

            start_pad = find_pad(from_ref, from_pad)
            end_pad = find_pad(to_ref, to_pad)

            if not start_pad:
                return {
                    "success": False,
                    "message": f"Pad not found: {from_ref} pad {from_pad}",
                    "errorDetails": f"Check pad number for {from_ref}",
                }
            if not end_pad:
                return {
                    "success": False,
                    "message": f"Pad not found: {to_ref} pad {to_pad}",
                    "errorDetails": f"Check pad number for {to_ref}",
                }

            start_pos = start_pad.GetPosition()
            end_pos = end_pad.GetPosition()

            # Use net from start pad if not overridden
            if not net:
                net = start_pad.GetNetname() or end_pad.GetNetname() or ""

            # Pick netclass-aware default width when caller didn't specify.
            # GetCurrentTrackWidth() returns whatever happens to be selected
            # in design settings, which is usually 0.2mm on a fresh board —
            # too thin for THT power nets and was the user's main complaint.
            if width is None:
                width = self._netclass_track_width_mm(start_pad)

            # Detect if pads are on different copper layers → need via.
            # SMD pad.GetLayer() reports F.Cu even on flipped B.Cu footprints in
            # KiCAD 9 SWIG. Use footprint.GetLayer() instead — it always reflects
            # the actual placed layer after Flip().
            fp_start = footprints[from_ref]
            fp_end = footprints[to_ref]
            start_layer = self.board.GetLayerName(fp_start.GetLayer())
            end_layer = self.board.GetLayerName(fp_end.GetLayer())
            copper_layers = {"F.Cu", "B.Cu"}
            needs_via = (
                start_layer in copper_layers
                and end_layer in copper_layers
                and start_layer != end_layer
            )

            # Endpoint pads are excluded from obstacle reporting by
            # (ref, pad_num).  `id(pad)` of the SWIG proxy does NOT
            # work because every fp.Pads() iteration creates fresh
            # Python proxies for the same underlying C++ pads, so the
            # IDs we collected from find_pad would never match the IDs
            # the obstacle scanner sees — that's why every trace
            # previously reported its own start/end pads as obstacles,
            # drowning out the real crossings the user actually needed
            # to see.  The via and same-layer branches build their own
            # exclude sets because the via case excludes only ONE
            # endpoint from each leg.

            if needs_via:
                # Place via directly below the start pad (same X).
                # Using the geometric midpoint X causes all vias to stack at
                # the same X when pads are back-to-back mirrored (e.g. J1/J2
                # on F.Cu/B.Cu): midpoint is always the board center.
                via_x = start_pos.x / scale
                via_y = (start_pos.y + end_pos.y) / 2 / scale
                # Plain duck-typed point — _pads_intersecting_segment
                # only reads .x / .y, so we don't need pcbnew.VECTOR2I.
                via_pt = SimpleNamespace(x=start_pos.x, y=(start_pos.y + end_pos.y) / 2)

                # Obstacle checks run per-actual-leg, not on the imaginary
                # direct line — start→via is vertical on start_layer, via→end
                # is diagonal on end_layer; the diagonal direct line is
                # neither of them.  Only the start pad is excluded from
                # leg 1 (the end pad isn't on this layer's path); only the
                # end pad from leg 2.
                obstacle_warnings = self._pads_intersecting_segment(
                    start_pos, via_pt, exclude_pad_keys={(from_ref, from_pad)}
                ) + self._pads_intersecting_segment(
                    via_pt, end_pos, exclude_pad_keys={(to_ref, to_pad)}
                )

                # Refuse before mutating the board if either leg crosses
                # a third pad — those crossings produce real DRC errors
                # (tracks_crossing + net-shorting) and the user has no
                # way to see the warning until they re-export the gerber.
                if obstacle_warnings and not force:
                    return _refuse_with_obstacles(
                        from_ref, from_pad, to_ref, to_pad, obstacle_warnings
                    )

                # Trace on start layer: start_pad → via
                r1 = self.route_trace(
                    {
                        "start": {"x": start_pos.x / scale, "y": start_pos.y / scale, "unit": "mm"},
                        "end": {"x": via_x, "y": via_y, "unit": "mm"},
                        "layer": start_layer,
                        "width": width,
                        "net": net,
                    }
                )
                # Via connecting both layers
                self.add_via(
                    {
                        "position": {"x": via_x, "y": via_y, "unit": "mm"},
                        "net": net,
                        "from_layer": start_layer,
                        "to_layer": end_layer,
                    }
                )
                # Trace on end layer: via → end_pad
                r2 = self.route_trace(
                    {
                        "start": {"x": via_x, "y": via_y, "unit": "mm"},
                        "end": {"x": end_pos.x / scale, "y": end_pos.y / scale, "unit": "mm"},
                        "layer": end_layer,
                        "width": width,
                        "net": net,
                    }
                )
                success = r1.get("success") and r2.get("success")
                result = {
                    "success": success,
                    "message": f"Routed {from_ref}.{from_pad} → via → {to_ref}.{to_pad} (net: {net}, via at {via_x:.2f},{via_y:.2f})",
                    "via_added": True,
                    "via_position": {"x": via_x, "y": via_y},
                }
            else:
                # Same layer — direct trace.  Exclude both endpoints; only
                # genuinely-crossed third-party pads remain in the warning.
                obstacle_warnings = self._pads_intersecting_segment(
                    start_pos,
                    end_pos,
                    exclude_pad_keys={(from_ref, from_pad), (to_ref, to_pad)},
                )
                if obstacle_warnings and not force:
                    return _refuse_with_obstacles(
                        from_ref, from_pad, to_ref, to_pad, obstacle_warnings
                    )
                result = self.route_trace(
                    {
                        "start": {"x": start_pos.x / scale, "y": start_pos.y / scale, "unit": "mm"},
                        "end": {"x": end_pos.x / scale, "y": end_pos.y / scale, "unit": "mm"},
                        "layer": layer if layer else start_layer,
                        "width": width,
                        "net": net,
                    }
                )

            if result.get("success"):
                result["fromPad"] = {
                    "ref": from_ref,
                    "pad": from_pad,
                    "x": start_pos.x / scale,
                    "y": start_pos.y / scale,
                }
                result["toPad"] = {
                    "ref": to_ref,
                    "pad": to_pad,
                    "x": end_pos.x / scale,
                    "y": end_pos.y / scale,
                }
                if obstacle_warnings:
                    result.setdefault("warnings", []).extend(obstacle_warnings)
                    result["obstaclesCrossed"] = obstacle_warnings
                    # Headline count so the agent doesn't have to scan a
                    # giant warnings array to know whether this trace is
                    # likely DRC-clean.  Same-net pads are tagged
                    # separately because crossing same-net pads is
                    # usually harmless (the trace still belongs to that
                    # net), while different-net crossings will short.
                    result["obstacleCount"] = len(obstacle_warnings)

            return result

        except Exception as e:
            logger.error(f"Error in route_pad_to_pad: {str(e)}")
            return {
                "success": False,
                "message": "Failed to route pad to pad",
                "errorDetails": str(e),
            }

    def route_trace(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Route a trace between two points or pads"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            start = params.get("start")
            end = params.get("end")
            layer = params.get("layer", "F.Cu")
            width = params.get("width")
            net = params.get("net")
            via = params.get("via", False)

            if not start or not end:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "start and end points are required",
                }

            # Get layer ID
            layer_id = self.board.GetLayerID(layer)
            if layer_id < 0:
                return {
                    "success": False,
                    "message": "Invalid layer",
                    "errorDetails": f"Layer '{layer}' does not exist",
                }

            # Get start point
            start_point = self._get_point(start)
            end_point = self._get_point(end)

            # Create track segment
            track = pcbnew.PCB_TRACK(self.board)
            track.SetStart(start_point)
            track.SetEnd(end_point)
            track.SetLayer(layer_id)

            # Set width (default to board's current track width)
            if width:
                track.SetWidth(int(width * 1000000))  # Convert mm to nm
            else:
                track.SetWidth(self.board.GetDesignSettings().GetCurrentTrackWidth())

            # Set net if provided
            if net:
                netinfo = self.board.GetNetInfo()
                nets_map = netinfo.NetsByName()
                if nets_map.has_key(net):
                    net_obj = nets_map[net]
                    track.SetNet(net_obj)

            # Add track to board
            self.board.Add(track)

            # Add via if requested and net is specified
            if via and net:
                via_point = end_point
                self.add_via(
                    {
                        "position": {
                            "x": via_point.x / 1000000,
                            "y": via_point.y / 1000000,
                            "unit": "mm",
                        },
                        "net": net,
                    }
                )

            return {
                "success": True,
                "message": "Added trace",
                "trace": {
                    "start": {
                        "x": start_point.x / 1000000,
                        "y": start_point.y / 1000000,
                        "unit": "mm",
                    },
                    "end": {
                        "x": end_point.x / 1000000,
                        "y": end_point.y / 1000000,
                        "unit": "mm",
                    },
                    "layer": layer,
                    "width": track.GetWidth() / 1000000,
                    "net": net,
                },
            }

        except Exception as e:
            logger.error(f"Error routing trace: {str(e)}")
            return {
                "success": False,
                "message": "Failed to route trace",
                "errorDetails": str(e),
            }

    def route_arc_trace(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Route a copper arc trace from start/mid/end points."""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            start = params.get("start")
            mid = params.get("mid")
            end = params.get("end")
            layer = params.get("layer", "F.Cu")
            width = params.get("width")
            net = params.get("net")

            if not start or not mid or not end:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "start, mid and end points are required",
                }

            layer_id = self.board.GetLayerID(layer)
            if layer_id < 0:
                return {
                    "success": False,
                    "message": "Invalid layer",
                    "errorDetails": f"Layer '{layer}' does not exist",
                }

            start_point = self._get_point(start)
            mid_point = self._get_point(mid)
            end_point = self._get_point(end)

            arc = pcbnew.PCB_ARC(self.board)
            arc.SetStart(start_point)
            arc.SetMid(mid_point)
            arc.SetEnd(end_point)
            arc.SetLayer(layer_id)

            if width:
                arc.SetWidth(int(width * 1000000))
            else:
                arc.SetWidth(self.board.GetDesignSettings().GetCurrentTrackWidth())

            if net:
                netinfo = self.board.GetNetInfo()
                nets_map = netinfo.NetsByName()
                if nets_map.has_key(net):
                    arc.SetNet(nets_map[net])

            self.board.Add(arc)

            return {
                "success": True,
                "message": "Added arc trace",
                "arc": {
                    "start": {
                        "x": start_point.x / 1000000,
                        "y": start_point.y / 1000000,
                        "unit": "mm",
                    },
                    "mid": {"x": mid_point.x / 1000000, "y": mid_point.y / 1000000, "unit": "mm"},
                    "end": {"x": end_point.x / 1000000, "y": end_point.y / 1000000, "unit": "mm"},
                    "layer": layer,
                    "width": arc.GetWidth() / 1000000,
                    "net": net,
                },
            }
        except Exception as e:
            logger.error(f"Error routing arc trace: {str(e)}")
            return {
                "success": False,
                "message": "Failed to route arc trace",
                "errorDetails": str(e),
            }

    def delete_trace(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delete a trace from the PCB"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            trace_uuid = params.get("traceUuid")
            position = params.get("position")
            net_name = params.get("net")
            layer = params.get("layer")
            include_vias = params.get("includeVias", False)

            if not trace_uuid and not position and not net_name:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "One of traceUuid, position, or net must be provided",
                }

            # NOTE on Remove vs Delete: the KiCAD 10 SWIG bindings leak the
            # detached C++ object on board.Remove() ("memory leak of type
            # 'PCB_TRACK *', no destructor found") and the dangling wrapper can
            # corrupt the SWIG object table, after which every later board call
            # returns a raw SwigPyObject or segfaults. board.Delete() frees the
            # object cleanly, so we Delete throughout this method.

            # 1) Delete by UUID (most specific).
            if trace_uuid:
                track = None
                for item in list(self.board.Tracks()):
                    if item.m_Uuid.AsString() == trace_uuid:
                        track = item
                        break

                if not track:
                    return {
                        "success": False,
                        "message": "Track not found",
                        "errorDetails": f"Could not find track with UUID: {trace_uuid}",
                    }

                self.board.Delete(track)
                track = None
                self.board.SetModified()
                return {"success": True, "message": f"Deleted track: {trace_uuid}"}

            # 2) Delete the SINGLE nearest item to a position.
            #
            # Position takes precedence over the bulk `net` delete below: passing
            # position + net means "delete the nearest trace ON that net" (one
            # item), NOT "bulk-delete the entire net". The old order treated any
            # net argument as bulk and silently wiped a fully-routed net when a
            # caller passed both — a data-loss footgun.
            if position:
                scale = (
                    1000000
                    if position.get("unit") == "mm"
                    else (25400 if position.get("unit") == "mil" else 25400000)
                )  # mm, mil, or inch to nm
                x_nm = int(position["x"] * scale)
                y_nm = int(position["y"] * scale)
                point = pcbnew.VECTOR2I(x_nm, y_nm)

                closest_track = None
                min_distance = float("inf")
                for track in list(self.board.Tracks()):
                    is_via = track.Type() == pcbnew.PCB_VIA_T
                    # Honour the same filters as the bulk path so callers can
                    # target "the nearest VIN trace" or "the via here".
                    if is_via and not include_vias:
                        continue
                    if net_name and net_name != "*" and track.GetNetname() != net_name:
                        continue
                    if layer and not is_via:
                        if track.GetLayer() != self.board.GetLayerID(layer):
                            continue
                    dist = self._point_to_track_distance(point, track)
                    if dist < min_distance:
                        min_distance = dist
                        closest_track = track

                if closest_track is not None and min_distance < 1000000:  # within 1mm
                    was_via = closest_track.Type() == pcbnew.PCB_VIA_T
                    self.board.Delete(closest_track)
                    closest_track = None
                    self.board.SetModified()
                    return {
                        "success": True,
                        "message": f"Deleted {'via' if was_via else 'track'} at specified position",
                    }
                else:
                    return {
                        "success": False,
                        "message": "No track found",
                        "errorDetails": "No track/via found near specified position matching the filters",
                    }

            # 3) Bulk delete by net name (only when neither uuid nor position
            #    was given). Use "*" to delete every track.
            if net_name:
                tracks_to_remove = []
                for track in list(self.board.Tracks()):
                    if net_name != "*" and track.GetNetname() != net_name:
                        continue

                    # Skip vias if not requested
                    is_via = track.Type() == pcbnew.PCB_VIA_T
                    if is_via and not include_vias:
                        continue

                    # Filter by layer if specified (only for non-vias)
                    if layer and not is_via:
                        layer_id = self.board.GetLayerID(layer)
                        if track.GetLayer() != layer_id:
                            continue

                    tracks_to_remove.append(track)

                deleted_count = len(tracks_to_remove)
                for track in tracks_to_remove:
                    self.board.Delete(track)
                tracks_to_remove.clear()
                self.board.SetModified()

                return {
                    "success": True,
                    "message": f"Deleted {deleted_count} traces on net '{net_name}'",
                    "deletedCount": deleted_count,
                }

            # No valid parameters provided
            return {
                "success": False,
                "message": "No valid search parameter provided",
                "errorDetails": "Provide traceUuid, position, or net parameter",
            }

        except Exception as e:
            logger.error(f"Error deleting trace: {str(e)}")
            return {
                "success": False,
                "message": "Failed to delete trace",
                "errorDetails": str(e),
            }

    def modify_trace(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Modify properties of an existing trace

        Allows changing trace width, layer, and net assignment.
        Find trace by UUID or position.
        """
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            # Identification parameters
            trace_uuid = params.get("uuid")
            position = params.get("position")  # {x, y, unit}

            # Modification parameters
            new_width = params.get("width")  # in mm
            new_layer = params.get("layer")
            new_net = params.get("net")

            if not trace_uuid and not position:
                return {
                    "success": False,
                    "message": "Missing trace identifier",
                    "errorDetails": "Provide either 'uuid' or 'position' to identify the trace",
                }

            scale = 1000000  # nm to mm conversion

            # Find the track
            track = None

            if trace_uuid:
                for item in list(self.board.Tracks()):
                    if item.m_Uuid.AsString() == trace_uuid:
                        track = item
                        break
            elif position:
                pos_unit = position.get("unit", "mm")
                pos_scale = (
                    scale if pos_unit == "mm" else (25400 if pos_unit == "mil" else 25400000)
                )
                x_nm = int(position["x"] * pos_scale)
                y_nm = int(position["y"] * pos_scale)
                point = pcbnew.VECTOR2I(x_nm, y_nm)

                # Find closest track
                min_distance = float("inf")
                for item in list(self.board.Tracks()):
                    dist = self._point_to_track_distance(point, item)
                    if dist < min_distance:
                        min_distance = dist
                        track = item

                # Only accept if within 1mm
                if min_distance >= 1000000:
                    track = None

            if not track:
                return {
                    "success": False,
                    "message": "Track not found",
                    "errorDetails": "Could not find track with specified identifier",
                }

            # Check if it's a via (some modifications don't apply)
            is_via = track.Type() == pcbnew.PCB_VIA_T
            modifications = []

            # Apply modifications
            if new_width is not None:
                width_nm = int(new_width * scale)
                track.SetWidth(width_nm)
                modifications.append(f"width={new_width}mm")

            if new_layer and not is_via:
                layer_id = self.board.GetLayerID(new_layer)
                if layer_id < 0:
                    return {
                        "success": False,
                        "message": "Invalid layer",
                        "errorDetails": f"Layer '{new_layer}' not found",
                    }
                track.SetLayer(layer_id)
                modifications.append(f"layer={new_layer}")

            if new_net:
                netinfo = self.board.GetNetInfo()
                net = netinfo.GetNetItem(new_net)
                if not net:
                    return {
                        "success": False,
                        "message": "Invalid net",
                        "errorDetails": f"Net '{new_net}' not found",
                    }
                track.SetNet(net)
                modifications.append(f"net={new_net}")

            if not modifications:
                return {
                    "success": False,
                    "message": "No modifications specified",
                    "errorDetails": "Provide at least one of: width, layer, net",
                }

            return {
                "success": True,
                "message": f"Modified trace: {', '.join(modifications)}",
                "uuid": track.m_Uuid.AsString(),
                "modifications": modifications,
            }

        except Exception as e:
            logger.error(f"Error modifying trace: {str(e)}")
            return {
                "success": False,
                "message": "Failed to modify trace",
                "errorDetails": str(e),
            }

    def copy_routing_pattern(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Copy routing pattern from source components to target components

        This enables routing replication between identical component groups.
        The pattern is copied with a translation offset calculated from
        the position difference between source and target components.
        """
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            source_refs = params.get("sourceRefs", [])  # e.g., ["U1", "U2", "U3"]
            target_refs = params.get("targetRefs", [])  # e.g., ["U4", "U5", "U6"]
            include_vias = params.get("includeVias", True)
            trace_width = params.get("traceWidth")  # Optional override

            if not source_refs or not target_refs:
                return {
                    "success": False,
                    "message": "Missing component references",
                    "errorDetails": "Provide both 'sourceRefs' and 'targetRefs' arrays",
                }

            if len(source_refs) != len(target_refs):
                return {
                    "success": False,
                    "message": "Mismatched component counts",
                    "errorDetails": f"sourceRefs has {len(source_refs)} items, targetRefs has {len(target_refs)}",
                }

            scale = 1000000  # nm to mm conversion

            # Get footprints
            footprints = {fp.GetReference(): fp for fp in self.board.GetFootprints()}

            # Validate all references exist
            for ref in source_refs + target_refs:
                if ref not in footprints:
                    return {
                        "success": False,
                        "message": "Component not found",
                        "errorDetails": f"Component '{ref}' not found on board",
                    }

            # Calculate offset from first source to first target component
            source_fp = footprints[source_refs[0]]
            target_fp = footprints[target_refs[0]]
            source_pos = source_fp.GetPosition()
            target_pos = target_fp.GetPosition()

            offset_x = target_pos.x - source_pos.x
            offset_y = target_pos.y - source_pos.y

            # Collect all nets connected to source components
            source_nets = set()
            source_pad_positions = []  # (x, y) in nm for geometric fallback
            for ref in source_refs:
                fp = footprints[ref]
                for pad in fp.Pads():
                    net_name = pad.GetNetname()
                    if net_name and net_name != "":
                        source_nets.add(net_name)
                    pos = pad.GetPosition()
                    source_pad_positions.append((pos.x, pos.y))

            # Build bounding box around source pads (with 5mm tolerance in nm)
            TOLERANCE_NM = int(5 * scale)
            if source_pad_positions:
                xs = [p[0] for p in source_pad_positions]
                ys = [p[1] for p in source_pad_positions]
                bbox_x1 = min(xs) - TOLERANCE_NM
                bbox_x2 = max(xs) + TOLERANCE_NM
                bbox_y1 = min(ys) - TOLERANCE_NM
                bbox_y2 = max(ys) + TOLERANCE_NM
            else:
                # Fall back to component position ± 25mm
                sp = source_fp.GetPosition()
                bbox_x1 = sp.x - int(25 * scale)
                bbox_x2 = sp.x + int(25 * scale)
                bbox_y1 = sp.y - int(25 * scale)
                bbox_y2 = sp.y + int(25 * scale)

            def point_in_bbox(px: int, py: int) -> bool:
                return bbox_x1 <= px <= bbox_x2 and bbox_y1 <= py <= bbox_y2

            # Collect traces: by net name (if available) OR by geometric proximity
            use_net_filter = len(source_nets) > 0
            traces_to_copy = []
            vias_to_copy = []

            for track in list(self.board.Tracks()):
                is_via = track.Type() == pcbnew.PCB_VIA_T

                if use_net_filter:
                    # Primary: net-based filter
                    if track.GetNetname() not in source_nets:
                        continue
                else:
                    # Fallback: geometric filter — trace start OR end inside source bbox
                    if is_via:
                        pos = track.GetPosition()
                        if not point_in_bbox(pos.x, pos.y):
                            continue
                    else:
                        s = track.GetStart()
                        e = track.GetEnd()
                        if not (point_in_bbox(s.x, s.y) or point_in_bbox(e.x, e.y)):
                            continue

                if is_via:
                    if include_vias:
                        vias_to_copy.append(track)
                else:
                    traces_to_copy.append(track)

            filter_method = "net-based" if use_net_filter else "geometric (pads have no nets)"
            logger.info(
                f"copy_routing_pattern: {len(traces_to_copy)} traces, "
                f"{len(vias_to_copy)} vias selected via {filter_method}"
            )

            # Create new traces with offset
            created_traces = 0
            created_vias = 0

            for track in traces_to_copy:
                start = track.GetStart()
                end = track.GetEnd()

                # Create new track
                new_track = pcbnew.PCB_TRACK(self.board)
                new_track.SetStart(pcbnew.VECTOR2I(start.x + offset_x, start.y + offset_y))
                new_track.SetEnd(pcbnew.VECTOR2I(end.x + offset_x, end.y + offset_y))
                new_track.SetLayer(track.GetLayer())

                # Set width (use override or original)
                if trace_width:
                    new_track.SetWidth(int(trace_width * scale))
                else:
                    new_track.SetWidth(track.GetWidth())

                # Try to find corresponding target net
                # This is a simplification - more sophisticated mapping would be needed
                # for complex designs
                self.board.Add(new_track)
                created_traces += 1

            for via in vias_to_copy:
                pos = via.GetPosition()

                # Create new via
                new_via = pcbnew.PCB_VIA(self.board)
                new_via.SetPosition(pcbnew.VECTOR2I(pos.x + offset_x, pos.y + offset_y))
                new_via.SetWidth(via.GetWidth(pcbnew.F_Cu))
                new_via.SetDrill(via.GetDrillValue())
                new_via.SetViaType(via.GetViaType())

                self.board.Add(new_via)
                created_vias += 1

            result = {
                "success": True,
                "message": f"Copied routing pattern: {created_traces} traces, {created_vias} vias",
                "filterMethod": filter_method,
                "offset": {"x": offset_x / scale, "y": offset_y / scale, "unit": "mm"},
                "createdTraces": created_traces,
                "createdVias": created_vias,
                "sourceComponents": source_refs,
                "targetComponents": target_refs,
            }

            return result

        except Exception as e:
            logger.error(f"Error copying routing pattern: {str(e)}")
            return {
                "success": False,
                "message": "Failed to copy routing pattern",
                "errorDetails": str(e),
            }

    def route_differential_pair(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Route a differential pair between two sets of points or pads"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            start_pos = params.get("startPos")
            end_pos = params.get("endPos")
            net_pos = params.get("netPos")
            net_neg = params.get("netNeg")
            layer = params.get("layer", "F.Cu")
            width = params.get("width")
            gap = params.get("gap")

            if not start_pos or not end_pos or not net_pos or not net_neg:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "startPos, endPos, netPos, and netNeg are required",
                }

            # Get layer ID
            layer_id = self.board.GetLayerID(layer)
            if layer_id < 0:
                return {
                    "success": False,
                    "message": "Invalid layer",
                    "errorDetails": f"Layer '{layer}' does not exist",
                }

            # Get nets
            netinfo = self.board.GetNetInfo()
            nets_map = netinfo.NetsByName()

            net_pos_obj = nets_map[net_pos] if nets_map.has_key(net_pos) else None
            net_neg_obj = nets_map[net_neg] if nets_map.has_key(net_neg) else None

            if not net_pos_obj or not net_neg_obj:
                return {
                    "success": False,
                    "message": "Nets not found",
                    "errorDetails": "One or both nets specified for the differential pair do not exist",
                }

            # Get start and end points
            start_point = self._get_point(start_pos)
            end_point = self._get_point(end_pos)

            # Calculate offset vectors for the two traces
            # First, get the direction vector from start to end
            dx = end_point.x - start_point.x
            dy = end_point.y - start_point.y
            length = math.sqrt(dx * dx + dy * dy)

            if length <= 0:
                return {
                    "success": False,
                    "message": "Invalid points",
                    "errorDetails": "Start and end points must be different",
                }

            # Normalize direction vector
            dx /= length
            dy /= length

            # Get perpendicular vector
            px = -dy
            py = dx

            # Set default gap if not provided
            if gap is None:
                gap = 0.2  # mm

            # Convert to nm
            gap_nm = int(gap * 1000000)

            # Calculate offsets
            offset_x = int(px * gap_nm / 2)
            offset_y = int(py * gap_nm / 2)

            # Create positive and negative trace points
            pos_start = pcbnew.VECTOR2I(
                int(start_point.x + offset_x), int(start_point.y + offset_y)
            )
            pos_end = pcbnew.VECTOR2I(int(end_point.x + offset_x), int(end_point.y + offset_y))
            neg_start = pcbnew.VECTOR2I(
                int(start_point.x - offset_x), int(start_point.y - offset_y)
            )
            neg_end = pcbnew.VECTOR2I(int(end_point.x - offset_x), int(end_point.y - offset_y))

            # Create positive trace
            pos_track = pcbnew.PCB_TRACK(self.board)
            pos_track.SetStart(pos_start)
            pos_track.SetEnd(pos_end)
            pos_track.SetLayer(layer_id)
            pos_track.SetNet(net_pos_obj)

            # Create negative trace
            neg_track = pcbnew.PCB_TRACK(self.board)
            neg_track.SetStart(neg_start)
            neg_track.SetEnd(neg_end)
            neg_track.SetLayer(layer_id)
            neg_track.SetNet(net_neg_obj)

            # Set width
            if width:
                trace_width_nm = int(width * 1000000)
                pos_track.SetWidth(trace_width_nm)
                neg_track.SetWidth(trace_width_nm)
            else:
                # Get default width from design rules or net class
                trace_width = self.board.GetDesignSettings().GetCurrentTrackWidth()
                pos_track.SetWidth(trace_width)
                neg_track.SetWidth(trace_width)

            # Add tracks to board
            self.board.Add(pos_track)
            self.board.Add(neg_track)

            return {
                "success": True,
                "message": "Added differential pair traces",
                "diffPair": {
                    "posNet": net_pos,
                    "negNet": net_neg,
                    "layer": layer,
                    "width": pos_track.GetWidth() / 1000000,
                    "gap": gap,
                    "length": length / 1000000,
                },
            }

        except Exception as e:
            logger.error(f"Error routing differential pair: {str(e)}")
            return {
                "success": False,
                "message": "Failed to route differential pair",
                "errorDetails": str(e),
            }
