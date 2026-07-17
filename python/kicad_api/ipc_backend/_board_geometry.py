"""IPCBoardAPI board size / layers / origin / title-block operations.

Split out of the former monolithic kicad_api/ipc_backend.py.
"""

import logging
from typing import Any, Dict, List, Optional

from ._helpers import (
    INCH_TO_NM,
)

logger = logging.getLogger("kicad_interface")


class _GeometryMixin:
    def set_size(self, width: float, height: float, unit: str = "mm") -> bool:
        """
        Set board size.

        Note: Board size in KiCAD is typically defined by the board outline,
        not a direct size property. This method may need to create/modify
        the board outline.
        """
        try:
            from kipy.board_types import BoardRectangle
            from kipy.geometry import Vector2
            from kipy.proto.board.board_types_pb2 import BoardLayer
            from kipy.util.units import from_mm

            board = self._get_board()

            # Convert to nm
            if unit == "mm":
                w = from_mm(width)
                h = from_mm(height)
            else:
                w = int(width * INCH_TO_NM)
                h = int(height * INCH_TO_NM)

            # Create board outline rectangle on Edge.Cuts layer
            rect = BoardRectangle()
            rect.start = Vector2.from_xy(0, 0)
            rect.end = Vector2.from_xy(w, h)
            rect.layer = BoardLayer.BL_Edge_Cuts
            rect.width = from_mm(0.1)  # Standard edge cut width

            self._apply_create(board, rect, f"Set board size to {width}x{height} {unit}")

            self._notify("board_size", {"width": width, "height": height, "unit": unit})

            return True

        except Exception as e:
            logger.error(f"Failed to set board size: {e}")
            return False

    def get_size(self) -> Dict[str, Any]:
        """Get current board size from bounding box."""
        try:
            board = self._get_board()

            # Get shapes on Edge.Cuts layer to determine board size
            shapes = board.get_shapes()

            if not shapes:
                return {"width": 0, "height": 0, "unit": "mm"}

            # Find bounding box of edge cuts
            from kipy.util.units import to_mm

            min_x = min_y = float("inf")
            max_x = max_y = float("-inf")

            for shape in shapes:
                # Check if on Edge.Cuts layer
                bbox = board.get_item_bounding_box(shape)
                if bbox:
                    left, top, right, bottom = self._get_box2_extents(bbox)
                    min_x = min(min_x, left)
                    min_y = min(min_y, top)
                    max_x = max(max_x, right)
                    max_y = max(max_y, bottom)

            if min_x == float("inf"):
                return {"width": 0, "height": 0, "unit": "mm"}

            return {"width": to_mm(max_x - min_x), "height": to_mm(max_y - min_y), "unit": "mm"}

        except Exception as e:
            logger.error(f"Failed to get board size: {e}")
            return {"width": 0, "height": 0, "unit": "mm", "error": str(e)}

    def get_outline_bbox(self, unit: str = "mm") -> Optional[Dict[str, Any]]:
        """Return the board's Edge.Cuts bounding box, or ``None`` if no outline.

        Unlike :meth:`get_size` (which returns width/height only), this returns
        the absolute ``{x1, y1, x2, y2}`` extents so a caller can tell whether a
        point lies inside the board.  Only Edge.Cuts shapes are considered — a
        silk logo or courtyard must not be mistaken for the board edge.
        """
        try:
            from kipy.proto.board.board_types_pb2 import BoardLayer
            from kipy.util.units import to_mm

            board = self._get_board()
            shapes = board.get_shapes()
            if not shapes:
                return None

            edge_enum = BoardLayer.BL_Edge_Cuts
            min_x = min_y = float("inf")
            max_x = max_y = float("-inf")
            found = False
            for shape in shapes:
                if getattr(shape, "layer", None) != edge_enum:
                    continue
                bbox = board.get_item_bounding_box(shape)
                if not bbox:
                    continue
                left, top, right, bottom = self._get_box2_extents(bbox)
                min_x = min(min_x, left)
                min_y = min(min_y, top)
                max_x = max(max_x, right)
                max_y = max(max_y, bottom)
                found = True

            if not found or max_x - min_x <= 0 or max_y - min_y <= 0:
                return None

            if unit == "inch":
                scale = 1.0 / INCH_TO_NM
                conv = lambda v: v * scale  # noqa: E731
            else:
                conv = to_mm
            return {
                "x1": conv(min_x),
                "y1": conv(min_y),
                "x2": conv(max_x),
                "y2": conv(max_y),
                "unit": unit,
            }
        except Exception as e:
            logger.error(f"Failed to get board outline bbox: {e}")
            return None

    def add_layer(self, layer_name: str, layer_type: str) -> bool:
        """Add layer to the board (layers are typically predefined in KiCAD)."""
        logger.warning("Layer management via IPC is limited - layers are predefined")
        return False

    def get_enabled_layers(self) -> List[str]:
        """Get list of enabled layers."""
        try:
            board = self._get_board()
            layers = board.get_enabled_layers()
            return [str(layer) for layer in layers]
        except Exception as e:
            logger.error(f"Failed to get enabled layers: {e}")
            return []

    # ------------------------------------------------------------------
    # Board metadata: origins + title block
    # ------------------------------------------------------------------
    def get_origin(self, origin_type: str = "drill", unit: str = "mm") -> Dict[str, Any]:
        """Return the requested board origin in user units.

        ``origin_type`` is ``"grid"`` (the user grid origin) or
        ``"drill"`` (the drill/place a.k.a. aux origin — what Gerber and
        pick-and-place files use as their coordinate zero).
        ``unit`` is ``"mm"`` or ``"inch"``; anything else is rejected
        (silent fallback would mis-label inch values as mm or vice versa).
        """
        try:
            from kipy.util.units import to_mm

            self._require_unit(unit)
            board = self._get_board()
            type_int = self._origin_name_to_enum(origin_type)
            origin = board.get_origin(type_int)
            x_nm = int(origin.x)
            y_nm = int(origin.y)
            if unit == "inch":
                x = x_nm / INCH_TO_NM
                y = y_nm / INCH_TO_NM
            else:
                x = to_mm(x_nm)
                y = to_mm(y_nm)
            return {
                "success": True,
                "type": origin_type,
                "x": x,
                "y": y,
                "unit": unit,
            }
        except Exception as e:
            logger.error(f"Failed to get origin: {e}")
            return {"success": False, "message": str(e)}

    def set_origin(
        self,
        origin_type: str,
        x: float,
        y: float,
        unit: str = "mm",
    ) -> Dict[str, Any]:
        """Set the grid or drill/place origin to ``(x, y)`` in user units.

        ``unit`` must be ``"mm"`` or ``"inch"`` — unknown units are
        rejected rather than silently treated as mm.
        """
        try:
            from kipy.geometry import Vector2
            from kipy.util.units import from_mm

            self._require_unit(unit)
            board = self._get_board()
            type_int = self._origin_name_to_enum(origin_type)
            if unit == "inch":
                x_nm = int(x * INCH_TO_NM)
                y_nm = int(y * INCH_TO_NM)
            else:
                x_nm = from_mm(x)
                y_nm = from_mm(y)
            board.set_origin(type_int, Vector2.from_xy(x_nm, y_nm))
            self._notify(
                "origin_set",
                {"type": origin_type, "x": x, "y": y, "unit": unit},
            )
            return {
                "success": True,
                "type": origin_type,
                "x": x,
                "y": y,
                "unit": unit,
            }
        except Exception as e:
            logger.error(f"Failed to set origin: {e}")
            return {"success": False, "message": str(e)}

    def get_title_block_info(self) -> Dict[str, Any]:
        """Return the board title block — title / date / revision / company /
        comments (a dict keyed 1..9, KiCad's fixed nine comment slots)."""
        try:
            board = self._get_board()
            tb = board.get_title_block_info()
            return {
                "success": True,
                "title": tb.title,
                "date": tb.date,
                "revision": tb.revision,
                "company": tb.company,
                # Materialise as a string-keyed dict so it survives JSON
                # round-trips without integer-key coercion surprises.
                "comments": {str(k): v for k, v in tb.comments.items()},
            }
        except Exception as e:
            logger.error(f"Failed to get title block: {e}")
            return {"success": False, "message": str(e)}

    def set_title_block_info(
        self,
        title: Optional[str] = None,
        date: Optional[str] = None,
        revision: Optional[str] = None,
        company: Optional[str] = None,
        comments: Optional[Dict[int, str]] = None,
    ) -> Dict[str, Any]:
        """Update title block — any field left ``None`` is preserved.

        ``comments`` is a partial dict ``{slot: text}`` where ``slot`` is
        1..9.  Only listed slots are overwritten; the rest stay put.  Pass
        an explicit empty string to clear a slot.

        kipy's ``set_title_block_info`` replaces the whole block, so we
        fetch the current one, merge the incoming partial update, and send
        the result back.  This makes partial updates safe — without the
        get-merge-set dance a single missing field would erase the rest.
        """
        try:
            from kipy.common_types import TitleBlockInfo

            board = self._get_board()
            current = board.get_title_block_info()
            merged = TitleBlockInfo()
            merged.title = title if title is not None else current.title
            merged.date = date if date is not None else current.date
            merged.revision = revision if revision is not None else current.revision
            merged.company = company if company is not None else current.company
            # Comments are read-only via the wrapper's .comments property
            # (it constructs a fresh dict each call), so write through the
            # proto fields comment1..comment9 directly.  Source-of-truth
            # for unchanged slots is the *current* board state, not the
            # default-zero proto.
            for idx in range(1, 10):
                field = f"comment{idx}"
                setattr(merged._proto, field, getattr(current._proto, field))
            if comments:
                for k, v in comments.items():
                    try:
                        slot = int(k)
                    except (TypeError, ValueError):
                        logger.warning(f"Ignoring non-integer comment slot {k!r}")
                        continue
                    if 1 <= slot <= 9:
                        setattr(merged._proto, f"comment{slot}", str(v))
                    else:
                        logger.warning(f"Comment slot {slot} out of range 1..9; ignored")
            board.set_title_block_info(merged)
            self._notify(
                "title_block_set",
                {
                    "title": merged.title,
                    "date": merged.date,
                    "revision": merged.revision,
                    "company": merged.company,
                },
            )
            return {
                "success": True,
                "title": merged.title,
                "date": merged.date,
                "revision": merged.revision,
                "company": merged.company,
                "comments": {str(k): v for k, v in merged.comments.items()},
            }
        except Exception as e:
            logger.error(f"Failed to set title block: {e}")
            return {"success": False, "message": str(e)}
