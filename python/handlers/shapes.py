"""
Graphic shape handlers (IPC-only).

These cover the BoardShape primitive types that the existing tool set
left out — straight segments, arcs, circles, rectangles, and polygons
on any layer (silk / fab / Edge.Cuts / User.* etc.).

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

logger = logging.getLogger(__name__)


def _ipc_unavailable(reason: str = "") -> Dict[str, Any]:
    base = (
        "Shape commands require the IPC backend. Launch KiCAD with "
        "Preferences > Plugins > Enable IPC API Server, then retry."
    )
    return {"success": False, "message": f"{base} ({reason})" if reason else base}


def _require_ipc(iface: "KiCADInterface") -> Dict[str, Any]:
    if iface.use_ipc and iface.ipc_board_api:
        return {}
    ok, reason = iface.ensure_ipc(allow_launch=True)
    if ok:
        return {}
    return _ipc_unavailable(reason)


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
