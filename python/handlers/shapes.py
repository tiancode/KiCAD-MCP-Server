"""
Graphic shape handlers (IPC-only).

These cover the BoardShape primitive types that the existing tool set
left out — straight segments, arcs, circles, rectangles, and polygons
on any layer (silk / fab / Edge.Cuts / User.* etc.).  The list / edit /
delete handlers also manage free board TEXT items (kind "text" /
"textbox", e.g. the gr_text placed by add_board_text).

Naming distinction vs. routing:
- ``add_segment`` / ``add_arc`` here are *graphic* shapes (no net).
- For *copper traces* use ``route_trace`` / ``route_arc_trace`` (they
  bind a net and route through the autorouter primitives).
- For copper fills use ``add_zone``.

All commands require the IPC backend.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

from handlers.ipc_gate import require_ipc

logger = logging.getLogger(__name__)


def _ipc_unavailable(reason: str = "") -> Dict[str, Any]:
    base = (
        "Shape commands require the IPC backend. Launch KiCAD with "
        "Preferences > Plugins > Enable IPC API Server, then retry."
    )
    return {"success": False, "message": f"{base} ({reason})" if reason else base}


def _require_ipc(iface: "KiCADInterface", *, read_only: bool = False) -> Dict[str, Any]:
    """Gate shape ops on IPC + an open PCB editor frame.

    ``read_only=True`` (list_shapes) skips the cross-backend conflict
    refusal — reads can't lose data; the dispatcher stamps staleVsDisk."""
    return require_ipc(iface, _ipc_unavailable, read_only=read_only)


def _xy(params: Dict[str, Any], key: str, fallback_x: str, fallback_y: str) -> tuple:
    """Pull (x, y) from either ``params[key] = {x, y}`` or flat top-level."""
    nested = params.get(key)
    if isinstance(nested, dict):
        return float(nested.get("x", 0)), float(nested.get("y", 0))
    return float(params.get(fallback_x, 0)), float(params.get(fallback_y, 0))


def handle_add_segment(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Graphic line. Accepts {start:{x,y}, end:{x,y}} or flat startX/startY/endX/endY."""
    gate = _require_ipc(iface)
    if gate:
        return gate
    sx, sy = _xy(params, "start", "startX", "startY")
    ex, ey = _xy(params, "end", "endX", "endY")
    return iface.ipc_board_api.add_segment(
        start_x=sx,
        start_y=sy,
        end_x=ex,
        end_y=ey,
        width=float(params.get("width", 0.15)),
        layer=str(params.get("layer", "F.SilkS")),
    )


def handle_add_arc(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Graphic arc through three points: start → mid → end."""
    gate = _require_ipc(iface)
    if gate:
        return gate
    sx, sy = _xy(params, "start", "startX", "startY")
    mx, my = _xy(params, "mid", "midX", "midY")
    ex, ey = _xy(params, "end", "endX", "endY")
    return iface.ipc_board_api.add_arc(
        start_x=sx,
        start_y=sy,
        mid_x=mx,
        mid_y=my,
        end_x=ex,
        end_y=ey,
        width=float(params.get("width", 0.15)),
        layer=str(params.get("layer", "F.SilkS")),
    )


def handle_add_circle(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Graphic circle. Accepts {center:{x,y}, radius, ...} or flat centerX/centerY."""
    gate = _require_ipc(iface)
    if gate:
        return gate
    cx, cy = _xy(params, "center", "centerX", "centerY")
    return iface.ipc_board_api.add_circle(
        center_x=cx,
        center_y=cy,
        radius=float(params.get("radius", 1.0)),
        width=float(params.get("width", 0.15)),
        layer=str(params.get("layer", "F.SilkS")),
        filled=bool(params.get("filled", False)),
    )


def handle_add_rectangle(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Graphic axis-aligned rectangle. Accepts {topLeft, bottomRight} or flat coords."""
    gate = _require_ipc(iface)
    if gate:
        return gate
    tlx, tly = _xy(params, "topLeft", "topLeftX", "topLeftY")
    brx, bry = _xy(params, "bottomRight", "bottomRightX", "bottomRightY")
    return iface.ipc_board_api.add_rectangle(
        top_left_x=tlx,
        top_left_y=tly,
        bottom_right_x=brx,
        bottom_right_y=bry,
        width=float(params.get("width", 0.15)),
        layer=str(params.get("layer", "F.SilkS")),
        filled=bool(params.get("filled", False)),
    )


def handle_add_polygon(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Closed graphic polygon. ``points`` must be a list of {x, y} (≥3)."""
    gate = _require_ipc(iface)
    if gate:
        return gate
    points: List[Dict[str, float]] = params.get("points") or []
    if not isinstance(points, list):
        return {"success": False, "message": "'points' must be a list of {x, y} dicts"}
    return iface.ipc_board_api.add_polygon(
        points=points,
        width=float(params.get("width", 0.15)),
        layer=str(params.get("layer", "F.SilkS")),
        filled=bool(params.get("filled", False)),
    )


def _normalize_bbox(bbox):
    if bbox is None:
        return None
    return {
        "x1": min(float(bbox.get("x1", 0)), float(bbox.get("x2", 0))),
        "y1": min(float(bbox.get("y1", 0)), float(bbox.get("y2", 0))),
        "x2": max(float(bbox.get("x1", 0)), float(bbox.get("x2", 0))),
        "y2": max(float(bbox.get("y1", 0)), float(bbox.get("y2", 0))),
    }


def handle_list_shapes(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """List graphic shapes and board text with optional layer/kind/boundingBox filters."""
    gate = _require_ipc(iface, read_only=True)
    if gate:
        return gate
    bbox = _normalize_bbox(params.get("boundingBox"))
    return iface.ipc_board_api.list_shapes(
        layer=params.get("layer"),
        kind=params.get("kind"),
        bbox=bbox,
    )


def handle_delete_shape(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Delete shapes / board text by id(s) or layer / kind / boundingBox filters."""
    gate = _require_ipc(iface)
    if gate:
        return gate
    ids = params.get("ids")
    single = params.get("id")
    bbox = _normalize_bbox(params.get("boundingBox"))
    # Other usable selectors alongside a (possibly blank) `id`.
    has_other_selector = (
        bool(ids) or bool(params.get("layer")) or bool(params.get("kind")) or bbox is not None
    )
    # An explicitly-passed but blank id is a bad argument ONLY when it is the
    # SOLE selector: give it a truthful VALIDATION code (never INTERNAL_ERROR)
    # with the exact remedy. Clients that serialize optional fields as ""/null
    # often ALSO pass a valid ids[]/layer/kind/bbox filter — don't regress those
    # (ignore the falsy id and proceed, the old behavior).
    blank_id = "id" in params and (
        single is None or (isinstance(single, str) and not single.strip())
    )
    if blank_id and not has_other_selector:
        return {
            "success": False,
            "errorCode": "VALIDATION",
            "message": "id must be a non-empty string from list_shapes",
        }
    if single and not ids:
        ids = [single]
    if not ids and not params.get("layer") and not params.get("kind") and bbox is None:
        return {
            "success": False,
            "errorCode": "VALIDATION",
            "message": (
                "Select shapes to delete: pass id/ids (from list_shapes) or "
                "layer / kind / boundingBox filters"
            ),
        }
    return iface.ipc_board_api.delete_shapes(
        ids=ids,
        layer=params.get("layer"),
        kind=params.get("kind"),
        bbox=bbox,
        delete_all=bool(params.get("all", False)),
    )


def handle_edit_shape(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Edit one shape or board-text item (by id): newLayer, width, filled,
    move {dx, dy}, plus text / size for text items."""
    gate = _require_ipc(iface)
    if gate:
        return gate
    shape_id = params.get("id")
    if not shape_id:
        return {"success": False, "message": "id is required (from list_shapes)"}
    return iface.ipc_board_api.edit_shape(
        shape_id=str(shape_id),
        new_layer=params.get("newLayer"),
        width=params.get("width"),
        filled=params.get("filled"),
        move=params.get("move"),
        text=params.get("text"),
        size=params.get("size"),
    )
