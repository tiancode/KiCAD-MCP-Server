"""route_smart: obstacle-avoiding pad-to-pad routing on the SWIG board.

Bridges the board to the pure grid A* core in _astar.py: extracts pads,
tracks and vias as keep-out rectangles, runs the search, then commits the
resulting segments and vias as real board objects. Unlike route_pad_to_pad
(one straight segment, refuses on obstacles) this routes AROUND obstacles
and may change layers through vias when two copper layers are given.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from ._helpers import _refuse_cross_net_short, _track_width_error, endpoint_net_conflicts
from ._nets import netclass_property, resolve_netclass_name

logger = logging.getLogger("kicad_interface")

_NM = 1_000_000  # nm per mm


class SmartRouteMixin:
    """Adds route_smart to RoutingCommands."""

    def route_smart(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Route between two pads (or points) with grid A* obstacle avoidance.

        Params: fromRef/fromPad/toRef/toPad (or start/end {x,y} mm), layers
        (1-2 copper layers, default ["F.Cu"]), width (mm), net (override),
        gridMm (default 0.25), clearance (mm, default 0.2), viaCost,
        maxNodes. Diagonal track bounding boxes over-approximate obstacles
        slightly — increase gridMm on dense boards if routes are refused.
        """
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            from ._astar import obstacles_from_board_items, route_grid_astar

            layers = params.get("layers") or [params.get("layer", "F.Cu")]
            if not isinstance(layers, list) or not 1 <= len(layers) <= 2:
                return {
                    "success": False,
                    "message": "layers must be a list of 1 or 2 copper layer names",
                    "errorCode": "VALIDATION",
                }
            for layer_name in layers:
                if self.board.GetLayerID(layer_name) < 0:
                    return {
                        "success": False,
                        "message": f"Unknown layer: {layer_name}",
                        "errorCode": "VALIDATION",
                    }

            # Reject an out-of-range explicit width up front (P10) — same bound
            # as route_trace / create_netclass.  A width omitted here falls back
            # to the net's net-class width below, which is always sane.
            width_err = _track_width_error(params.get("width"))
            if width_err is not None:
                return width_err

            start_pt, end_pt, net = self._resolve_smart_endpoints(params, layers)
            if isinstance(start_pt, dict):  # error dict from resolver
                return start_pt
            start_xy: Tuple[float, float] = (start_pt[0], start_pt[1])
            end_xy: Tuple[float, float] = (end_pt[0], end_pt[1])

            # Refuse a cross-net short (B4): the net is adopted from the source
            # pad, but the destination pad may belong to a DIFFERENT net —
            # routing between them shorts the two nets.  Endpoints are mm here;
            # endpoint_net_conflicts works in nm, so convert.  force=true
            # overrides (forwarded from the TS astar branch).
            force = bool(params.get("force", False))
            if net and not force:
                conflicts = endpoint_net_conflicts(
                    self.board,
                    [
                        (int(start_xy[0] * _NM), int(start_xy[1] * _NM)),
                        (int(end_xy[0] * _NM), int(end_xy[1] * _NM)),
                    ],
                    net,
                )
                if conflicts:
                    return _refuse_cross_net_short(net, conflicts)

            # Resolve the net's net-class props ONCE (trace + via widths) from
            # the .kicad_pro so both the default-width and via placement honour
            # the class the user assigned via assign_net_to_class (P2).
            netclass_props = self._project_netclass_props(net)
            width_mm = float(
                params.get("width") or self._smart_default_width_mm(net, netclass_props)
            )
            if width_mm <= 0:
                return {
                    "success": False,
                    "message": f"Track width must be positive (got {width_mm} mm)",
                    "errorCode": "VALIDATION",
                }

            items = self._collect_obstacle_items(layers)
            obstacles = obstacles_from_board_items(items)

            bbox = self.board.GetBoardEdgesBoundingBox()
            bounds = (
                bbox.GetLeft() / _NM,
                bbox.GetTop() / _NM,
                bbox.GetRight() / _NM,
                bbox.GetBottom() / _NM,
            )
            if bounds[2] - bounds[0] <= 0 or bounds[3] - bounds[1] <= 0:
                return {
                    "success": False,
                    "message": "Board has no outline; add_board_outline first",
                    "errorCode": "VALIDATION",
                }

            result = route_grid_astar(
                start_xy,
                end_xy,
                net=net,
                layers=layers,
                obstacles=obstacles,
                bounds=bounds,
                grid_mm=float(params.get("gridMm", 0.25)),
                clearance_mm=float(params.get("clearance", 0.2)),
                trace_width_mm=width_mm,
                via_cost=float(params.get("viaCost", 20.0)),
                max_nodes=int(params.get("maxNodes", 200_000)),
                start_layer=start_pt[2],
                end_layer=end_pt[2],
            )
            if not result.success:
                return {
                    "success": False,
                    # Truthful code: a routing outcome (blocked endpoint / area
                    # too dense), not an internal error — agents can branch on
                    # NO_PATH to retry with different params or route manually.
                    "errorCode": "NO_PATH",
                    "message": f"route_smart found no path: {result.message}",
                    "explored": result.explored,
                    "hint": (
                        "Try a coarser gridMm, a smaller clearance, two layers, "
                        "or route manually with route_trace."
                    ),
                }

            created = self._commit_smart_route(result, net, width_mm, netclass_props)
            return {
                "success": True,
                "segments": result.segments,
                "vias": result.vias,
                "lengthMm": round(result.length_mm, 4),
                "explored": result.explored,
                "net": net,
                "widthMm": width_mm,
                **created,
            }
        except Exception as e:  # API boundary; bucket: catch + return
            logger.error(f"Error in route_smart: {str(e)}", exc_info=True)
            return {
                "success": False,
                "message": "Failed to route with route_smart",
                "errorDetails": str(e),
            }

    # -- helpers -----------------------------------------------------------

    def _resolve_smart_endpoints(
        self, params: Dict[str, Any], layers: List[str]
    ) -> Tuple[Any, Any, Optional[str]]:
        """Return ((x, y, layer|None), (x, y, layer|None), net) or (error_dict, None, None).

        The layer element pins an SMD pad's endpoint to its copper layer so the
        search starts/ends there; through-hole pads and bare points yield None
        (any routing layer). An SMD pad on none of the requested layers is a
        refusal — routing to it would be electrically disconnected.
        """
        start = params.get("start")
        end = params.get("end")
        if start and end:
            try:
                return (
                    (float(start["x"]), float(start["y"]), None),
                    (float(end["x"]), float(end["y"]), None),
                    params.get("net"),
                )
            except (KeyError, TypeError, ValueError):
                return (
                    {
                        "success": False,
                        "message": "start/end must be {x, y} in mm",
                        "errorCode": "VALIDATION",
                    },
                    None,
                    None,
                )

        from_ref, to_ref = params.get("fromRef"), params.get("toRef")
        from_pad, to_pad = str(params.get("fromPad", "")), str(params.get("toPad", ""))
        if not from_ref or not from_pad or not to_ref or not to_pad:
            return (
                {
                    "success": False,
                    "message": "Provide fromRef/fromPad/toRef/toPad, or start/end points",
                    "errorCode": "VALIDATION",
                },
                None,
                None,
            )
        footprints = {fp.GetReference(): fp for fp in self.board.GetFootprints()}
        endpoints = []
        for ref, pad_num in ((from_ref, from_pad), (to_ref, to_pad)):
            fp = footprints.get(ref)
            if fp is None:
                return (
                    {
                        "success": False,
                        "message": f"Component not found: {ref}",
                        "errorCode": "NOT_FOUND",
                    },
                    None,
                    None,
                )
            pad = next((p for p in fp.Pads() if p.GetNumber() == pad_num), None)
            if pad is None:
                return (
                    {
                        "success": False,
                        "message": f"Pad {pad_num} not found on {ref}",
                        "errorCode": "NOT_FOUND",
                    },
                    None,
                    None,
                )
            pad_layer = self._pad_routing_layer(pad, layers)
            if pad_layer == "":
                return (
                    {
                        "success": False,
                        "message": (
                            f"Pad {pad_num} on {ref} is not on any requested routing layer "
                            f"({', '.join(layers)}); include the pad's copper layer in `layers`"
                        ),
                        "errorCode": "VALIDATION",
                    },
                    None,
                    None,
                )
            endpoints.append((pad, pad_layer))
        net = params.get("net") or endpoints[0][0].GetNetname() or None
        pos_a = endpoints[0][0].GetPosition()
        pos_b = endpoints[1][0].GetPosition()
        return (
            (pos_a.x / _NM, pos_a.y / _NM, endpoints[0][1]),
            (pos_b.x / _NM, pos_b.y / _NM, endpoints[1][1]),
            net,
        )

    def _pad_routing_layer(self, pad: Any, layers: List[str]) -> Optional[str]:
        """The routing layer an endpoint pad pins the search to.

        Through-hole pads span the stackup -> None (any layer). SMD pads
        return the first requested layer they sit on, or "" (sentinel) when
        they are on none of them — the caller refuses that case.
        """
        through = bool(pad.HasHole()) if hasattr(pad, "HasHole") else True
        if through:
            return None
        for layer_name in layers:
            layer_id = self.board.GetLayerID(layer_name)
            if layer_id >= 0 and pad.IsOnLayer(layer_id):
                return layer_name
        return ""

    def _project_netclass_props(self, net: Optional[str]) -> Dict[str, float]:
        """Resolve ``net``'s net-class trace/via widths (mm) from the .kicad_pro.

        In KiCad 9/10 net-class membership lives in the project JSON, not the
        SWIG board, so ``NETINFO_ITEM.GetNetClass()`` returns Default for a net
        the user assigned via ``assign_net_to_class`` — which is why route_smart
        routed power nets at the global default (P2).  This reads the sibling
        ``.kicad_pro``, resolves the net's class (exact assignment then wildcard
        pattern), and returns the mm floats for ``track_width`` / ``via_diameter``
        / ``via_drill`` present on that class (plus ``className``).  Returns an
        empty dict — never raises — when there is no project file, no assigned
        class, or anything unreadable, so callers fall back to the board default.
        """
        if not net:
            return {}
        try:
            from utils import kicad_pro

            project_file = kicad_pro.project_path_for_board(self.board)
            if not project_file or not os.path.exists(project_file):
                return {}
            data, _ = kicad_pro.load_kicad_pro(project_file)
            net_settings = data.get("net_settings")
            if not isinstance(net_settings, dict):
                return {}
            class_name = resolve_netclass_name(net_settings, net)
            if not class_name:
                return {}
            props: Dict[str, float] = {"className": class_name}
            for key in ("track_width", "via_diameter", "via_drill"):
                value = netclass_property(net_settings, class_name, key)
                if value is not None:
                    props[key] = value
            return props
        except Exception:  # noqa: BLE001 — never let project-read errors break routing
            return {}

    def _smart_default_width_mm(
        self, net: Optional[str], netclass_props: Optional[Dict[str, float]] = None
    ) -> float:
        """Netclass track width for the net, falling back to the board default.

        Resolution order:
          1. the net's net-class ``track_width`` from the .kicad_pro (where
             KiCad 9/10 stores membership — the SWIG board does not reflect it);
          2. the SWIG net-class width (KiCad 6/7, or in-memory-created classes);
          3. the board's current default track width;
          4. 0.25 mm.

        Non-positive widths are KiCad's "inherit" sentinel, not real widths —
        treat them as unset and keep falling back (same convention as
        _geometry._netclass_track_width_mm), ending at 0.25 mm.
        """
        if netclass_props is None:
            netclass_props = self._project_netclass_props(net)
        project_width = netclass_props.get("track_width")
        if project_width and project_width > 0:
            return project_width

        try:
            design = self.board.GetDesignSettings()
            if net:
                net_item = self.board.GetNetInfo().GetNetItem(net)
                if net_item is not None:
                    netclass = net_item.GetNetClass()
                    if netclass is not None:
                        width = int(netclass.GetTrackWidth())
                        if width > 0:
                            return width / _NM
            width = int(design.GetCurrentTrackWidth())
            if width > 0:
                return width / _NM
            return 0.25
        except Exception:  # noqa: BLE001 — conservative fallback
            return 0.25

    def _collect_obstacle_items(self, layers: List[str]) -> List[Dict[str, Any]]:
        """Extract pads/tracks/vias as mm rectangles for the A* obstacle model."""
        import pcbnew

        wanted = set(layers)
        items: List[Dict[str, Any]] = []
        for fp in self.board.GetFootprints():
            for pad in fp.Pads():
                bb = pad.GetBoundingBox()
                through = bool(pad.HasHole()) if hasattr(pad, "HasHole") else True
                pad_layer: Optional[str] = None
                if not through:
                    for layer_name in wanted:
                        if pad.IsOnLayer(self.board.GetLayerID(layer_name)):
                            pad_layer = layer_name
                            break
                    if pad_layer is None:
                        continue  # SMD pad on a layer we don't route
                items.append(
                    {
                        "type": "pad",
                        "x1": bb.GetLeft() / _NM,
                        "y1": bb.GetTop() / _NM,
                        "x2": bb.GetRight() / _NM,
                        "y2": bb.GetBottom() / _NM,
                        "layer": pad_layer,
                        "through_hole": through,
                        "net": pad.GetNetname() or None,
                    }
                )
        for track in list(self.board.Tracks()):
            if track.Type() == pcbnew.PCB_VIA_T:
                pos = track.GetPosition()
                r = (track.GetWidth() / _NM) / 2.0
                items.append(
                    {
                        "type": "via",
                        "x1": pos.x / _NM - r,
                        "y1": pos.y / _NM - r,
                        "x2": pos.x / _NM + r,
                        "y2": pos.y / _NM + r,
                        "net": track.GetNetname() or None,
                    }
                )
                continue
            layer_name = self.board.GetLayerName(track.GetLayer())
            if layer_name not in wanted:
                continue
            s, e = track.GetStart(), track.GetEnd()
            half = (track.GetWidth() / _NM) / 2.0
            items.append(
                {
                    "type": "track",
                    "x1": min(s.x, e.x) / _NM - half,
                    "y1": min(s.y, e.y) / _NM - half,
                    "x2": max(s.x, e.x) / _NM + half,
                    "y2": max(s.y, e.y) / _NM + half,
                    "layer": layer_name,
                    "net": track.GetNetname() or None,
                }
            )
        return items

    def _commit_smart_route(
        self,
        result: Any,
        net: Optional[str],
        width_mm: float,
        netclass_props: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """Create board tracks/vias for an A* result; returns creation stats.

        Vias inherit the net's net-class ``via_diameter`` / ``via_drill`` from
        the .kicad_pro when present (P2 — a Power net's fat via, not the global
        default), falling back to the board's current via size/drill otherwise.
        """
        import pcbnew

        if netclass_props is None:
            netclass_props = self._project_netclass_props(net)

        net_item = self.board.GetNetInfo().GetNetItem(net) if net else None
        net_code = net_item.GetNetCode() if net_item else 0
        design = self.board.GetDesignSettings()

        via_diameter_mm = netclass_props.get("via_diameter")
        via_drill_mm = netclass_props.get("via_drill")

        track_uuids: List[str] = []
        for seg in result.segments:
            track = pcbnew.PCB_TRACK(self.board)
            track.SetStart(
                pcbnew.VECTOR2I(int(seg["start"]["x"] * _NM), int(seg["start"]["y"] * _NM))
            )
            track.SetEnd(pcbnew.VECTOR2I(int(seg["end"]["x"] * _NM), int(seg["end"]["y"] * _NM)))
            track.SetLayer(self.board.GetLayerID(seg["layer"]))
            track.SetWidth(int(width_mm * _NM))
            if net_code:
                track.SetNetCode(net_code)
            self.board.Add(track)
            track_uuids.append(track.m_Uuid.AsString())

        via_uuids: List[str] = []
        for via_pt in result.vias:
            via = pcbnew.PCB_VIA(self.board)
            via.SetPosition(pcbnew.VECTOR2I(int(via_pt["x"] * _NM), int(via_pt["y"] * _NM)))
            if via_diameter_mm and via_diameter_mm > 0:
                via.SetWidth(int(via_diameter_mm * _NM))
            else:
                via.SetWidth(design.GetCurrentViaSize())
            if via_drill_mm and via_drill_mm > 0:
                via.SetDrill(int(via_drill_mm * _NM))
            else:
                via.SetDrill(design.GetCurrentViaDrill())
            if net_code:
                via.SetNetCode(net_code)
            self.board.Add(via)
            via_uuids.append(via.m_Uuid.AsString())

        return {
            "tracksCreated": len(track_uuids),
            "viasCreated": len(via_uuids),
            "trackUuids": track_uuids,
            "viaUuids": via_uuids,
        }
