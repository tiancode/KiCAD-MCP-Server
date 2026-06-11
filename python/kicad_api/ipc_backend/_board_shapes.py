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

    # ------------------------------------------------------------------
    # Shape queries / mutation — list, delete, edit existing shapes.
    # ------------------------------------------------------------------

    _SHAPE_KIND_BY_TYPE = {
        "BoardSegment": "segment",
        "BoardArc": "arc",
        "BoardCircle": "circle",
        "BoardRectangle": "rectangle",
        "BoardPolygon": "polygon",
    }

    def _describe_shape(self, board: Any, shape: Any) -> Dict[str, Any]:
        from kipy.util.units import to_mm

        from ._helpers import kiid_str, normalize_board_layer

        info: Dict[str, Any] = {
            "id": kiid_str(getattr(shape, "id", None)),
            "kind": self._SHAPE_KIND_BY_TYPE.get(
                shape.__class__.__name__, shape.__class__.__name__
            ),
            "layer": normalize_board_layer(getattr(shape, "layer", None)),
        }
        try:
            info["width"] = to_mm(shape.attributes.stroke.width)
        except Exception:
            pass
        try:
            info["filled"] = bool(shape.attributes.fill.filled)
        except Exception:
            pass
        try:
            bbox = board.get_item_bounding_box(shape)
            if bbox:
                left, top, right, bottom = self._get_box2_extents(bbox)
                info["boundingBox"] = {
                    "x1": to_mm(left),
                    "y1": to_mm(top),
                    "x2": to_mm(right),
                    "y2": to_mm(bottom),
                    "unit": "mm",
                }
        except Exception:
            pass
        return info

    def _match_shapes(
        self,
        board: Any,
        ids: Optional[List[str]] = None,
        layer: Optional[str] = None,
        kind: Optional[str] = None,
        bbox: Optional[Dict[str, float]] = None,
    ) -> List[Any]:
        """Resolve shapes by id list and/or layer / kind / bbox-overlap filters."""
        from kipy.util.units import to_mm

        from ._helpers import kiid_str

        wanted_ids = set(ids) if ids else None
        layer_enum = self._layer_to_enum(layer) if layer else None
        matches = []
        for shape in board.get_shapes():
            if wanted_ids is not None and kiid_str(getattr(shape, "id", None)) not in wanted_ids:
                continue
            if layer_enum is not None and getattr(shape, "layer", None) != layer_enum:
                continue
            if kind is not None:
                shape_kind = self._SHAPE_KIND_BY_TYPE.get(
                    shape.__class__.__name__, shape.__class__.__name__
                )
                if shape_kind != kind:
                    continue
            if bbox is not None:
                try:
                    item_box = board.get_item_bounding_box(shape)
                    left, top, right, bottom = self._get_box2_extents(item_box)
                    if (
                        to_mm(right) < bbox["x1"]
                        or to_mm(left) > bbox["x2"]
                        or to_mm(bottom) < bbox["y1"]
                        or to_mm(top) > bbox["y2"]
                    ):
                        continue
                except Exception:
                    continue
            matches.append(shape)
        return matches

    def list_shapes(
        self,
        layer: Optional[str] = None,
        kind: Optional[str] = None,
        bbox: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """List graphic shapes with optional layer / kind / bbox filters."""
        try:
            board = self._get_board()
            shapes = self._match_shapes(board, layer=layer, kind=kind, bbox=bbox)
            described = [self._describe_shape(board, s) for s in shapes]
            return {"success": True, "shapeCount": len(described), "shapes": described}
        except Exception as e:
            logger.error(f"Failed to list shapes: {e}")
            return {"success": False, "message": str(e)}

    def delete_shapes(
        self,
        ids: Optional[List[str]] = None,
        layer: Optional[str] = None,
        kind: Optional[str] = None,
        bbox: Optional[Dict[str, float]] = None,
        delete_all: bool = False,
    ) -> Dict[str, Any]:
        """Delete shapes by id(s) or filters.

        Multiple filter matches require ``delete_all=True`` — otherwise the
        call is refused with the candidate list (mirrors delete_copper_pour).
        Explicit ``ids`` are deleted without the flag.
        """
        try:
            board = self._get_board()
            matches = self._match_shapes(board, ids=ids, layer=layer, kind=kind, bbox=bbox)
            if not matches:
                return {
                    "success": False,
                    "message": "No shapes matched — call list_shapes to see what exists",
                }
            if len(matches) > 1 and not ids and not delete_all:
                return {
                    "success": False,
                    "message": (
                        f"{len(matches)} shapes matched — pass all=true to delete "
                        "every match, or select by id (from list_shapes)"
                    ),
                    "shapes": [self._describe_shape(board, s) for s in matches],
                }
            deleted = [self._describe_shape(board, s) for s in matches]
            self._apply_remove(board, matches, f"Deleted {len(matches)} shape(s)")
            self._notify("shape_deleted", {"count": len(matches)})
            return {
                "success": True,
                "message": f"Deleted {len(deleted)} shape(s)",
                "deleted": deleted,
            }
        except Exception as e:
            logger.error(f"Failed to delete shapes: {e}")
            return {"success": False, "message": str(e)}

    def edit_shape(
        self,
        shape_id: str,
        new_layer: Optional[str] = None,
        width: Optional[float] = None,
        filled: Optional[bool] = None,
        move: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """Edit one shape: layer, stroke width, fill, or translate by dx/dy mm."""
        try:
            board = self._get_board()
            matches = self._match_shapes(board, ids=[shape_id])
            if not matches:
                return {
                    "success": False,
                    "message": f"No shape with id {shape_id} — call list_shapes",
                }
            shape = matches[0]
            changed: List[str] = []

            from kipy.geometry import Vector2
            from kipy.util.units import from_mm

            if new_layer is not None:
                shape.layer = self._layer_to_enum(new_layer)
                changed.append("layer")
            if width is not None:
                shape.attributes.stroke.width = from_mm(width)
                changed.append("width")
            if filled is not None:
                shape.attributes.fill.filled = bool(filled)
                changed.append("filled")
            if move is not None:
                dx = from_mm(float(move.get("dx", 0)))
                dy = from_mm(float(move.get("dy", 0)))
                moved = False
                for attr in (
                    "start",
                    "mid",
                    "end",
                    "center",
                    "radius_point",
                    "top_left",
                    "bottom_right",
                ):
                    v = getattr(shape, attr, None)
                    if v is not None and hasattr(v, "x"):
                        setattr(shape, attr, Vector2.from_xy(v.x + dx, v.y + dy))
                        moved = True
                if not moved:
                    # Polygon: shift every outline node through the proto.
                    try:
                        for pwh in shape._proto.shape.polygon.polygons:
                            for node in pwh.outline.nodes:
                                node.point.x_nm += dx
                                node.point.y_nm += dy
                        moved = True
                    except Exception:
                        pass
                if moved:
                    changed.append("move")

            if not changed:
                return {
                    "success": False,
                    "message": (
                        "No editable property given — pass newLayer, width, "
                        "filled, or move {dx, dy}"
                    ),
                    "shape": self._describe_shape(board, shape),
                }

            self._apply_update(board, [shape], f"Edited shape ({', '.join(changed)})")
            self._notify("shape_edited", {"id": shape_id, "changed": changed})
            return {
                "success": True,
                "message": f"Edited shape ({', '.join(changed)})",
                "changed": changed,
                "shape": self._describe_shape(board, shape),
            }
        except Exception as e:
            logger.error(f"Failed to edit shape: {e}")
            return {"success": False, "message": str(e)}
