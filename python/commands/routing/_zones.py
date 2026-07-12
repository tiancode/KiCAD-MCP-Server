"""Copper zone / pour commands for RoutingCommands.

Split out of the former monolithic commands/routing.py."""

import logging
from typing import Any, Dict, List, Optional, Tuple

import pcbnew

logger = logging.getLogger("kicad_interface")


def resolve_net_name(
    requested: str, available: List[str], cap: int = 12
) -> Tuple[Optional[str], List[str]]:
    """Resolve a requested net name against the board's actual net names.

    Boards built from a hierarchical / sheet-prefixed schematic expose their
    nets as ``/GND`` etc., so a bare ``GND`` request must NOT silently miss —
    that produced an electrically-dead net-code-0 floating zone (finding F3).

    Resolution order (first hit wins):

      1. exact match
      2. ``/`` + requested            (sheet-root prefix, the common case)
      3. case-insensitive variant of #1 or #2
      4. exactly one net whose last ``/``-segment equals the requested name
         (e.g. ``Power/GND`` for ``GND``), case-sensitive then -insensitive

    Returns ``(resolved, candidates)``:

      * ``resolved`` is the actual board net name to assign (verbatim, so an
        exact ``NetsByName`` lookup will find it), or ``None`` when nothing
        matched or the match was ambiguous.
      * ``candidates`` is empty on success; on failure it lists the closest
        net names (ambiguous matches, else substring matches, else a capped
        sample of the board's real nets) to guide the caller.

    Pure over the net-name list so it is unit-testable without pcbnew.
    """
    if not requested:
        return None, []

    names = list(available)
    name_set = set(names)

    # 1. exact
    if requested in name_set:
        return requested, []

    # 2. sheet-root '/' prefix
    slash = requested if requested.startswith("/") else "/" + requested
    if slash != requested and slash in name_set:
        return slash, []

    # 3. case-insensitive variant of the exact or '/'-prefixed name
    rl = requested.lower()
    sl = slash.lower()
    ci = [n for n in names if n.lower() == rl or n.lower() == sl]
    if len(ci) == 1:
        return ci[0], []
    if len(ci) > 1:
        # genuine collision (e.g. both 'GND' and '/gnd') — don't guess.
        return None, sorted(set(ci))[:cap]

    # 4. unique last-path-segment match (handles Power/GND ↔ GND either way)
    req_seg = requested.rsplit("/", 1)[-1]
    req_seg_l = req_seg.lower()
    seg_exact = [n for n in names if n.rsplit("/", 1)[-1] == req_seg]
    if len(seg_exact) == 1:
        return seg_exact[0], []
    seg_ci = [n for n in names if n.rsplit("/", 1)[-1].lower() == req_seg_l]
    if len(seg_ci) == 1:
        return seg_ci[0], []

    # Nothing uniquely matched — build the closest-candidate list.
    ambiguous = seg_exact or seg_ci  # non-empty only when >1 share the segment
    if ambiguous:
        return None, sorted(set(ambiguous))[:cap]
    subs = sorted(n for n in names if n and rl in n.lower())
    if subs:
        return None, subs[:cap]
    meaningful = sorted(n for n in names if n and not n.startswith("unconnected-"))
    return None, meaningful[:cap]


def resolve_query_net_filter(
    requested: Optional[str], available: List[str]
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Resolve a net-name *filter* for a read-only copper query.

    ``query_copper`` (query_traces / query_zones) used to compare the requested
    net verbatim, so a bare ``GND`` filter returned nothing on a board whose
    real net is the sheet-prefixed ``/GND`` — while copper_pour/routing resolve
    the same name via :func:`resolve_net_name` (Bug 2).  This routes a query's
    net filter through the same resolver, but — being read-only — it never
    refuses:

      * unique match → return the resolved board net; annotate ``resolvedNet``
        + ``requestedNet`` when it differs from what was asked.
      * no / ambiguous match → return the literal requested name (so the query
        yields an empty result, as before) and annotate ``netCandidates`` with
        the closest names instead of silently returning nothing.

    Returns ``(target_net, annotations)``.  ``annotations`` is empty on an
    exact hit; merge it into the query response.  Pure over the net-name list.
    """
    annotations: Dict[str, Any] = {}
    if not requested:
        return requested, annotations
    resolved, candidates = resolve_net_name(requested, available)
    if resolved is None:
        if candidates:
            annotations["netCandidates"] = candidates
        return requested, annotations
    if resolved != requested:
        annotations["resolvedNet"] = resolved
        annotations["requestedNet"] = requested
    return resolved, annotations


def _zone_filled_area_mm2(zone: Any) -> Optional[float]:
    """Filled copper area of a zone in mm², or ``None`` when unobtainable.

    ``ZONE.GetFilledArea()`` returns the *cached* area (``m_area``), which is 0
    for a zone freshly loaded from disk — the cache is only populated by a fill
    operation or by ``CalculateFilledArea()``.  Reading the getter alone
    therefore reported ``filledArea: 0`` for zones that carry ``filled_polygon``
    geometry on disk (Bug 3).  Recompute via ``CalculateFilledArea()`` first so
    the value is real; fall back to the cached getter; return ``None`` only when
    the zone exposes neither numeric method (so the caller emits ``null`` rather
    than a misleading 0).  An *unfilled* zone legitimately reports ``0.0``.
    """
    scale = 1000000  # internal units (nm) per mm
    area_iu2: Optional[float] = None
    for meth in ("CalculateFilledArea", "GetFilledArea"):
        fn = getattr(zone, meth, None)
        if fn is None:
            continue
        try:
            val = fn()
        except Exception:
            continue
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue  # e.g. a bare MagicMock in unit tests — not a real area
        area_iu2 = float(val)
        if val:  # a non-zero CalculateFilledArea wins; a 0 falls through
            break
    if area_iu2 is None:
        return None
    return area_iu2 / (scale * scale)


class ZoneMixin:
    def _board_net_names(self) -> List[str]:
        """All net names on the SWIG board (reliable across pcbnew versions)."""
        names: List[str] = []
        try:
            netinfo = self.board.GetNetInfo()
            for code in range(netinfo.GetNetCount()):
                item = netinfo.GetNetItem(code)
                if item is not None:
                    names.append(item.GetNetname())
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"Could not enumerate board net names: {e}")
        return names

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

            # Resolve the net filter against the board's real nets so a bare
            # "GND" query matches a hierarchical "/GND" zone (Bug 2 — parity
            # with copper_pour).  Read-only: never refuses, only annotates.
            target_net = net_name
            net_annotations: Dict[str, Any] = {}
            if net_name:
                target_net, net_annotations = resolve_query_net_filter(
                    net_name, self._board_net_names()
                )

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
                    if target_net and z_net != target_net:
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
                    # Filled area in mm².  CalculateFilledArea() is called
                    # first so a zone loaded from disk (empty area cache)
                    # reports its real area instead of 0 (Bug 3); None when the
                    # backend can't compute it (emitted as null, never a fake 0).
                    entry["filledArea"] = _zone_filled_area_mm2(zone)

                    zones_out.append(entry)
                except Exception as zone_err:
                    logger.warning(f"Skipping invalid zone object: {zone_err}")
                    continue

            return {
                "success": True,
                "zoneCount": len(zones_out),
                "zones": zones_out,
                **net_annotations,
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
            allow_unconnected = bool(params.get("allowUnconnected", False))

            # Resolve the requested net against the board's real nets BEFORE
            # building the zone.  A name mismatch (e.g. "GND" when the board
            # uses "/GND") previously produced a silent net-code-0 floating
            # zone — a large electrically-dead plane (finding F3).  We refuse
            # instead, listing the closest candidates.  A deliberate no-net
            # zone is still possible via allowUnconnected=true (or net="").
            resolved_net: Optional[str] = None  # actual board net to assign
            net_was_resolved = False
            if net:  # non-empty net requested
                available = self._board_net_names()
                resolved_net, candidates = resolve_net_name(net, available)
                if resolved_net is None:
                    return {
                        "success": False,
                        "message": (
                            f"Net '{net}' not found on the board. A copper pour "
                            "must attach to a real net — a name mismatch would "
                            "create an electrically-dead net-0 plane. Pass one of "
                            "the candidate net names, or allowUnconnected=true "
                            '(or net="") for a deliberate no-net zone.'
                        ),
                        "requestedNet": net,
                        "candidates": candidates,
                    }
                net_was_resolved = resolved_net != net
            elif net == "" or allow_unconnected:
                # Deliberate no-net (net-0) zone — explicit empty net or flag.
                resolved_net = None
            else:
                # net is None and no escape hatch: refuse rather than guess.
                return {
                    "success": False,
                    "message": (
                        "Copper pour needs a net. Pass net=<name>, or "
                        'allowUnconnected=true (or net="") to deliberately '
                        "create an unconnected (net-0) zone."
                    ),
                    "requestedNet": net,
                }

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

            # Set net (resolved to the board's actual net name above).
            if resolved_net:
                netinfo = self.board.GetNetInfo()
                nets_map = netinfo.NetsByName()
                if nets_map.has_key(resolved_net):
                    zone.SetNet(nets_map[resolved_net])

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
            outline.NewOutline()

            # Add points to outline
            for point in points:
                scale = (
                    1000000
                    if point.get("unit", "mm") == "mm"
                    else (25400 if point.get("unit", "mm") == "mil" else 25400000)
                )
                x_nm = int(point["x"] * scale)
                y_nm = int(point["y"] * scale)
                outline.Append(pcbnew.VECTOR2I(x_nm, y_nm))

            self.board.Add(zone)

            # Zones are left unfilled here: the SWIG ZONE_FILLER is unreliable,
            # so filling is deferred until the board is opened/saved in KiCad.

            pour: Dict[str, Any] = {
                "layer": layer,
                "net": resolved_net if resolved_net is not None else "",
                "clearance": clearance,
                "minWidth": min_width,
                "priority": priority,
                "fillType": fill_type,
                "pointCount": len(points),
            }
            if net_was_resolved:
                pour["requestedNet"] = net
                pour["resolvedNet"] = resolved_net
            if resolved_net is None:
                pour["unconnected"] = True

            result: Dict[str, Any] = {
                "success": True,
                "message": "Added copper pour",
                "pour": pour,
            }
            if net_was_resolved:
                result["resolvedNet"] = resolved_net
                result["warning"] = (
                    f"Requested net '{net}' resolved to board net " f"'{resolved_net}'."
                )
            return result

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
            # Resolve the net selector so "GND" matches a zone on "/GND"
            # (finding F3 — keep delete/edit filters consistent with add).
            resolved, _ = resolve_net_name(net, [z.GetNetname() for z in matches])
            target_net = resolved if resolved is not None else net
            matches = [z for z in matches if z.GetNetname() == target_net]
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

        Select the zone by ``zoneUuid`` (preferred, from query_zones; ``uuid``
        accepted as an alias) or by ``net``/``layer`` filters that must match
        exactly one zone.  Any of
        the optional property params then overwrite that zone's settings.
        The fill is marked stale — call refill_zones afterwards.
        """
        try:
            matches, err = self._find_zones(
                params.get("zoneUuid") or params.get("uuid"),
                params.get("net"),
                params.get("layer"),
            )
            if err:
                return err
            if len(matches) > 1:
                return {
                    "success": False,
                    "message": (
                        f"{len(matches)} zones matched — refine with zoneUuid "
                        "(from query_zones) or a net+layer pair"
                    ),
                    "zones": [self._zone_brief(z) for z in matches],
                }
            zone = matches[0]
            scale = 1000000  # mm -> nm
            changed: List[str] = []

            new_net = params.get("newNet")
            resolved_new_net: Optional[str] = None
            if new_net is not None:
                resolved, candidates = resolve_net_name(new_net, self._board_net_names())
                if resolved is None:
                    return {
                        "success": False,
                        "message": f"Net '{new_net}' does not exist on the board",
                        "requestedNet": new_net,
                        "candidates": candidates,
                    }
                nets_map = self.board.GetNetInfo().NetsByName()
                if nets_map.has_key(resolved):
                    zone.SetNet(nets_map[resolved])
                changed.append("net")
                if resolved != new_net:
                    resolved_new_net = resolved

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

            edit_result: Dict[str, Any] = {
                "success": True,
                "message": f"Edited copper pour ({', '.join(changed)})",
                "changed": changed,
                "zone": self._zone_brief(zone),
                "refillStatus": (
                    "fill marked stale — call refill_zones (or let KiCad refill "
                    "on open) before export_gerber"
                ),
            }
            if resolved_new_net is not None:
                edit_result["resolvedNet"] = resolved_new_net
                edit_result["warning"] = (
                    f"Requested net '{new_net}' resolved to board net " f"'{resolved_new_net}'."
                )
            return edit_result

        except Exception as e:
            logger.error(f"Error editing copper pour: {str(e)}")
            return {
                "success": False,
                "message": "Failed to edit copper pour",
                "errorDetails": str(e),
            }

    def delete_copper_pour(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delete copper pour(s).

        Select by ``zoneUuid`` (single zone; ``uuid`` accepted as an alias) or
        ``net``/``layer`` filters.  When
        the filters match several zones, pass ``all=true`` to delete every
        match — otherwise the call is refused with the candidate list.
        """
        try:
            matches, err = self._find_zones(
                params.get("zoneUuid") or params.get("uuid"),
                params.get("net"),
                params.get("layer"),
            )
            if err:
                return err
            if len(matches) > 1 and not bool(params.get("all", False)):
                return {
                    "success": False,
                    "message": (
                        f"{len(matches)} zones matched — pass all=true to delete "
                        "every match, or refine with zoneUuid (from query_zones)"
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
