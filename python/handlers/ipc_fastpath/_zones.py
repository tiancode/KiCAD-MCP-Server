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
    """
    try:
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
        zones_out = []
        for zone in board.get_zones():
            try:
                z_net = ""
                try:
                    z_net = zone.net.name if zone.net else ""
                except Exception:
                    z_net = ""
                if net_filter and z_net != net_filter:
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
                }
                if bbox_data is not None:
                    entry["boundingBox"] = bbox_data
                zones_out.append(entry)
            except Exception as zone_err:
                logger.warning(f"Skipping invalid zone object: {zone_err}")
                continue

        return {"success": True, "zoneCount": len(zones_out), "zones": zones_out}
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
