"""IPC fast-path: trace / via / net routing handlers.

Split out of the former handlers/ipc_fastpath.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.ipc_fastpath")

from ._common import _TO_MM_SCALE, extract_xy, swig_fallback_mutation


def handle_route_trace(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for route_trace — adds track with real-time UI update."""
    try:
        # Extract parameters matching the existing route_trace interface.
        # Accept both nested {"start": {"x":..,"y":..}} and flat startX/startY shapes.
        start_x, start_y, _ = extract_xy(params, key="start", flat_x="startX", flat_y="startY")
        end_x, end_y, _ = extract_xy(params, key="end", flat_x="endX", flat_y="endY")
        layer = params.get("layer", "F.Cu")
        width = params.get("width", 0.25)
        net = params.get("net")

        success = iface.ipc_board_api.add_track(
            start_x=start_x,
            start_y=start_y,
            end_x=end_x,
            end_y=end_y,
            width=width,
            layer=layer,
            net_name=net,
        )

        return {
            "success": success,
            "message": ("Added trace (visible in KiCAD UI)" if success else "Failed to add trace"),
            "trace": {
                "start": {"x": start_x, "y": start_y, "unit": "mm"},
                "end": {"x": end_x, "y": end_y, "unit": "mm"},
                "layer": layer,
                "width": width,
                "net": net,
            },
        }
    except Exception as e:
        logger.error(f"IPC route_trace error: {e}")
        return {"success": False, "message": str(e)}


def handle_route_arc_trace(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for route_arc_trace — adds copper arc with real-time UI update."""
    try:
        start = params.get("start", {})
        mid = params.get("mid", {})
        end = params.get("end", {})
        layer = params.get("layer", "F.Cu")
        width = params.get("width", 0.25)
        net = params.get("net")

        start_x = start.get("x", 0)
        start_y = start.get("y", 0)
        mid_x = mid.get("x", 0)
        mid_y = mid.get("y", 0)
        end_x = end.get("x", 0)
        end_y = end.get("y", 0)

        if not hasattr(iface.ipc_board_api, "add_arc_track"):
            return {
                "success": False,
                "message": "IPC backend does not support arc track on this installation",
            }

        success = iface.ipc_board_api.add_arc_track(
            start_x=start_x,
            start_y=start_y,
            mid_x=mid_x,
            mid_y=mid_y,
            end_x=end_x,
            end_y=end_y,
            width=width,
            layer=layer,
            net_name=net,
        )

        return {
            "success": success,
            "message": (
                "Added arc trace (visible in KiCAD UI)" if success else "Failed to add arc trace"
            ),
            "arc": {
                "start": {"x": start_x, "y": start_y, "unit": "mm"},
                "mid": {"x": mid_x, "y": mid_y, "unit": "mm"},
                "end": {"x": end_x, "y": end_y, "unit": "mm"},
                "layer": layer,
                "width": width,
                "net": net,
            },
        }
    except Exception as e:
        logger.error(f"IPC route_arc_trace error: {e}")
        return {"success": False, "message": str(e)}


def handle_add_via(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for add_via — adds via with real-time UI update."""
    try:
        x, y, _ = extract_xy(params)

        size = params.get("size", 0.8)
        drill = params.get("drill", 0.4)
        net = params.get("net")
        from_layer = params.get("from_layer", "F.Cu")
        to_layer = params.get("to_layer", "B.Cu")

        success = iface.ipc_board_api.add_via(
            x=x, y=y, diameter=size, drill=drill, net_name=net, via_type="through"
        )

        return {
            "success": success,
            "message": ("Added via (visible in KiCAD UI)" if success else "Failed to add via"),
            "via": {
                "position": {"x": x, "y": y, "unit": "mm"},
                "size": size,
                "drill": drill,
                "from_layer": from_layer,
                "to_layer": to_layer,
                "net": net,
            },
        }
    except Exception as e:
        logger.error(f"IPC add_via error: {e}")
        return {"success": False, "message": str(e)}


def handle_add_net(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for add_net."""
    # Note: Net creation via IPC is limited - nets are typically created
    # when components are placed. Return success for compatibility.
    name = params.get("name")
    logger.info(f"IPC add_net: {name} (nets auto-created with components)")
    return {
        "success": True,
        "message": f"Net '{name}' will be created when components are connected",
        "net": {"name": name},
    }


def handle_delete_trace(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for delete_trace.

    IPC doesn't support direct trace deletion yet, so fall back to SWIG —
    via swig_fallback_mutation, which keeps the cross-backend bookkeeping
    (conflict gate, auto-save, _swig_writes_landed) that the dispatcher
    only applies on its own SWIG branch.
    """
    logger.info("delete_trace: Falling back to SWIG (IPC doesn't support trace deletion)")
    return swig_fallback_mutation(
        iface, "delete_trace", iface.routing_commands.delete_trace, params
    )


def handle_query_traces(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for query_traces — reads traces from the live KiCAD board."""
    try:
        net_name = params.get("net")
        layer_filter = params.get("layer")
        bbox = params.get("boundingBox")
        include_vias = params.get("includeVias", False)

        def point_in_bbox(point: Dict[str, Any]) -> bool:
            if not bbox:
                return True
            # Use the shared scale table so "mil" isn't silently treated as mm —
            # the prior branch only recognised "inch".
            unit_scale = _TO_MM_SCALE.get(bbox.get("unit", "mm"), 1.0)
            x1 = bbox.get("x1", 0) * unit_scale
            y1 = bbox.get("y1", 0) * unit_scale
            x2 = bbox.get("x2", 0) * unit_scale
            y2 = bbox.get("y2", 0) * unit_scale
            low_x, high_x = sorted((x1, x2))
            low_y, high_y = sorted((y1, y2))
            return low_x <= point.get("x", 0) <= high_x and low_y <= point.get("y", 0) <= high_y

        traces = []
        for track in iface.ipc_board_api.get_tracks():
            if net_name and track.get("net") != net_name:
                continue

            layer = iface._normalize_ipc_layer_name(track.get("layer", ""))
            if layer_filter and layer != layer_filter:
                continue

            start = track.get("start", {})
            end = track.get("end", {})
            if bbox and not (point_in_bbox(start) or point_in_bbox(end)):
                continue

            start_with_unit = {**start, "unit": "mm"}
            end_with_unit = {**end, "unit": "mm"}
            dx = end.get("x", 0) - start.get("x", 0)
            dy = end.get("y", 0) - start.get("y", 0)
            traces.append(
                {
                    "uuid": track.get("id", ""),
                    "net": track.get("net", ""),
                    "netCode": track.get("netCode", 0),
                    "layer": layer,
                    "width": track.get("width", 0),
                    "start": start_with_unit,
                    "end": end_with_unit,
                    "length": (dx**2 + dy**2) ** 0.5,
                }
            )

        result: Dict[str, Any] = {"success": True, "traceCount": len(traces), "traces": traces}

        if include_vias:
            vias = []
            for via in iface.ipc_board_api.get_vias():
                if net_name and via.get("net") != net_name:
                    continue
                position = via.get("position", {})
                if bbox and not point_in_bbox(position):
                    continue
                vias.append(
                    {
                        "uuid": via.get("id", ""),
                        "position": {**position, "unit": "mm"},
                        "net": via.get("net", ""),
                        "netCode": via.get("netCode", 0),
                        "diameter": via.get("diameter", 0),
                        "drill": via.get("drill", 0),
                    }
                )
            result["viaCount"] = len(vias)
            result["vias"] = vias

        return result
    except Exception as e:
        logger.error(f"IPC query_traces error: {e}")
        return {"success": False, "message": str(e)}


def handle_get_nets_list(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for get_nets_list — gets nets with real-time data."""
    try:
        nets = iface.ipc_board_api.get_nets()

        return {"success": True, "nets": nets, "count": len(nets)}
    except Exception as e:
        logger.error(f"IPC get_nets_list error: {e}")
        return {"success": False, "message": str(e)}
