"""IPC fast-path: copper pour / zone refill handlers.

Split out of the former handlers/ipc_fastpath.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.ipc_fastpath")

from ._common import _TO_MM_SCALE


def _ipc_board_edge_rect(ipc_board_api: Any) -> Optional[List[Dict[str, float]]]:
    """Best-effort rectangle from the board's Edge.Cuts shapes, or None.

    Mirrors the SWIG path's "omit outline → use board outline" fallback so
    ``add_copper_pour`` is usable on either backend without forcing the
    caller to pass an outline.  Returns four CCW corners in mm, or None
    when no Edge.Cuts geometry is available (in which case the handler
    refuses with a clear message instead of silently picking a wrong rect).
    """
    try:
        from kipy.proto.board.board_types_pb2 import BoardLayer  # type: ignore
        from kipy.util.units import to_mm  # type: ignore

        board = ipc_board_api._get_board()  # noqa: SLF001 — private accessor on our wrapper
        shapes = board.get_shapes() if board is not None else []
        if not shapes:
            return None
        edge_layer = BoardLayer.BL_Edge_Cuts
        min_x = min_y = float("inf")
        max_x = max_y = float("-inf")
        for shape in shapes:
            try:
                if getattr(shape, "layer", None) != edge_layer:
                    continue
                bbox = board.get_item_bounding_box(shape)
                if not bbox:
                    continue
                left, top, right, bottom = ipc_board_api._get_box2_extents(bbox)
                if left < min_x:
                    min_x = left
                if top < min_y:
                    min_y = top
                if right > max_x:
                    max_x = right
                if bottom > max_y:
                    max_y = bottom
            except Exception:
                continue
        if min_x == float("inf"):
            return None
        return [
            {"x": to_mm(min_x), "y": to_mm(min_y)},
            {"x": to_mm(max_x), "y": to_mm(min_y)},
            {"x": to_mm(max_x), "y": to_mm(max_y)},
            {"x": to_mm(min_x), "y": to_mm(max_y)},
        ]
    except Exception as e:
        logger.debug(f"Could not derive board edge rect via IPC: {e}")
        return None


def _ipc_available_net_names(iface: "KiCADInterface") -> Optional[List[str]]:
    """Net names on the live IPC board, or None when they can't be enumerated.

    Returning None (rather than an empty list) lets the caller fall back to
    passing the requested net through unchanged instead of refusing — e.g. in
    unit tests where ``ipc_board_api`` is a bare mock whose ``get_nets`` isn't
    iterable.  A real board always yields a non-empty list.
    """
    try:
        nets = iface.ipc_board_api.get_nets()
        names = [n.get("name", "") for n in nets if isinstance(n, dict)]
        names = [n for n in names if n]
        return names or None
    except Exception as e:
        logger.debug(f"Could not enumerate IPC net names: {e}")
        return None


def handle_add_copper_pour(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for add_copper_pour — adds zone with real-time UI update.

    Accepts the outline under either ``outline`` (canonical, matches the
    TS schema and the SWIG path) or ``points`` (legacy alias).  When the
    caller omits both, falls back to the board's Edge.Cuts bounding box
    so the documented "omit → use board outline" behaviour holds on the
    IPC path too — previously the IPC handler only read ``points`` and
    rejected every call that used the documented ``outline`` name.
    """
    try:
        from commands.routing._zones import resolve_net_name

        layer = params.get("layer", "F.Cu")
        net = params.get("net")
        allow_unconnected = bool(params.get("allowUnconnected", False))
        clearance = params.get("clearance", 0.5)
        min_width = params.get("minWidth", 0.25)
        # The MCP schema names this `outline`; some legacy callers pass
        # `points`.  Accept both.
        points = params.get("outline")
        if not points:
            points = params.get("points", [])
        priority = params.get("priority", 0)
        fill_type = params.get("fillType", "solid")
        name = params.get("name", "")

        # If no outline given, derive from Edge.Cuts (matches SWIG behaviour
        # and the public docstring).
        if not points or len(points) < 3:
            derived = _ipc_board_edge_rect(iface.ipc_board_api)
            if derived is not None:
                points = derived
            else:
                return {
                    "success": False,
                    "message": (
                        "Missing outline. Pass `outline` as an array of at "
                        "least 3 {x, y} points, or add a board outline "
                        "(Edge.Cuts) first so the pour can default to it."
                    ),
                }

        # Coordinate unit handling.  The IPC ``add_zone`` API expects mm.
        # ``add_copper_pour`` callers conventionally pass mm without a unit;
        # ``add_zone``'s schema makes ``unit`` required and accepts mil/inch,
        # so honour either a top-level ``unit`` field (whole-call) or a
        # per-point ``unit`` (matches the SWIG path) before forwarding.
        top_unit = str(params.get("unit", "mm")).lower()

        def _pt_scale(p: Dict[str, Any]) -> float:
            return _TO_MM_SCALE.get(str(p.get("unit", top_unit)).lower(), 1.0)

        formatted_points = [
            {"x": p.get("x", 0) * _pt_scale(p), "y": p.get("y", 0) * _pt_scale(p)} for p in points
        ]

        # Resolve the requested net against the board's real nets so a name
        # mismatch (e.g. "GND" vs "/GND") never silently produces a net-0
        # floating zone (finding F3).  A deliberate no-net zone is still
        # possible via allowUnconnected=true (or net="").
        resolved_net = net
        net_was_resolved = False
        if net:  # non-empty net requested
            available = _ipc_available_net_names(iface)
            if available is not None:
                resolved, candidates = resolve_net_name(net, available)
                if resolved is None:
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
                resolved_net = resolved
                net_was_resolved = resolved != net
            # else: nets not enumerable (mock / no live board) — pass through.
        elif net == "" or allow_unconnected:
            resolved_net = None  # deliberate no-net zone
        else:
            return {
                "success": False,
                "message": (
                    "Copper pour needs a net. Pass net=<name>, or "
                    'allowUnconnected=true (or net="") to deliberately '
                    "create an unconnected (net-0) zone."
                ),
                "requestedNet": net,
            }

        success = iface.ipc_board_api.add_zone(
            points=formatted_points,
            layer=layer,
            net_name=resolved_net,
            clearance=clearance,
            min_thickness=min_width,
            priority=priority,
            fill_mode=fill_type,
            name=name,
        )

        pour: Dict[str, Any] = {
            "layer": layer,
            "net": resolved_net if resolved_net is not None else "",
            "clearance": clearance,
            "minWidth": min_width,
            "priority": priority,
            "fillType": fill_type,
            "pointCount": len(formatted_points),
        }
        if net_was_resolved:
            pour["requestedNet"] = net
            pour["resolvedNet"] = resolved_net
        if resolved_net is None:
            pour["unconnected"] = True

        response: Dict[str, Any] = {
            "success": success,
            "message": (
                "Added copper pour (visible in KiCAD UI)"
                if success
                else "Failed to add copper pour"
            ),
            "pour": pour,
        }
        if success and net_was_resolved:
            response["resolvedNet"] = resolved_net
            response["warning"] = f"Requested net '{net}' resolved to board net '{resolved_net}'."

        # autoRefill parity with the SWIG wrapper
        # (_add_copper_pour_with_optional_refill): default ON so the zone is
        # filled for the next export; autoRefill=false keeps the legacy
        # deferred-fill behaviour.  Previously the IPC path silently ignored
        # the flag and always left the zone unfilled.
        if success:
            if bool(params.get("autoRefill", True)):
                try:
                    refilled = iface.ipc_board_api.refill_zones()
                except Exception as refill_err:  # pragma: no cover - best-effort
                    logger.warning(f"IPC auto-refill after add_copper_pour failed: {refill_err}")
                    refilled = False
                response["refillStatus"] = (
                    "filled"
                    if refilled
                    else (
                        "deferred_after_failure — zone added but the refill "
                        "failed; call refill_zones (or press B in KiCad)"
                    )
                )
            else:
                response["refillStatus"] = (
                    "deferred — zone defined but not filled; "
                    "call refill_zones before export_gerber"
                )
        return response
    except Exception as e:
        logger.error(f"IPC add_copper_pour error: {e}")
        return {"success": False, "message": str(e)}


def _normalize_zone_layer(raw_layer: Any) -> str:
    """Turn a kipy BoardLayer value (enum, name, or raw int) into ``F.Cu``."""
    from kicad_api.ipc_backend._helpers import normalize_board_layer

    return normalize_board_layer(raw_layer)


def _zone_uuid_str(zone: Any) -> str:
    """Bare uuid string for a kipy zone (see kiid_str for the proto-repr trap)."""
    from kicad_api.ipc_backend._helpers import kiid_str

    return kiid_str(getattr(zone, "id", None))


def handle_query_zones(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for query_zones — copper zones read live from the KiCad board.

    The SWIG handler reads ``iface.board`` and fails "No board is loaded" when
    the board is open in KiCad but was never loaded through the MCP; this path
    reads zones over IPC instead.  Output shape mirrors the SWIG handler (net,
    layers, priority, isFilled, boundingBox) with the same net / layer /
    boundingBox filters so callers don't have to branch on backend.

    ``filledArea`` is always ``None`` on this path: the IPC API exposes no
    server-side area computation (kipy zones carry filled polygons, not an
    area), so ``null`` — never a misleading ``0`` — marks it unobtainable.
    The SWIG handler reports the real mm² value.
    """
    try:
        from commands.routing._zones import resolve_query_net_filter

        net_filter = params.get("net")
        layer_filter = params.get("layer")
        bbox = params.get("boundingBox")

        bbox_box = None
        if bbox:
            unit_scale = _TO_MM_SCALE.get(bbox.get("unit", "mm"), 1.0)
            xs = sorted((bbox.get("x1", 0) * unit_scale, bbox.get("x2", 0) * unit_scale))
            ys = sorted((bbox.get("y1", 0) * unit_scale, bbox.get("y2", 0) * unit_scale))
            bbox_box = (xs[0], ys[0], xs[1], ys[1])

        # nm → mm, same converter the IPC board API uses for footprint bboxes.
        from kipy.util.units import to_mm as nm_to_mm  # type: ignore

        board = iface.ipc_board_api._get_board()  # noqa: SLF001 — our wrapper's accessor
        zones = list(board.get_zones())

        # Resolve the net filter against the board's real nets so a bare "GND"
        # matches a hierarchical "/GND" zone (Bug 2 — parity with copper_pour).
        # Read-only: never refuses, only annotates.  When the board nets can't
        # be enumerated (bare mock), resolve against the zones' own nets.
        target_net = net_filter
        net_annotations: Dict[str, Any] = {}
        if net_filter:
            available = _ipc_available_net_names(iface)
            if available is None:
                available = [n for n in (_zone_net_name(z) for z in zones) if n]
            if available:
                target_net, net_annotations = resolve_query_net_filter(net_filter, available)

        zones_out = []
        for zone in zones:
            try:
                z_net = _zone_net_name(zone)
                if target_net and z_net != target_net:
                    continue

                layer_names = []
                try:
                    layer_names = [_normalize_zone_layer(l) for l in zone.layers]
                except Exception:
                    layer_names = []
                if layer_filter and layer_filter not in layer_names:
                    continue

                bbox_data = None
                try:
                    bb = board.get_item_bounding_box(zone)
                    if bb:
                        bbox_data = {
                            "x1": nm_to_mm(bb.min.x),
                            "y1": nm_to_mm(bb.min.y),
                            "x2": nm_to_mm(bb.max.x),
                            "y2": nm_to_mm(bb.max.y),
                            "unit": "mm",
                        }
                except Exception:
                    pass  # bounding box not always available via IPC

                if bbox_box is not None and bbox_data is not None:
                    fx1, fy1, fx2, fy2 = bbox_box
                    if (
                        bbox_data["x2"] < fx1
                        or bbox_data["x1"] > fx2
                        or bbox_data["y2"] < fy1
                        or bbox_data["y1"] > fy2
                    ):
                        continue

                entry: Dict[str, Any] = {
                    "uuid": _zone_uuid_str(zone),
                    "net": z_net,
                    "netCode": None,
                    "layers": layer_names,
                    "priority": zone.priority if hasattr(zone, "priority") else 0,
                    "isFilled": bool(zone.filled) if hasattr(zone, "filled") else False,
                    # Not obtainable over IPC (no area API in kipy) — null,
                    # never a fake 0.  The SWIG path reports real mm².
                    "filledArea": None,
                }
                if bbox_data is not None:
                    entry["boundingBox"] = bbox_data
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
        logger.error(f"IPC query_zones error: {e}")
        return {"success": False, "message": str(e)}


def handle_refill_zones(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for refill_zones — refills all zones with real-time UI update."""
    try:
        success = iface.ipc_board_api.refill_zones()

        return {
            "success": success,
            "message": (
                "Zones refilled (visible in KiCAD UI)" if success else "Failed to refill zones"
            ),
        }
    except Exception as e:
        logger.error(f"IPC refill_zones error: {e}")
        return {"success": False, "message": str(e)}


# ---------------------------------------------------------------------------
# delete_copper_pour / edit_copper_pour fast paths (finding N2).
#
# Zone ADD and QUERY already routed through IPC while DELETE and EDIT existed
# only as SWIG methods — so with KiCad open, an added zone lived in KiCad's
# memory and the very next delete/edit read the SWIG board and refused with
# "No zone matched".  These fast paths give all four zone ops one coherent
# backend per session; the SWIG methods remain the fallback when KiCad is
# closed.  Selection semantics (uuid / net / layer filters, resolve_net_name
# on the net filter, multi-match refusal without all=true) mirror
# commands/routing/_zones.py so callers don't branch on backend.
# ---------------------------------------------------------------------------


def _zone_net_name(zone: Any) -> str:
    try:
        return zone.net.name if zone.net else ""
    except Exception:
        return ""


def _zone_layer_names(zone: Any) -> List[str]:
    try:
        return [_normalize_zone_layer(layer) for layer in zone.layers]
    except Exception:
        return []


def _zone_brief_ipc(zone: Any) -> Dict[str, Any]:
    """Identifying summary — same shape as the SWIG ``_zone_brief``."""
    layers = _zone_layer_names(zone)
    return {
        "uuid": _zone_uuid_str(zone),
        "net": _zone_net_name(zone),
        "layer": layers[0] if layers else None,
        "isFilled": bool(getattr(zone, "filled", False)),
    }


def _find_zones_ipc(
    iface: "KiCADInterface",
    uuid: Optional[str],
    net: Optional[str],
    layer: Optional[str],
) -> tuple:
    """(matches, error_response) over the live IPC board's zones.

    Mirrors the SWIG ``RoutingCommands._find_zones`` contract, including the
    exact refusal messages and the F3 net-name resolution on the net filter.
    """
    from commands.routing._zones import resolve_net_name

    board = iface.ipc_board_api._get_board()  # noqa: SLF001 — our wrapper's accessor
    zones = list(board.get_zones())

    if uuid:
        matches = [z for z in zones if _zone_uuid_str(z) == uuid]
        if not matches:
            return [], {
                "success": False,
                "message": f"No zone with uuid {uuid}",
                "errorDetails": "Call query_zones to list zone uuids",
                "zones": [_zone_brief_ipc(z) for z in zones],
            }
        return matches, None

    matches = zones
    if net is not None:
        zone_nets = [_zone_net_name(z) for z in matches]
        resolved, _ = resolve_net_name(net, zone_nets)
        target_net = resolved if resolved is not None else net
        matches = [z for z, zn in zip(matches, zone_nets) if zn == target_net]
    if layer is not None:
        matches = [z for z in matches if layer in _zone_layer_names(z)]
    if not matches:
        return [], {
            "success": False,
            "message": "No zone matched the given net/layer filters",
            "errorDetails": "Call query_zones to list zones",
            "zones": [_zone_brief_ipc(z) for z in zones],
        }
    return matches, None


def handle_delete_copper_pour(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for delete_copper_pour — deletes zones from the live board.

    Selects by ``zoneUuid`` (preferred, matches the TS schema; ``uuid`` kept
    as an alias) or ``net``/``layer`` filters.
    """
    try:
        matches, err = _find_zones_ipc(
            iface,
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
                "zones": [_zone_brief_ipc(z) for z in matches],
            }
        deleted = [_zone_brief_ipc(z) for z in matches]
        if not iface.ipc_board_api.remove_zones(matches):
            return {"success": False, "message": "Failed to delete copper pour via IPC"}
        return {
            "success": True,
            "message": f"Deleted {len(deleted)} copper pour(s) (visible in KiCAD UI)",
            "deleted": deleted,
        }
    except Exception as e:
        logger.error(f"IPC delete_copper_pour error: {e}")
        return {"success": False, "message": str(e)}


# padConnection param value → kipy ZoneConnectionStyle name (mirrors the SWIG
# _PAD_CONNECTION_ATTRS mapping).
_ZONE_CONNECTION_STYLES = {
    "solid": "ZCS_FULL",
    "thermal": "ZCS_THERMAL",
    "none": "ZCS_NONE",
    "thru_hole_only": "ZCS_PTH_THERMAL",
}


def handle_edit_copper_pour(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for edit_copper_pour — edits one zone on the live board.

    Same param surface as the SWIG path: select by zoneUuid (``uuid`` kept as
    an alias) or net/layer filters
    matching exactly one zone; then any of newNet / newLayer / clearance /
    minWidth / priority / fillType / padConnection / thermalGap /
    thermalBridgeWidth / outline overwrite that zone's settings.
    """
    try:
        from commands.routing._zones import resolve_net_name
        from kipy.geometry import PolyLine, PolyLineNode
        from kipy.proto.board.board_types_pb2 import (
            BoardLayer,
            ZoneConnectionStyle,
            ZoneFillMode,
        )
        from kipy.util.units import from_mm

        matches, err = _find_zones_ipc(
            iface,
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
                "zones": [_zone_brief_ipc(z) for z in matches],
            }
        zone = matches[0]
        changed: List[str] = []

        new_net = params.get("newNet")
        resolved_new_net: Optional[str] = None
        if new_net is not None:
            board = iface.ipc_board_api._get_board()  # noqa: SLF001
            board_nets = list(board.get_nets())
            resolved, candidates = resolve_net_name(new_net, [n.name for n in board_nets])
            if resolved is None:
                return {
                    "success": False,
                    "message": f"Net '{new_net}' does not exist on the board",
                    "requestedNet": new_net,
                    "candidates": candidates,
                }
            for n in board_nets:
                if n.name == resolved:
                    zone.net = n
                    break
            changed.append("net")
            if resolved != new_net:
                resolved_new_net = resolved

        new_layer = params.get("newLayer")
        if new_layer is not None:
            layer_enum = getattr(BoardLayer, "BL_" + new_layer.replace(".", "_"), None)
            if layer_enum is None:
                return {"success": False, "message": f"Layer '{new_layer}' does not exist"}
            zone.layers = [layer_enum]
            changed.append("layer")

        if params.get("clearance") is not None:
            zone.clearance = from_mm(float(params["clearance"]))
            changed.append("clearance")

        if params.get("minWidth") is not None:
            zone.min_thickness = from_mm(float(params["minWidth"]))
            changed.append("minWidth")

        if params.get("priority") is not None:
            zone.priority = int(params["priority"])
            changed.append("priority")

        fill_type = params.get("fillType")
        if fill_type is not None:
            zone._proto.copper_settings.fill_mode = (
                ZoneFillMode.ZFM_HATCHED if fill_type == "hatched" else ZoneFillMode.ZFM_SOLID
            )
            changed.append("fillType")

        pad_connection = params.get("padConnection")
        if pad_connection is not None:
            style_name = _ZONE_CONNECTION_STYLES.get(pad_connection)
            style = getattr(ZoneConnectionStyle, style_name, None) if style_name else None
            if style is None:
                return {
                    "success": False,
                    "message": (
                        f"Unknown padConnection '{pad_connection}' — use one of "
                        f"{sorted(_ZONE_CONNECTION_STYLES)}"
                    ),
                }
            zone._proto.copper_settings.connection.zone_connection = style
            changed.append("padConnection")

        if params.get("thermalGap") is not None:
            zone._proto.copper_settings.connection.thermal_spokes.gap = from_mm(
                float(params["thermalGap"])
            )
            changed.append("thermalGap")

        if params.get("thermalBridgeWidth") is not None:
            zone._proto.copper_settings.connection.thermal_spokes.width = from_mm(
                float(params["thermalBridgeWidth"])
            )
            changed.append("thermalBridgeWidth")

        points = params.get("outline")
        if points:
            if len(points) < 3:
                return {"success": False, "message": "outline needs at least 3 points"}
            outline = PolyLine()
            outline.closed = True
            for point in points:
                scale = _TO_MM_SCALE.get(str(point.get("unit", "mm")).lower(), 1.0)
                outline.append(
                    PolyLineNode.from_xy(
                        from_mm(float(point["x"]) * scale), from_mm(float(point["y"]) * scale)
                    )
                )
            del zone._proto.outline.polygons[:]
            zone._proto.outline.polygons.add()
            zone._proto.outline.polygons[0].outline.CopyFrom(outline._proto)
            changed.append("outline")

        if not changed:
            return {
                "success": False,
                "message": (
                    "No editable property given — pass one of newNet, newLayer, "
                    "clearance, minWidth, priority, fillType, padConnection, "
                    "thermalGap, thermalBridgeWidth, outline"
                ),
                "zone": _zone_brief_ipc(zone),
            }

        # The stored fill no longer reflects the zone settings.
        try:
            zone._proto.filled = False
        except Exception:
            pass

        if not iface.ipc_board_api.update_zone(zone):
            return {"success": False, "message": "Failed to edit copper pour via IPC"}

        edit_result: Dict[str, Any] = {
            "success": True,
            "message": f"Edited copper pour ({', '.join(changed)})",
            "changed": changed,
            "zone": _zone_brief_ipc(zone),
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
        logger.error(f"IPC edit_copper_pour error: {e}")
        return {"success": False, "message": str(e)}
