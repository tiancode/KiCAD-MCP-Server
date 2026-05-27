"""
IPC fast-path command handlers.

These are alternate implementations of the regular MCP commands
(``route_trace``, ``place_component``, …) that mutate via KiCAD's live
IPC API (``iface.ipc_board_api``) instead of the SWIG ``pcbnew`` proxy.
They are dispatched from :py:meth:`KiCADInterface.handle_command`
whenever the IPC backend is connected and the incoming MCP command
appears in ``IPC_CAPABLE_COMMANDS`` on the interface class.

These are intentionally separate from :mod:`handlers.ipc`, which holds
the *explicit* ``ipc_*`` MCP commands (``ipc_add_track``, …).  The
fast-path handlers in this module share their MCP command name with the
SWIG path — the only difference is which backend serves the request.

The ``handle_*`` names line up with the ``_ipc_<suffix>`` method names
that used to live inline on :class:`KiCADInterface`: e.g. the function
formerly known as ``_ipc_route_trace`` is now :func:`handle_route_trace`.
The class's ``__getattr__`` trampoline still answers ``iface._ipc_<X>``
lookups by resolving to ``handle_<X>`` in this module, so tests that
poke at the private attribute keep working unchanged.

The ``_ipc_delete_trace`` / ``_ipc_add_board_outline`` paths intentionally
fall back to ``iface.routing_commands`` / ``iface.board_commands`` for
shapes the IPC API can't represent — that behaviour is preserved here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


# Length conversion factors → millimetres.
# inch = 25.4 mm; mil = 0.001 inch = 0.0254 mm.  Unknown units pass through
# unchanged at the to_mm() call site rather than raise — schema validation
# upstream is the right place to reject bad units.
_TO_MM_SCALE = {"mm": 1.0, "inch": 25.4, "mil": 0.0254}


def to_mm(value: Any, unit: str) -> Any:
    """Convert ``value`` from ``unit`` to millimetres.

    Preserves the input type when ``unit == "mm"`` (so an int passed in as
    mm comes back as an int) — IPC consumers accept either, but keeping the
    original shape avoids accidental float coercion changes downstream.
    """
    scale = _TO_MM_SCALE.get(unit, 1.0)
    return value if scale == 1.0 else value * scale


def extract_xy(
    params: Dict[str, Any],
    key: str = "position",
    flat_x: str = "x",
    flat_y: str = "y",
    default_unit: str = "mm",
) -> Tuple[Any, Any, str]:
    """Pull (x, y, unit) out of ``params``.

    Accepts both nested-dict and flat-top-level shapes:

        {"position": {"x": 1, "y": 2, "unit": "mm"}}    # preferred
        {"x": 1, "y": 2}                                # legacy flat form

    For commands whose flat names aren't ``x``/``y`` (e.g. route_trace uses
    ``startX``/``startY``), pass ``flat_x``/``flat_y`` explicitly.  The
    ``unit`` is only meaningful when the nested form is used; the flat form
    has no place to carry it, so it falls back to ``default_unit``.
    """
    nested = params.get(key)
    if isinstance(nested, dict):
        return (
            nested.get("x", 0),
            nested.get("y", 0),
            nested.get("unit", default_unit),
        )
    return params.get(flat_x, 0), params.get(flat_y, 0), default_unit


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
        layer = params.get("layer", "F.Cu")
        net = params.get("net")
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
        _to_mm = {"mm": 1.0, "inch": 25.4, "mil": 0.0254}
        top_unit = str(params.get("unit", "mm")).lower()

        def _pt_scale(p: Dict[str, Any]) -> float:
            return _to_mm.get(str(p.get("unit", top_unit)).lower(), 1.0)

        formatted_points = [
            {"x": p.get("x", 0) * _pt_scale(p), "y": p.get("y", 0) * _pt_scale(p)}
            for p in points
        ]

        success = iface.ipc_board_api.add_zone(
            points=formatted_points,
            layer=layer,
            net_name=net,
            clearance=clearance,
            min_thickness=min_width,
            priority=priority,
            fill_mode=fill_type,
            name=name,
        )

        return {
            "success": success,
            "message": (
                "Added copper pour (visible in KiCAD UI)"
                if success
                else "Failed to add copper pour"
            ),
            "pour": {
                "layer": layer,
                "net": net,
                "clearance": clearance,
                "minWidth": min_width,
                "priority": priority,
                "fillType": fill_type,
                "pointCount": len(formatted_points),
            },
        }
    except Exception as e:
        logger.error(f"IPC add_copper_pour error: {e}")
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


def handle_add_text(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for add_text / add_board_text — adds text with real-time UI update."""
    try:
        text = params.get("text", "")
        x, y, _ = extract_xy(params)
        layer = params.get("layer", "F.SilkS")
        size = params.get("size", 1.0)
        rotation = params.get("rotation", 0)

        success = iface.ipc_board_api.add_text(
            text=text, x=x, y=y, layer=layer, size=size, rotation=rotation
        )

        return {
            "success": success,
            "message": (
                f"Added text '{text}' (visible in KiCAD UI)" if success else "Failed to add text"
            ),
        }
    except Exception as e:
        logger.error(f"IPC add_text error: {e}")
        return {"success": False, "message": str(e)}


def handle_set_board_size(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for set_board_size."""
    try:
        width = params.get("width", 100)
        height = params.get("height", 100)
        unit = params.get("unit", "mm")

        success = iface.ipc_board_api.set_size(width, height, unit)

        return {
            "success": success,
            "message": (
                f"Board size set to {width}x{height} {unit} (visible in KiCAD UI)"
                if success
                else "Failed to set board size"
            ),
            "boardSize": {"width": width, "height": height, "unit": unit},
        }
    except Exception as e:
        logger.error(f"IPC set_board_size error: {e}")
        return {"success": False, "message": str(e)}


def handle_get_board_info(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for get_board_info."""
    try:
        size = iface.ipc_board_api.get_size()
        components = iface.ipc_board_api.list_components()
        tracks = iface.ipc_board_api.get_tracks()
        vias = iface.ipc_board_api.get_vias()
        nets = iface.ipc_board_api.get_nets()

        return {
            "success": True,
            "boardInfo": {
                "size": size,
                "componentCount": len(components),
                "trackCount": len(tracks),
                "viaCount": len(vias),
                "netCount": len(nets),
                "backend": "ipc",
                "realtime": True,
            },
        }
    except Exception as e:
        logger.error(f"IPC get_board_info error: {e}")
        return {"success": False, "message": str(e)}


def handle_place_component(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for place_component — places component with real-time UI update.

    Refuses to run inside an open IPC transaction.  ``place_component``
    loads library footprints through pcbnew SWIG, which writes the
    placement directly to the .kicad_pcb file and then calls
    ``board.revert()`` to re-sync the IPC view.  That revert invalidates
    the open commit handle, *and* the placement is already persisted to
    disk — so a subsequent ``rollback_transaction`` can't undo it.  The
    atomicity contract would silently break, so fail fast instead.
    """
    api = iface.ipc_board_api
    if api is not None and getattr(api, "_current_commit", None) is not None:
        return {
            "success": False,
            "message": (
                "place_component cannot run inside an IPC transaction: it "
                "uses a SWIG fallback to load library footprints which writes "
                "directly to disk and reloads the in-memory board, "
                "invalidating the open commit. Commit or rollback the "
                "transaction first, then place the component."
            ),
        }
    try:
        reference = params.get("reference", params.get("componentId", ""))
        footprint = params.get("footprint", "")
        # ipc_backend expects mm — normalise whatever the caller sent.
        x, y, unit = extract_xy(params)
        x, y = to_mm(x, unit), to_mm(y, unit)
        rotation = params.get("rotation", 0)
        layer = params.get("layer", "F.Cu")
        value = params.get("value", "")

        success = iface.ipc_board_api.place_component(
            reference=reference,
            footprint=footprint,
            x=x,
            y=y,
            rotation=rotation,
            layer=layer,
            value=value,
        )

        return {
            "success": success,
            "message": (
                f"Placed component {reference} (visible in KiCAD UI)"
                if success
                else "Failed to place component"
            ),
            "component": {
                "reference": reference,
                "footprint": footprint,
                "position": {"x": x, "y": y, "unit": "mm"},
                "rotation": rotation,
                "layer": layer,
            },
        }
    except Exception as e:
        logger.error(f"IPC place_component error: {e}")
        return {"success": False, "message": str(e)}


def handle_move_component(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for move_component — moves component with real-time UI update."""
    try:
        reference = params.get("reference", params.get("componentId", ""))
        # ipc_backend.move_component expects mm — normalise the caller's unit.
        x, y, unit = extract_xy(params)
        x, y = to_mm(x, unit), to_mm(y, unit)
        rotation = params.get("rotation")

        success = iface.ipc_board_api.move_component(
            reference=reference, x=x, y=y, rotation=rotation
        )

        return {
            "success": success,
            "message": (
                f"Moved component {reference} (visible in KiCAD UI)"
                if success
                else "Failed to move component"
            ),
        }
    except Exception as e:
        logger.error(f"IPC move_component error: {e}")
        return {"success": False, "message": str(e)}


def handle_delete_component(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for delete_component — deletes component with real-time UI update."""
    try:
        reference = params.get("reference", params.get("componentId", ""))

        success = iface.ipc_board_api.delete_component(reference=reference)

        return {
            "success": success,
            "message": (
                f"Deleted component {reference} (visible in KiCAD UI)"
                if success
                else "Failed to delete component"
            ),
        }
    except Exception as e:
        logger.error(f"IPC delete_component error: {e}")
        return {"success": False, "message": str(e)}


def handle_get_component_list(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for get_component_list.

    Every field in a returned component comes from the live IPC view of the
    board — never from the SWIG on-disk copy.  An earlier version patched
    missing ``boundingBox`` values from ``iface.board`` (SWIG), but SWIG
    holds the pre-IPC-mutation positions, so a component that just moved via
    ``move_component`` came back with a fresh ``position`` and a stale
    ``boundingBox`` pointing at where it used to be.  When IPC can't supply
    a box, leave it ``null`` rather than mix two sources in one record.
    """
    try:
        components = iface.ipc_board_api.list_components()
        return {"success": True, "components": components, "count": len(components)}
    except Exception as e:
        logger.error(f"IPC get_component_list error: {e}")
        return {"success": False, "message": str(e)}


def handle_save_project(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for save_project."""
    try:
        success = iface.ipc_board_api.save()

        return {
            "success": success,
            "message": "Project saved" if success else "Failed to save project",
        }
    except Exception as e:
        logger.error(f"IPC save_project error: {e}")
        return {"success": False, "message": str(e)}


def handle_delete_trace(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for delete_trace.

    IPC doesn't support direct trace deletion yet, so fall back to SWIG.
    """
    logger.info("delete_trace: Falling back to SWIG (IPC doesn't support trace deletion)")
    return iface.routing_commands.delete_trace(params)


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


def handle_add_board_outline(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for add_board_outline — adds board edge with real-time UI update.

    Rounded rectangles are delegated to the SWIG path because the IPC
    ``BoardSegment`` type cannot represent arcs; the SWIG path writes
    directly to the ``.kicad_pcb`` file and correctly generates
    ``PCB_SHAPE`` arcs for rounded corners.
    """
    shape = params.get("shape", "rectangle")
    if shape in ("rounded_rectangle", "rectangle"):
        # IPC path only supports straight segments from a points list,
        # but Claude sends rectangle/rounded_rectangle as shape+width+height.
        # Fall back to the SWIG path which correctly handles both shapes.
        logger.info(f"handle_add_board_outline (IPC): delegating {shape} to SWIG path")
        return iface.board_commands.add_board_outline(params)

    try:
        from kipy.board_types import BoardSegment
        from kipy.geometry import Vector2
        from kipy.proto.board.board_types_pb2 import BoardLayer
        from kipy.util.units import from_mm

        board = iface.ipc_board_api._get_board()

        # Unwrap nested params (Claude sends {"shape":..., "params":{...}})
        inner = params.get("params", params)
        points = inner.get("points", params.get("points", []))
        width = inner.get("width", params.get("width", 0.1))

        if len(points) < 2:
            return {
                "success": False,
                "message": "At least 2 points required for board outline",
            }

        commit = board.begin_commit()
        lines_created = 0

        # Create line segments connecting the points
        for i in range(len(points)):
            start = points[i]
            end = points[(i + 1) % len(points)]  # Wrap around to close the outline

            segment = BoardSegment()
            segment.start = Vector2.from_xy(from_mm(start.get("x", 0)), from_mm(start.get("y", 0)))
            segment.end = Vector2.from_xy(from_mm(end.get("x", 0)), from_mm(end.get("y", 0)))
            segment.layer = BoardLayer.BL_Edge_Cuts
            segment.attributes.stroke.width = from_mm(width)

            board.create_items(segment)
            lines_created += 1

        board.push_commit(commit, "Added board outline")

        return {
            "success": True,
            "message": f"Added board outline with {lines_created} segments (visible in KiCAD UI)",
            "segments": lines_created,
        }
    except Exception as e:
        logger.error(f"IPC add_board_outline error: {e}")
        return {"success": False, "message": str(e)}


def handle_add_mounting_hole(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for add_mounting_hole — adds mounting hole with real-time UI update."""
    try:
        from kipy.board_types import BoardCircle
        from kipy.geometry import Vector2
        from kipy.proto.board.board_types_pb2 import BoardLayer
        from kipy.util.units import from_mm

        board = iface.ipc_board_api._get_board()

        x = params.get("x", 0)
        y = params.get("y", 0)
        diameter = params.get("diameter", 3.2)  # M3 hole default

        commit = board.begin_commit()

        # Create circle on Edge.Cuts layer for the hole
        circle = BoardCircle()
        circle.center = Vector2.from_xy(from_mm(x), from_mm(y))
        circle.radius = from_mm(diameter / 2)
        circle.layer = BoardLayer.BL_Edge_Cuts
        circle.attributes.stroke.width = from_mm(0.1)

        board.create_items(circle)
        board.push_commit(commit, f"Added mounting hole at ({x}, {y})")

        return {
            "success": True,
            "message": f"Added mounting hole at ({x}, {y}) mm (visible in KiCAD UI)",
            "hole": {"position": {"x": x, "y": y}, "diameter": diameter},
        }
    except Exception as e:
        logger.error(f"IPC add_mounting_hole error: {e}")
        return {"success": False, "message": str(e)}


def handle_get_layer_list(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for get_layer_list — gets enabled layers."""
    try:
        layers = iface.ipc_board_api.get_enabled_layers()

        return {"success": True, "layers": layers, "count": len(layers)}
    except Exception as e:
        logger.error(f"IPC get_layer_list error: {e}")
        return {"success": False, "message": str(e)}


def handle_rotate_component(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for rotate_component — rotates component with real-time UI update."""
    try:
        reference = params.get("reference", params.get("componentId", ""))
        angle = params.get("angle", params.get("rotation", 90))

        # Get current component to find its position
        components = iface.ipc_board_api.list_components()
        target = None
        for comp in components:
            if comp.get("reference") == reference:
                target = comp
                break

        if not target:
            return {"success": False, "message": f"Component {reference} not found"}

        # Use angle as absolute rotation (matches schema description)
        new_rotation = angle % 360

        # Use move_component with new rotation (position stays the same)
        success = iface.ipc_board_api.move_component(
            reference=reference,
            x=target.get("position", {}).get("x", 0),
            y=target.get("position", {}).get("y", 0),
            rotation=new_rotation,
        )

        return {
            "success": success,
            "message": (
                f"Rotated component {reference} by {angle}° (visible in KiCAD UI)"
                if success
                else "Failed to rotate component"
            ),
            "newRotation": new_rotation,
        }
    except Exception as e:
        logger.error(f"IPC rotate_component error: {e}")
        return {"success": False, "message": str(e)}


def handle_get_component_properties(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """IPC handler for get_component_properties — gets detailed component info.

    Like ``handle_get_component_list``, this returns a pure IPC view.  The
    earlier SWIG-fallback for ``boundingBox`` / ``courtyard`` mixed live
    positions with on-disk geometry — a component that just moved via
    ``move_component`` came back with the new ``position`` and the old
    ``boundingBox``.  When IPC doesn't have the box, leave it ``null``
    rather than serve two coordinate frames in one record.
    """
    try:
        reference = params.get("reference", params.get("componentId", ""))

        components = iface.ipc_board_api.list_components()
        target = None
        for comp in components:
            if comp.get("reference") == reference:
                target = comp
                break

        if not target:
            return {"success": False, "message": f"Component {reference} not found"}

        return {"success": True, "component": target}
    except Exception as e:
        logger.error(f"IPC get_component_properties error: {e}")
        return {"success": False, "message": str(e)}
