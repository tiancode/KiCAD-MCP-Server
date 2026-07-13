"""IPC fast-path: board size / outline / layers / text handlers.

Split out of the former handlers/ipc_fastpath.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.ipc_fastpath")

from ._common import extract_xy, swig_fallback_mutation


def handle_add_text(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for add_text / add_board_text — adds text with real-time UI update."""
    try:
        text = params.get("text", "")
        x, y, _ = extract_xy(params)
        layer = params.get("layer", "F.SilkS")
        size = params.get("size", 1.0)
        rotation = params.get("rotation", 0)

        # Auto-mirror back-layer text (P13): un-mirrored B.* text trips DRC's
        # nonmirrored_text_on_back_layer.  Resolve auto → concrete bool here so
        # the reported state is accurate regardless of the backend internals; an
        # explicit `mirror` param overrides.
        mirror_param = params.get("mirror")
        mirror_auto = mirror_param is None
        is_back_layer = str(layer).startswith("B.")
        mirror = is_back_layer if mirror_auto else bool(mirror_param)

        success = iface.ipc_board_api.add_text(
            text=text, x=x, y=y, layer=layer, size=size, rotation=rotation, mirror=mirror
        )

        return {
            "success": success,
            "message": (
                f"Added text '{text}' (visible in KiCAD UI)" if success else "Failed to add text"
            ),
            "mirror": mirror,
            "mirrorAuto": mirror_auto and is_back_layer,
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


def handle_add_board_outline(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for add_board_outline — adds board edge with real-time UI update.

    Rounded rectangles are delegated to the SWIG path because the IPC
    ``BoardSegment`` type cannot represent arcs; the SWIG path writes
    directly to the ``.kicad_pcb`` file and correctly generates
    ``PCB_SHAPE`` arcs for rounded corners.
    """
    shape = params.get("shape", "rectangle")
    # Unwrap nested params (Claude sends {"shape":..., "params":{...}})
    inner = params.get("params", params)

    # Assemble the outline as a closed list of corner points.  A rectangle
    # is four corners derived from width/height/x/y — drawable as straight
    # Edge.Cuts segments over IPC.  (The old code delegated rectangles to
    # the SWIG path, which has no board loaded in IPC mode and failed with
    # "No board is loaded".)
    points = list(inner.get("points", params.get("points", [])) or [])
    if shape == "rectangle" and not points:
        x0 = inner.get("x", params.get("x", 0))
        y0 = inner.get("y", params.get("y", 0))
        w = inner.get("width", params.get("width"))
        h = inner.get("height", params.get("height"))
        if w is not None and h is not None:
            points = [
                {"x": x0, "y": y0},
                {"x": x0 + w, "y": y0},
                {"x": x0 + w, "y": y0 + h},
                {"x": x0, "y": y0 + h},
            ]

    if not points:
        # rounded_rectangle / circle need arcs the IPC BoardSegment type
        # can't express — delegate to the SWIG path (needs a SWIG board).
        logger.info(f"handle_add_board_outline (IPC): delegating {shape} to SWIG path")
        return swig_fallback_mutation(
            iface, "add_board_outline", iface.board_commands.add_board_outline, params
        )

    try:
        from kipy.board_types import BoardSegment
        from kipy.geometry import Vector2
        from kipy.proto.board.board_types_pb2 import BoardLayer
        from kipy.util.units import from_mm

        board = iface.ipc_board_api._get_board()

        # Edge.Cuts stroke width (mm) — NOT the rectangle's width dimension.
        stroke_width = inner.get("lineWidth", params.get("lineWidth", 0.1))

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
            segment.attributes.stroke.width = from_mm(stroke_width)

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
    """IPC handler for add_mounting_hole — delegates to the SWIG implementation.

    The old inline IPC version had three defects the SWIG path doesn't:
    it read flat ``x``/``y`` keys while the tool schema sends a nested
    ``position`` object (every hole landed at (0, 0)); it opened its own
    kipy commit, conflicting with an open transaction; and it ignored
    ``padDiameter``/``plated`` entirely (drew a bare Edge.Cuts circle
    instead of an NPTH/PTH pad).  The SWIG implementation handles all of
    that, so route through swig_fallback_mutation — which keeps the
    cross-backend bookkeeping (conflict gate, auto-save,
    _swig_writes_landed) — and let the caller reconcile to refresh KiCad.
    """
    logger.info("add_mounting_hole: delegating to SWIG implementation")
    return swig_fallback_mutation(
        iface, "add_mounting_hole", iface.board_commands.add_mounting_hole, params
    )


def handle_get_layer_list(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for get_layer_list — gets enabled layers."""
    try:
        layers = iface.ipc_board_api.get_enabled_layers()

        return {"success": True, "layers": layers, "count": len(layers)}
    except Exception as e:
        logger.error(f"IPC get_layer_list error: {e}")
        return {"success": False, "message": str(e)}
