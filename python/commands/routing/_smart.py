"""route_smart: obstacle-avoiding pad-to-pad routing on the SWIG board.

Bridges the board to the pure grid A* core in _astar.py: extracts pads,
tracks and vias as keep-out rectangles, runs the search, then commits the
resulting segments and vias as real board objects. Unlike route_pad_to_pad
(one straight segment, refuses on obstacles) this routes AROUND obstacles
and may change layers through vias when two copper layers are given.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

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
                }
            for layer_name in layers:
                if self.board.GetLayerID(layer_name) < 0:
                    return {"success": False, "message": f"Unknown layer: {layer_name}"}

            start_pt, end_pt, net = self._resolve_smart_endpoints(params)
            if isinstance(start_pt, dict):  # error dict from resolver
                return start_pt
            start_xy: Tuple[float, float] = (start_pt[0], start_pt[1])
            end_xy: Tuple[float, float] = (end_pt[0], end_pt[1])

            width_mm = float(params.get("width") or self._smart_default_width_mm(net))

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
            )
            if not result.success:
                return {
                    "success": False,
                    "message": f"route_smart found no path: {result.message}",
                    "explored": result.explored,
                    "hint": (
                        "Try a coarser gridMm, a smaller clearance, two layers, "
                        "or route manually with route_trace."
                    ),
                }

            created = self._commit_smart_route(result, net, width_mm)
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

    def _resolve_smart_endpoints(self, params: Dict[str, Any]):
        """Return ((x, y, pad|None), (x, y, pad|None), net) or (error_dict, _, _)."""
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
                return ({"success": False, "message": "start/end must be {x, y} in mm"}, None, None)

        from_ref, to_ref = params.get("fromRef"), params.get("toRef")
        from_pad, to_pad = str(params.get("fromPad", "")), str(params.get("toPad", ""))
        if not from_ref or not from_pad or not to_ref or not to_pad:
            return (
                {
                    "success": False,
                    "message": "Provide fromRef/fromPad/toRef/toPad, or start/end points",
                },
                None,
                None,
            )
        footprints = {fp.GetReference(): fp for fp in self.board.GetFootprints()}
        pads = []
        for ref, pad_num in ((from_ref, from_pad), (to_ref, to_pad)):
            fp = footprints.get(ref)
            if fp is None:
                return ({"success": False, "message": f"Component not found: {ref}"}, None, None)
            pad = next((p for p in fp.Pads() if p.GetNumber() == pad_num), None)
            if pad is None:
                return (
                    {"success": False, "message": f"Pad {pad_num} not found on {ref}"},
                    None,
                    None,
                )
            pads.append(pad)
        net = params.get("net") or pads[0].GetNetname() or None
        pos_a, pos_b = pads[0].GetPosition(), pads[1].GetPosition()
        return (
            (pos_a.x / _NM, pos_a.y / _NM, pads[0]),
            (pos_b.x / _NM, pos_b.y / _NM, pads[1]),
            net,
        )

    def _smart_default_width_mm(self, net: Optional[str]) -> float:
        """Netclass track width for the net, falling back to the board default."""
        try:
            design = self.board.GetDesignSettings()
            if net:
                net_item = self.board.GetNetInfo().GetNetItem(net)
                if net_item is not None:
                    netclass = net_item.GetNetClass()
                    if netclass is not None:
                        return netclass.GetTrackWidth() / _NM
            return design.GetCurrentTrackWidth() / _NM
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
        self, result: Any, net: Optional[str], width_mm: float
    ) -> Dict[str, Any]:
        """Create board tracks/vias for an A* result; returns creation stats."""
        import pcbnew

        net_item = self.board.GetNetInfo().GetNetItem(net) if net else None
        net_code = net_item.GetNetCode() if net_item else 0
        design = self.board.GetDesignSettings()

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
            via.SetWidth(design.GetCurrentViaSize())
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
