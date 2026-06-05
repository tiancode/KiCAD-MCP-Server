"""IPCBoardAPI graphic-shape drawing operations.

Split out of the former monolithic kicad_api/ipc_backend.py.
"""

import logging
import os
import platform
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from kicad_api.base import APINotAvailableError, BoardAPI, ConnectionError, KiCADBackend

from ._helpers import (
    INCH_TO_NM,
    MM_TO_NM,
    _document_type_enum,
    get_open_documents_compat,
    has_open_pcb_document,
)

logger = logging.getLogger("kicad_interface")


class _ShapeMixin:
    # ------------------------------------------------------------------
    # Drawing primitives — graphic shapes on any layer.
    #
    # These are *graphic* shapes (no net association unless layer is Cu).
    # For copper traces use add_track / route_trace; for filled copper use
    # add_zone.  Routed *arc tracks* (copper) live on add_arc_track —
    # add_arc here is the graphic version for silk / fab / Edge.Cuts /
    # User layers.
    # ------------------------------------------------------------------
    def add_segment(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        width: float = 0.15,
        layer: str = "F.SilkS",
    ) -> Dict[str, Any]:
        """Add a straight graphic line on any layer."""
        try:
            from kipy.board_types import BoardSegment
            from kipy.geometry import Vector2
            from kipy.util.units import from_mm

            board = self._get_board()
            seg = BoardSegment()
            seg.start = Vector2.from_xy(from_mm(start_x), from_mm(start_y))
            seg.end = Vector2.from_xy(from_mm(end_x), from_mm(end_y))
            seg.layer = self._layer_to_enum(layer)
            seg.attributes.stroke.width = from_mm(width)
            created_id = self._apply_create(board, seg, f"Added segment on {layer}")
            self._notify(
                "shape_added",
                {
                    "kind": "segment",
                    "layer": layer,
                    "start": {"x": start_x, "y": start_y},
                    "end": {"x": end_x, "y": end_y},
                },
            )
            return {"success": True, "id": created_id, "layer": layer}
        except Exception as e:
            logger.error(f"Failed to add segment: {e}")
            return {"success": False, "message": str(e)}

    def add_arc(
        self,
        start_x: float,
        start_y: float,
        mid_x: float,
        mid_y: float,
        end_x: float,
        end_y: float,
        width: float = 0.15,
        layer: str = "F.SilkS",
    ) -> Dict[str, Any]:
        """Add a graphic arc on any layer (start → mid → end)."""
        try:
            from kipy.board_types import BoardArc
            from kipy.geometry import Vector2
            from kipy.util.units import from_mm

            board = self._get_board()
            arc = BoardArc()
            arc.start = Vector2.from_xy(from_mm(start_x), from_mm(start_y))
            arc.mid = Vector2.from_xy(from_mm(mid_x), from_mm(mid_y))
            arc.end = Vector2.from_xy(from_mm(end_x), from_mm(end_y))
            arc.layer = self._layer_to_enum(layer)
            arc.attributes.stroke.width = from_mm(width)
            created_id = self._apply_create(board, arc, f"Added arc on {layer}")
            self._notify(
                "shape_added",
                {
                    "kind": "arc",
                    "layer": layer,
                    "start": {"x": start_x, "y": start_y},
                    "mid": {"x": mid_x, "y": mid_y},
                    "end": {"x": end_x, "y": end_y},
                },
            )
            return {"success": True, "id": created_id, "layer": layer}
        except Exception as e:
            logger.error(f"Failed to add arc: {e}")
            return {"success": False, "message": str(e)}

    def add_circle(
        self,
        center_x: float,
        center_y: float,
        radius: float,
        width: float = 0.15,
        layer: str = "F.SilkS",
        filled: bool = False,
    ) -> Dict[str, Any]:
        """Add a graphic circle on any layer.

        ``filled=True`` produces a solid disc (radius is the disc radius);
        ``filled=False`` produces a stroked ring of the given ``width``.
        """
        try:
            from kipy.board_types import BoardCircle
            from kipy.geometry import Vector2
            from kipy.util.units import from_mm

            board = self._get_board()
            circle = BoardCircle()
            circle.center = Vector2.from_xy(from_mm(center_x), from_mm(center_y))
            # radius is given as a "point on the circle" in kipy — pick a
            # canonical one to the right of centre.
            circle.radius_point = Vector2.from_xy(from_mm(center_x + radius), from_mm(center_y))
            circle.layer = self._layer_to_enum(layer)
            circle.attributes.stroke.width = from_mm(width)
            circle.attributes.fill.filled = bool(filled)
            created_id = self._apply_create(board, circle, f"Added circle on {layer}")
            self._notify(
                "shape_added",
                {
                    "kind": "circle",
                    "layer": layer,
                    "center": {"x": center_x, "y": center_y},
                    "radius": radius,
                    "filled": filled,
                },
            )
            return {"success": True, "id": created_id, "layer": layer}
        except Exception as e:
            logger.error(f"Failed to add circle: {e}")
            return {"success": False, "message": str(e)}

    def add_rectangle(
        self,
        top_left_x: float,
        top_left_y: float,
        bottom_right_x: float,
        bottom_right_y: float,
        width: float = 0.15,
        layer: str = "F.SilkS",
        filled: bool = False,
    ) -> Dict[str, Any]:
        """Add a graphic rectangle on any layer (axis-aligned)."""
        try:
            from kipy.board_types import BoardRectangle
            from kipy.geometry import Vector2
            from kipy.util.units import from_mm

            board = self._get_board()
            rect = BoardRectangle()
            rect.top_left = Vector2.from_xy(from_mm(top_left_x), from_mm(top_left_y))
            rect.bottom_right = Vector2.from_xy(from_mm(bottom_right_x), from_mm(bottom_right_y))
            rect.layer = self._layer_to_enum(layer)
            rect.attributes.stroke.width = from_mm(width)
            rect.attributes.fill.filled = bool(filled)
            created_id = self._apply_create(board, rect, f"Added rectangle on {layer}")
            self._notify(
                "shape_added",
                {
                    "kind": "rectangle",
                    "layer": layer,
                    "topLeft": {"x": top_left_x, "y": top_left_y},
                    "bottomRight": {"x": bottom_right_x, "y": bottom_right_y},
                    "filled": filled,
                },
            )
            return {"success": True, "id": created_id, "layer": layer}
        except Exception as e:
            logger.error(f"Failed to add rectangle: {e}")
            return {"success": False, "message": str(e)}

    def add_polygon(
        self,
        points: List[Dict[str, float]],
        width: float = 0.15,
        layer: str = "F.SilkS",
        filled: bool = False,
    ) -> Dict[str, Any]:
        """Add a closed graphic polygon on any layer.

        ``points`` is a list of ``{"x": ..., "y": ...}`` in mm.  At least 3
        points are required.  ``filled=True`` produces a solid polygon;
        ``filled=False`` produces a stroked outline of the given ``width``.
        """
        try:
            from kipy.board_types import BoardPolygon
            from kipy.util.units import from_mm

            if len(points) < 3:
                return {"success": False, "message": "Polygon requires at least 3 points"}

            board = self._get_board()
            poly = BoardPolygon()
            # Write the polygon outline through the proto directly — the
            # kipy wrapper's `polygons` list is a one-way cache that doesn't
            # round-trip into the proto on append.  Same trick the existing
            # add_zone() code uses for Zone outlines.
            pwh_proto = poly._proto.shape.polygon.polygons.add()
            pwh_proto.outline.closed = True
            for pt in points:
                px = float(pt.get("x", 0))
                py = float(pt.get("y", 0))
                node = pwh_proto.outline.nodes.add()
                node.point.x_nm = from_mm(px)
                node.point.y_nm = from_mm(py)
            poly.layer = self._layer_to_enum(layer)
            poly.attributes.stroke.width = from_mm(width)
            poly.attributes.fill.filled = bool(filled)
            created_id = self._apply_create(board, poly, f"Added polygon on {layer}")
            self._notify(
                "shape_added",
                {
                    "kind": "polygon",
                    "layer": layer,
                    "points": len(points),
                    "filled": filled,
                },
            )
            return {"success": True, "id": created_id, "layer": layer}
        except Exception as e:
            logger.error(f"Failed to add polygon: {e}")
            return {"success": False, "message": str(e)}
