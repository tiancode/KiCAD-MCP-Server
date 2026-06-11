"""IPCBoardAPI component listing / placement operations.

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
    kiid_str,
    normalize_board_layer,
)

logger = logging.getLogger("kicad_interface")


class _ComponentMixin:
    def list_components(self) -> List[Dict[str, Any]]:
        """List all components (footprints) on the board."""
        try:
            from kipy.util.units import to_mm

            board = self._get_board()
            footprints = board.get_footprints()

            components = []
            for fp in footprints:
                try:
                    pos = fp.position

                    # Try to get bounding box
                    bbox_data = None
                    try:
                        bbox = board.get_item_bounding_box(fp)
                        if bbox:
                            bbox_data = {
                                "min_x": to_mm(bbox.min.x),
                                "min_y": to_mm(bbox.min.y),
                                "max_x": to_mm(bbox.max.x),
                                "max_y": to_mm(bbox.max.y),
                                "width": to_mm(bbox.max.x - bbox.min.x),
                                "height": to_mm(bbox.max.y - bbox.min.y),
                                "unit": "mm",
                            }
                    except Exception:
                        pass  # Bounding box may not be available via IPC

                    # Fallback: compute bounding box from pad positions + sizes
                    if not bbox_data:
                        try:
                            pads = fp.pads if hasattr(fp, "pads") else []
                            pad_list = list(pads)
                            if pad_list:
                                min_x = float("inf")
                                min_y = float("inf")
                                max_x = float("-inf")
                                max_y = float("-inf")
                                for pad in pad_list:
                                    px = to_mm(pad.position.x) if pad.position else 0
                                    py = to_mm(pad.position.y) if pad.position else 0
                                    pw = (
                                        to_mm(pad.size.x) / 2
                                        if hasattr(pad, "size") and pad.size
                                        else 0.5
                                    )
                                    ph = (
                                        to_mm(pad.size.y) / 2
                                        if hasattr(pad, "size") and pad.size
                                        else 0.5
                                    )
                                    min_x = min(min_x, px - pw)
                                    min_y = min(min_y, py - ph)
                                    max_x = max(max_x, px + pw)
                                    max_y = max(max_y, py + ph)
                                margin = 0.25  # mm — small margin for component body beyond pads
                                bbox_data = {
                                    "min_x": min_x - margin,
                                    "min_y": min_y - margin,
                                    "max_x": max_x + margin,
                                    "max_y": max_y + margin,
                                    "width": (max_x - min_x) + 2 * margin,
                                    "height": (max_y - min_y) + 2 * margin,
                                    "unit": "mm",
                                }
                        except Exception as e:
                            logger.debug(f"Could not compute bbox from pads: {e}")

                    # Normalize via the shared helper: kipy may return an
                    # enum object, a name string, or a bare protobuf int.
                    layer_str = normalize_board_layer(getattr(fp, "layer", None)) or "F.Cu"

                    components.append(
                        {
                            "reference": (
                                fp.reference_field.text.value if fp.reference_field else ""
                            ),
                            "value": fp.value_field.text.value if fp.value_field else "",
                            "footprint": (
                                str(fp.definition.library_link)
                                if fp.definition and hasattr(fp.definition, "library_link")
                                else (
                                    str(fp.definition.id)
                                    if fp.definition and hasattr(fp.definition, "id")
                                    else ""
                                )
                            ),
                            "position": {
                                "x": to_mm(pos.x) if pos else 0,
                                "y": to_mm(pos.y) if pos else 0,
                                "unit": "mm",
                            },
                            "rotation": fp.orientation.degrees if fp.orientation else 0,
                            "layer": layer_str,
                            "id": kiid_str(getattr(fp, "id", None)),
                            "boundingBox": bbox_data,
                        }
                    )
                except Exception as e:
                    logger.warning(f"Error processing footprint: {e}")
                    continue

            return components

        except Exception as e:
            logger.error(f"Failed to list components: {e}")
            return []

    def get_component_pads(self, reference: str, unit: str = "mm") -> Optional[Dict[str, Any]]:
        """Pad geometry + net for one footprint, read live from KiCad over IPC.

        Returns the same shape as the SWIG ``get_component_pads`` (absolute
        positions in ``unit`` — mm/mil/inch) so callers don't have to branch on
        backend, or ``None`` if no footprint matches ``reference``.  ``netCode``
        is reported as ``None`` — kipy deprecates net codes (removed in KiCad
        10) — but kept for output-shape parity with the SWIG handler.
        """
        from kipy.proto.board.board_types_pb2 import PadStackShape, PadType
        from utils.units import nm_to_unit, normalize_unit

        unit = normalize_unit(unit)
        board = self._get_board()
        target = None
        for fp in board.get_footprints():
            ref = fp.reference_field.text.value if fp.reference_field else ""
            if ref == reference:
                target = fp
                break
        if target is None:
            return None

        shape_map = {
            "PSS_CIRCLE": "circle",
            "PSS_RECTANGLE": "rect",
            "PSS_OVAL": "oval",
            "PSS_TRAPEZOID": "trapezoid",
            "PSS_ROUNDRECT": "roundrect",
            "PSS_CHAMFEREDRECT": "chamfered_rect",
            "PSS_CUSTOM": "custom",
        }
        type_map = {
            "PT_PTH": "through_hole",
            "PT_SMD": "smd",
            "PT_EDGE_CONNECTOR": "connector",
            "PT_NPTH": "npth",
        }

        # FootprintInstance has no .pads; pads live on .definition and (per
        # kipy) carry absolute board coordinates because the instance's
        # position setter translates them.
        definition = getattr(target, "definition", None)
        pad_items = list(definition.pads) if definition is not None else []

        pads_out: List[Dict[str, Any]] = []
        for pad in pad_items:
            pos = pad.position
            size = None
            shape = "unknown"
            try:
                copper = pad.padstack.copper_layers
                if copper:
                    size = {
                        "x": nm_to_unit(copper[0].size.x, unit),
                        "y": nm_to_unit(copper[0].size.y, unit),
                        "unit": unit,
                    }
                    shape = shape_map.get(PadStackShape.Name(copper[0].shape), "unknown")
            except Exception:
                pass  # padstack geometry not always available via IPC
            drill = None
            try:
                d = pad.padstack.drill.diameter.x
                if d and d > 0:
                    drill = nm_to_unit(d, unit)
            except Exception:
                pass
            try:
                pad_type = type_map.get(PadType.Name(pad.pad_type), "unknown")
            except Exception:
                pad_type = "unknown"
            pads_out.append(
                {
                    "name": pad.number,
                    "number": pad.number,
                    "position": {
                        "x": nm_to_unit(pos.x, unit),
                        "y": nm_to_unit(pos.y, unit),
                        "unit": unit,
                    },
                    "net": pad.net.name if pad.net else "",
                    # Shape parity with the SWIG handler, which emits netCode.
                    # kipy deprecates net codes (removed in KiCad 10), so the
                    # honest value over IPC is None rather than a stale int.
                    "netCode": None,
                    "shape": shape,
                    "type": pad_type,
                    "size": size,
                    "drillSize": drill,
                }
            )

        cpos = target.position
        return {
            "reference": reference,
            "componentPosition": {
                "x": nm_to_unit(cpos.x, unit),
                "y": nm_to_unit(cpos.y, unit),
                "unit": unit,
            },
            "padCount": len(pads_out),
            "pads": pads_out,
        }

    def place_component(
        self,
        reference: str,
        footprint: str,
        x: float,
        y: float,
        rotation: float = 0,
        layer: str = "F.Cu",
        value: str = "",
    ) -> bool:
        """
        Place a component on the board.

        The component appears immediately in the KiCAD UI.

        This method uses a hybrid approach:
        1. Load the footprint definition from the library using pcbnew (SWIG)
        2. Place it on the board via IPC for real-time UI updates

        Args:
            reference: Component reference designator (e.g., "R1", "U1")
            footprint: Footprint path in format "Library:FootprintName" or just "FootprintName"
            x: X position in mm
            y: Y position in mm
            rotation: Rotation angle in degrees
            layer: Layer name ("F.Cu" for top, "B.Cu" for bottom)
            value: Component value (optional)
        """
        try:
            # First, try to load the footprint from library using pcbnew SWIG
            loaded_fp = self._load_footprint_from_library(footprint)

            if loaded_fp:
                # We have the footprint from the library - place it via SWIG
                # then sync to IPC for UI update
                return self._place_loaded_footprint(
                    loaded_fp, reference, x, y, rotation, layer, value
                )
            else:
                # Fallback: Create a basic placeholder footprint via IPC
                logger.warning(
                    f"Could not load footprint '{footprint}' from library, creating placeholder"
                )
                return self._place_placeholder_footprint(
                    reference, footprint, x, y, rotation, layer, value
                )

        except Exception as e:
            logger.error(f"Failed to place component: {e}")
            return False

    def move_component(
        self, reference: str, x: float, y: float, rotation: Optional[float] = None
    ) -> bool:
        """Move a component to a new position (updates UI immediately)."""
        try:
            from kipy.geometry import Angle, Vector2
            from kipy.util.units import from_mm

            board = self._get_board()
            footprints = board.get_footprints()

            # Find the footprint by reference
            target_fp = None
            for fp in footprints:
                if fp.reference_field and fp.reference_field.text.value == reference:
                    target_fp = fp
                    break

            if not target_fp:
                logger.error(f"Component not found: {reference}")
                return False

            # Update position
            target_fp.position = Vector2.from_xy(from_mm(x), from_mm(y))

            if rotation is not None:
                target_fp.orientation = Angle.from_degrees(rotation)

            self._apply_update(board, [target_fp], f"Moved component {reference}")

            self._notify(
                "component_moved",
                {"reference": reference, "position": {"x": x, "y": y}, "rotation": rotation},
            )

            return True

        except Exception as e:
            logger.error(f"Failed to move component: {e}")
            return False

    def delete_component(self, reference: str) -> bool:
        """Delete a component from the board."""
        try:
            board = self._get_board()
            footprints = board.get_footprints()

            # Find the footprint by reference
            target_fp = None
            for fp in footprints:
                if fp.reference_field and fp.reference_field.text.value == reference:
                    target_fp = fp
                    break

            if not target_fp:
                logger.error(f"Component not found: {reference}")
                return False

            self._apply_remove(board, [target_fp], f"Deleted component {reference}")

            self._notify("component_deleted", {"reference": reference})

            return True

        except Exception as e:
            logger.error(f"Failed to delete component: {e}")
            return False
