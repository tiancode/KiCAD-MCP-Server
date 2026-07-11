"""IPCBoardAPI selection / hit-test / interactive-move operations.

Split out of the former monolithic kicad_api/ipc_backend.py.
"""

import logging
from typing import Any, Dict, List, Optional


from ._helpers import (
    INCH_TO_NM,
    MM_TO_NM,
)

logger = logging.getLogger("kicad_interface")


class _SelectionMixin:
    # ------------------------------------------------------------------
    # Selection / interaction
    # ------------------------------------------------------------------
    def get_selection(self) -> List[Dict[str, Any]]:
        """Get currently selected items in the KiCAD UI.

        Returns one dict per item with at least ``id`` and ``type``, plus a
        few common attributes (reference / value for footprints, position /
        layer where available) so a caller can identify what's selected
        without a second round-trip.
        """
        try:
            board = self._get_board()
            selection = board.get_selection()
            return [self._describe_item(item) for item in selection]
        except Exception as e:
            logger.error(f"Failed to get selection: {e}")
            return []

    def clear_selection(self) -> bool:
        """Clear the current selection in KiCAD UI."""
        try:
            board = self._get_board()
            board.clear_selection()
            self._notify("selection_cleared", {})
            return True
        except Exception as e:
            logger.error(f"Failed to clear selection: {e}")
            return False

    def add_to_selection(self, ids: List[str]) -> Dict[str, Any]:
        """Add board items (by KIID) to the current selection."""
        return self._mutate_selection(ids, add=True)

    def remove_from_selection(self, ids: List[str]) -> Dict[str, Any]:
        """Remove board items (by KIID) from the current selection."""
        return self._mutate_selection(ids, add=False)

    def hit_test(
        self,
        x: float,
        y: float,
        item_id: Optional[str] = None,
        tolerance: float = 0,
        unit: str = "mm",
    ) -> Dict[str, Any]:
        """Hit-test a board item at ``(x, y)``.

        If ``item_id`` is given, test only that item. Otherwise, sweep all
        footprints, tracks, vias, zones, and graphic shapes and return every
        item whose ``hit_test`` returns True — useful for "what's at this
        coordinate?" queries.

        ``tolerance`` is in the same unit as the coordinates.
        """
        try:
            from kipy.geometry import Vector2
            from kipy.util.units import from_mm

            board = self._get_board()
            scale = MM_TO_NM if unit == "mm" else INCH_TO_NM
            position = Vector2.from_xy(int(x * scale), int(y * scale))
            tol_nm = int(tolerance * scale)

            if item_id:
                items = self._resolve_items_by_ids(board, [item_id])
                if not items:
                    return {"success": False, "message": f"Item {item_id} not found"}
                hit = bool(board.hit_test(items[0], position, tol_nm))
                return {
                    "success": True,
                    "hit": hit,
                    "items": [self._describe_item(items[0])] if hit else [],
                }

            # Sweep — collect anything underneath the cursor.
            from_mm  # keep import for type-checkers; not used here directly
            candidates: List[Any] = []
            for getter in (
                "get_footprints",
                "get_tracks",
                "get_vias",
                "get_zones",
                "get_shapes",
            ):
                try:
                    candidates.extend(list(getattr(board, getter)()))
                except Exception as e:
                    logger.debug(f"hit_test sweep: {getter} failed: {e}")

            hits = []
            for item in candidates:
                try:
                    if board.hit_test(item, position, tol_nm):
                        hits.append(self._describe_item(item))
                except Exception as e:
                    logger.debug(f"hit_test on item failed: {e}")
                    continue

            return {"success": True, "hit": bool(hits), "items": hits, "count": len(hits)}
        except Exception as e:
            logger.error(f"Failed to hit-test: {e}")
            return {"success": False, "message": str(e)}

    def interactive_move(self, ids: List[str]) -> Dict[str, Any]:
        """Initiate KiCad's interactive move tool on the given items.

        This is a blocking-style operation in KiCad — future API calls return
        AS_BUSY until the user finishes the drag.  We return immediately;
        callers should not chain further mutations until the user releases.
        """
        try:
            board = self._get_board()
            items = self._resolve_items_by_ids(board, ids)
            if not items:
                return {
                    "success": False,
                    "message": "No items resolved from supplied IDs",
                    "requested": list(ids),
                }
            # kipy's interactive_move accepts a single KIID or an iterable.
            # Pass the proto KIIDs (item.id), not the wrappers.
            board.interactive_move([item.id for item in items])
            self._notify("interactive_move", {"ids": list(ids), "count": len(items)})
            return {
                "success": True,
                "requested": list(ids),
                "resolved": len(items),
                "message": (
                    "Interactive move started — KiCAD UI is now in drag mode. "
                    "Further API calls will return AS_BUSY until the user releases."
                ),
            }
        except Exception as e:
            logger.error(f"Failed to start interactive move: {e}")
            return {"success": False, "message": str(e)}
