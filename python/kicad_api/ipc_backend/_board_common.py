"""Shared IPCBoardAPI infrastructure: board access, apply primitives, enum/unit conversions.

Split out of the former monolithic kicad_api/ipc_backend.py.
"""

import logging
import os
from typing import Any, Dict, List

from kicad_api.base import ConnectionError

from ._helpers import (
    MM_TO_NM,
    _document_type_enum,
    get_open_documents_compat,
)

logger = logging.getLogger("kicad_interface")


class _CommonMixin:
    # Instance attributes set in IPCBoardAPI.__init__ (kicad_api.ipc_backend.
    # _board_core); declared here so mypy can resolve them in this mixin after
    # the package split. Annotation-only — no runtime effect.
    _kicad: Any
    _board: Any
    _notify: Any

    def _get_board(self) -> Any:
        """Get board instance, connecting if needed."""
        if self._board is None:
            try:
                self._board = self._kicad.get_board()
            except Exception as e:
                logger.error(f"Failed to get board: {e}")
                raise ConnectionError(f"No board open in KiCAD: {e}")
        return self._board

    def invalidate_board(self) -> None:
        """Drop the cached kipy Board wrapper so the next :meth:`_get_board`
        re-fetches the CURRENT document from the client.

        The wrapper is otherwise cached for the connection's lifetime, so after
        a same-instance board switch (or a reselect heal) it keeps serving the
        OLD board's document — which made the board-identity gate refuse forever
        and post-heal reads return stale data (finding 2)."""
        self._board = None

    #: Default label shown in KiCad's undo history when the caller didn't
    #: supply one.  Single source of truth — handlers pass through
    #: ``None`` rather than copy-substituting their own default.
    _DEFAULT_COMMIT_LABEL = "MCP Operation"

    # ------------------------------------------------------------------
    # Mutation helpers — every mutator funnels through these so that an
    # open transaction (via begin_transaction) catches the change instead
    # of opening its own commit.
    # ------------------------------------------------------------------
    def _apply_create(self, board: Any, item: Any, description: str) -> str:
        """Create one item, respecting any open transaction.

        Returns the new item's KIID string. kipy's ``create_items``
        returns fresh wrappers with the server-assigned IDs; the input
        wrapper is *not* mutated, so we must read the id from the
        return value (not from the local ``item``).
        """
        if self._current_commit is not None:
            created = board.create_items(item)
        else:
            commit = board.begin_commit()
            created = board.create_items(item)
            board.push_commit(commit, description)
        # create_items returns a list (or None from older stubs). Take
        # the first entry's id; fall back to the input item if the
        # backend gave us nothing useful (defensive — real kipy always
        # returns the created wrapper list, but tests / stubs vary).
        if created:
            first = created[0]
            if hasattr(first, "id"):
                return str(first.id)
        return str(item.id) if hasattr(item, "id") else ""

    def _apply_update(self, board: Any, items: List[Any], description: str) -> None:
        """Update items, respecting any open transaction."""
        if self._current_commit is not None:
            board.update_items(items)
        else:
            commit = board.begin_commit()
            board.update_items(items)
            board.push_commit(commit, description)

    def _apply_remove(self, board: Any, items: List[Any], description: str) -> None:
        """Remove items, respecting any open transaction."""
        if self._current_commit is not None:
            board.remove_items(items)
        else:
            commit = board.begin_commit()
            board.remove_items(items)
            board.push_commit(commit, description)

    @staticmethod
    def _get_box2_extents(bbox: Any) -> tuple[float, float, float, float]:
        """Return left/top/right/bottom for kipy Box2 wrappers across versions."""
        if hasattr(bbox, "min") and hasattr(bbox, "max"):
            return bbox.min.x, bbox.min.y, bbox.max.x, bbox.max.y

        if hasattr(bbox, "pos") and hasattr(bbox, "size"):
            x1 = bbox.pos.x
            y1 = bbox.pos.y
            x2 = x1 + bbox.size.x
            y2 = y1 + bbox.size.y
            return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)

        raise AttributeError("Unsupported Box2 shape: expected min/max or pos/size")

    def _load_footprint_from_library(self, footprint_path: str) -> Any:
        """
        Load a footprint from the library using pcbnew SWIG API.

        Args:
            footprint_path: Either "Library:FootprintName" or just "FootprintName"

        Returns:
            pcbnew.FOOTPRINT object or None if not found
        """
        try:
            import pcbnew
            from commands.library import get_library_manager

            # ``pcbnew.GetGlobalFootprintLib()`` does NOT exist in KiCad 9/10
            # — the old code AttributeError'd here, so every IPC placement
            # silently failed.  Resolve the nickname to its ``.pretty``
            # directory via the library table (same path the working SWIG
            # place_component uses) and load by path. Cached manager so a
            # multi-component placement doesn't re-parse the lib-table per part.
            resolved = get_library_manager().find_footprint(footprint_path)
            if not resolved:
                logger.warning(f"Footprint '{footprint_path}' not found in any library")
                return None

            library_path, fp_name = resolved
            loaded_fp = pcbnew.FootprintLoad(library_path, fp_name)
            if loaded_fp:
                logger.info(f"Loaded footprint '{fp_name}' from '{library_path}'")
                return loaded_fp

            logger.warning(f"FootprintLoad returned None for {library_path}/{fp_name}")
            return None

        except ImportError:
            logger.warning("pcbnew not available - cannot load footprints from library")
            return None
        except Exception as e:
            logger.error(f"Error loading footprint from library: {e}")
            return None

    def _place_loaded_footprint(
        self,
        loaded_fp: Any,
        reference: str,
        x: float,
        y: float,
        rotation: float,
        layer: str,
        value: str,
    ) -> bool:
        """
        Place a loaded pcbnew footprint onto the board.

        Uses SWIG to add the footprint, then notifies for IPC sync.
        """
        try:
            import pcbnew

            # Get the board file path from IPC to load via pcbnew
            board = self._get_board()
            board_path = None

            # Try to get the board path from kipy.  Docs expose
            # ``board_filename`` (relative) + ``project.path`` (dir), not a
            # single ``path`` attribute; stitch them.
            try:
                DocumentType = _document_type_enum()
                dt = DocumentType.DOCTYPE_PCB if DocumentType is not None else None
                for doc in get_open_documents_compat(self._kicad, dt):
                    fname = getattr(doc, "board_filename", "") or ""
                    if not str(fname).endswith(".kicad_pcb"):
                        continue
                    proj = getattr(doc, "project", None)
                    proj_dir = getattr(proj, "path", "") if proj is not None else ""
                    candidate = os.path.join(proj_dir, fname) if proj_dir else fname
                    board_path = candidate
                    break
            except Exception as e:
                logger.debug(f"Could not get board path from IPC: {e}")

            if board_path and os.path.exists(board_path):
                pcb_board = pcbnew.LoadBoard(board_path)
            else:
                pcb_board = pcbnew.GetBoard()

            if not pcb_board:
                logger.error("Could not get pcbnew board instance")
                return self._place_placeholder_footprint(
                    reference, "", x, y, rotation, layer, value
                )

            scale = MM_TO_NM
            loaded_fp.SetPosition(pcbnew.VECTOR2I(int(x * scale), int(y * scale)))
            loaded_fp.SetOrientationDegrees(rotation)

            loaded_fp.SetReference(reference)

            if value:
                loaded_fp.SetValue(value)

            # Set layer (flip if bottom)
            if layer == "B.Cu":
                if not loaded_fp.IsFlipped():
                    loaded_fp.Flip(loaded_fp.GetPosition(), False)

            pcb_board.Add(loaded_fp)

            # Save the board so IPC can see the changes
            pcbnew.SaveBoard(board_path, pcb_board)

            try:
                board.revert()  # Reload from disk to sync IPC
            except Exception as e:
                logger.debug(f"Could not refresh IPC board: {e}")

            self._notify(
                "component_placed",
                {
                    "reference": reference,
                    "footprint": loaded_fp.GetFPIDAsString(),
                    "position": {"x": x, "y": y},
                    "rotation": rotation,
                    "layer": layer,
                    "loaded_from_library": True,
                },
            )

            logger.info(
                f"Placed component {reference} ({loaded_fp.GetFPIDAsString()}) at ({x}, {y}) mm"
            )
            return True

        except Exception as e:
            logger.error(f"Error placing loaded footprint: {e}")
            return self._place_placeholder_footprint(reference, "", x, y, rotation, layer, value)

    def _place_placeholder_footprint(
        self,
        reference: str,
        footprint: str,
        x: float,
        y: float,
        rotation: float,
        layer: str,
        value: str,
    ) -> bool:
        """
        Place a placeholder footprint when library loading fails.

        Creates a basic footprint via IPC with just reference/value fields.
        """
        try:
            from kipy.board_types import Footprint
            from kipy.geometry import Angle, Vector2
            from kipy.proto.board.board_types_pb2 import BoardLayer
            from kipy.util.units import from_mm

            board = self._get_board()

            fp = Footprint()
            fp.position = Vector2.from_xy(from_mm(x), from_mm(y))
            fp.orientation = Angle.from_degrees(rotation)

            if layer == "B.Cu":
                fp.layer = BoardLayer.BL_B_Cu
            else:
                fp.layer = BoardLayer.BL_F_Cu

            if fp.reference_field:
                fp.reference_field.text.value = reference
            if fp.value_field:
                fp.value_field.text.value = value if value else footprint

            self._apply_create(board, fp, f"Placed component {reference}")

            self._notify(
                "component_placed",
                {
                    "reference": reference,
                    "footprint": footprint,
                    "position": {"x": x, "y": y},
                    "rotation": rotation,
                    "layer": layer,
                    "loaded_from_library": False,
                    "is_placeholder": True,
                },
            )

            logger.info(f"Placed placeholder component {reference} at ({x}, {y}) mm")
            return True

        except Exception as e:
            logger.error(f"Failed to place placeholder component: {e}")
            return False

    def _mutate_selection(self, ids: List[str], *, add: bool) -> Dict[str, Any]:
        try:
            board = self._get_board()
            items = self._resolve_items_by_ids(board, ids)
            if not items:
                return {
                    "success": False,
                    "message": "No items resolved from supplied IDs",
                    "requested": list(ids),
                    "resolved": 0,
                }
            updated = board.add_to_selection(items) if add else board.remove_from_selection(items)
            event = "selection_added" if add else "selection_removed"
            self._notify(event, {"ids": list(ids), "count": len(items)})
            return {
                "success": True,
                "requested": list(ids),
                "resolved": len(items),
                "selection": [self._describe_item(i) for i in updated],
            }
        except Exception as e:
            logger.error(f"Failed to {'add to' if add else 'remove from'} selection: {e}")
            return {"success": False, "message": str(e)}

    @staticmethod
    def _require_unit(unit: str) -> None:
        """Reject any unit other than ``mm``/``inch``. Silent fallback would
        let a ``unit="mil"`` request walk through the mm code path and label
        the result as ``mil`` while the math used mm — wrong by 25.4×."""
        if unit not in ("mm", "inch"):
            raise ValueError(f"Unknown unit {unit!r}; expected 'mm' or 'inch'")

    @staticmethod
    def _origin_name_to_enum(name: str) -> int:
        """Resolve ``"grid"`` / ``"drill"`` / ``"aux"`` (alias for drill) to
        the ``BoardOriginType`` enum value.  Raises ``ValueError`` for
        anything else so callers see a clean error rather than silently
        falling back to ``BOT_UNKNOWN`` which kipy rejects."""
        from kipy.proto.board.board_commands_pb2 import BoardOriginType

        canonical = name.strip().lower()
        # "aux" is what the KiCad UI labels the drill/place origin as in
        # plot/export dialogs — accept it as a synonym.
        if canonical in ("drill", "aux", "drill/place"):
            return BoardOriginType.BOT_DRILL
        if canonical == "grid":
            return BoardOriginType.BOT_GRID
        raise ValueError(f"Unknown origin type {name!r}; expected 'grid', 'drill', or 'aux'")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _layer_to_enum(name: str) -> Any:
        """Map a dotted layer name (e.g. ``F.Cu``, ``Edge.Cuts``) to a
        ``BoardLayer`` enum value.

        The enum names follow ``BL_<dotted-name with '.' replaced by '_'>``,
        e.g. ``BL_F_Cu``, ``BL_Edge_Cuts``, ``BL_User_1``.  Unknown layers
        fall back to ``BL_F_SilkS`` rather than raising — graphic shapes
        with no layer set draw on nothing useful, so a visible default beats
        a hard failure.
        """
        from kipy.proto.board.board_types_pb2 import BoardLayer

        sanitized = "BL_" + name.replace(".", "_")
        value = BoardLayer.Value(sanitized) if sanitized in BoardLayer.keys() else None
        if value is None:
            logger.warning(f"Unknown layer {name!r}; defaulting to F.SilkS")
            return BoardLayer.BL_F_SilkS
        return value

    @staticmethod
    def _resolve_items_by_ids(board: Any, ids: List[str]) -> List[Any]:
        """Resolve KIID strings to BoardItem wrappers via the live board.

        Tries ``board.get_items_by_id`` first (newer kipy); falls back to a
        full scan if that's unavailable.  Unknown IDs are silently skipped —
        callers see the gap in the returned ``resolved`` count.
        """
        if not ids:
            return []
        # Preferred: bulk lookup by ID (kipy ≥ 9.x).
        try:
            return list(board.get_items_by_id(list(ids)))
        except Exception as e:
            logger.debug(f"get_items_by_id failed; falling back to scan: {e}")

        # Fallback: scan all known item collections.  Match on the clean
        # KIID string (kiid_str) — ``str()`` on a kipy KIID proto prints the
        # field repr (``value: "<uuid>"``), which never equals the uuid
        # callers pass in, so the old comparison silently matched nothing.
        from ._helpers import kiid_str

        wanted = set(str(i) for i in ids)
        out: List[Any] = []
        for getter in (
            "get_footprints",
            "get_tracks",
            "get_vias",
            "get_zones",
            "get_shapes",
            "get_text",
            "get_pads",
        ):
            try:
                for item in getattr(board, getter)():
                    if kiid_str(getattr(item, "id", None)) in wanted:
                        out.append(item)
            except Exception:
                continue
        return out

    @staticmethod
    def _describe_item(item: Any) -> Dict[str, Any]:
        """Build a JSON-safe summary of a BoardItem for selection / hit-test
        responses.  Tolerates missing attributes — the kipy wrapper shape
        varies by item type."""
        from ._helpers import kiid_str, normalize_board_layer

        info: Dict[str, Any] = {
            "type": type(item).__name__,
            "id": kiid_str(getattr(item, "id", None)),
        }
        # Footprint-ish: surface reference + value when present.
        ref_field = getattr(item, "reference_field", None)
        if ref_field is not None:
            try:
                info["reference"] = ref_field.text.value
            except Exception:
                pass
        val_field = getattr(item, "value_field", None)
        if val_field is not None:
            try:
                info["value"] = val_field.text.value
            except Exception:
                pass
        # Position-ish: footprints / vias / pads / text.
        try:
            from kipy.util.units import to_mm

            pos = getattr(item, "position", None)
            if pos is not None and hasattr(pos, "x"):
                info["position"] = {"x": to_mm(pos.x), "y": to_mm(pos.y), "unit": "mm"}
        except Exception:
            pass
        layer = getattr(item, "layer", None)
        if layer is not None:
            info["layer"] = normalize_board_layer(layer)
        return info
