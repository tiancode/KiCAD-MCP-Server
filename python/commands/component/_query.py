"""Component query commands: properties, listing, find.

Split out of the former monolithic commands/component.py.
"""

import logging
from typing import Any, Dict

import pcbnew
from utils.responses import failed, no_board_loaded

from ._shared import bbox_to_dict, resolve_footprint

logger = logging.getLogger("kicad_interface")


class QueryMixin:
    def get_component_properties(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get detailed properties of a component"""
        try:
            if not self.board:
                return no_board_loaded()

            module, err = resolve_footprint(self.board, params)
            if err:
                return err

            pos = module.GetPosition()
            x_mm = pos.x / 1000000
            y_mm = pos.y / 1000000

            bbox_data = bbox_to_dict(module.GetBoundingBox())

            # Try to get courtyard bounds (preferred for placement clearance)
            courtyard_data = None
            try:
                for layer_id in [pcbnew.F_CrtYd, pcbnew.B_CrtYd]:
                    courtyard = module.GetCourtyard(layer_id)
                    if courtyard and courtyard.OutlineCount() > 0:
                        courtyard_data = bbox_to_dict(courtyard.BBox())
                        break
            except (AttributeError, RuntimeError):
                # best-effort: courtyard may not exist on this footprint, or
                # the SWIG API may differ across KiCAD versions.  The caller
                # already returns the rest of the data with courtyard_data=None.
                pass

            return {
                "success": True,
                "component": {
                    "reference": module.GetReference(),
                    "value": module.GetValue(),
                    "footprint": module.GetFPIDAsString(),
                    "position": {"x": x_mm, "y": y_mm, "unit": "mm"},
                    "rotation": module.GetOrientation().AsDegrees(),
                    "layer": self.board.GetLayerName(module.GetLayer()),
                    "attributes": {
                        "smd": module.GetAttributes() & pcbnew.FP_SMD,
                        "through_hole": module.GetAttributes() & pcbnew.FP_THROUGH_HOLE,
                        "board_only": module.GetAttributes() & pcbnew.FP_BOARD_ONLY,
                    },
                    "boundingBox": bbox_data,
                    "courtyard": courtyard_data,
                },
            }

        except Exception as e:
            logger.error(f"Error getting component properties: {str(e)}")
            return failed("Failed to get component properties", e)

    def get_component_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get a list of all components on the board"""
        try:
            if not self.board:
                return no_board_loaded()

            from utils.footprint_class import is_mounting_hole

            components = []
            # Every footprint on the board is listed — including mounting holes
            # (MH1-4 etc.), which are real footprints with real references.  The
            # IPC listing does the same; they are tagged with is_mounting_hole
            # so a consumer can filter intentionally instead of the two backends
            # silently disagreeing on the component set.
            for module in self.board.GetFootprints():
                pos = module.GetPosition()
                x_mm = pos.x / 1000000
                y_mm = pos.y / 1000000

                bbox_data = bbox_to_dict(module.GetBoundingBox())

                reference = module.GetReference()
                fpid = module.GetFPIDAsString()
                components.append(
                    {
                        "reference": reference,
                        "value": module.GetValue(),
                        "footprint": fpid,
                        "position": {"x": x_mm, "y": y_mm, "unit": "mm"},
                        "rotation": module.GetOrientation().AsDegrees(),
                        "layer": self.board.GetLayerName(module.GetLayer()),
                        "boundingBox": bbox_data,
                        "is_mounting_hole": is_mounting_hole(fpid, reference),
                    }
                )

            from utils.pagination import paginate

            components, page = paginate(components, params)
            return {"success": True, "components": components, **page}

        except Exception as e:
            logger.error(f"Error getting component list: {str(e)}")
            return failed("Failed to get component list", e)

    def find_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Find components on the loaded PCB (board, not schematic).

        Matches on any of: a free-text ``query`` (case-insensitive substring
        checked across reference, value AND footprint-id), or the targeted
        ``reference`` / ``value`` / ``footprint`` substring filters. All
        supplied criteria combine with AND.
        """
        try:
            if not self.board:
                return no_board_loaded()

            # Get search parameters (tolerate explicit null / non-string).
            query = str(params.get("query") or "").lower()
            reference_pattern = str(params.get("reference") or "").lower()
            value_pattern = str(params.get("value") or "").lower()
            footprint_pattern = str(params.get("footprint") or "").lower()

            if not query and not reference_pattern and not value_pattern and not footprint_pattern:
                return {
                    "success": False,
                    "message": "Missing search criteria",
                    "errorDetails": (
                        "Provide a free-text `query`, or at least one of "
                        "reference / value / footprint."
                    ),
                }

            matches = []
            for module in self.board.GetFootprints():
                ref = module.GetReference().lower()
                val = module.GetValue().lower()
                fp = module.GetFPIDAsString().lower()

                # Check if component matches all provided criteria (AND).
                match = True
                if query and query not in ref and query not in val and query not in fp:
                    match = False
                if reference_pattern and reference_pattern not in ref:
                    match = False
                if value_pattern and value_pattern not in val:
                    match = False
                if footprint_pattern and footprint_pattern not in fp:
                    match = False

                if match:
                    pos = module.GetPosition()
                    matches.append(
                        {
                            "reference": module.GetReference(),
                            "value": module.GetValue(),
                            "footprint": module.GetFPIDAsString(),
                            "position": {"x": pos.x / 1000000, "y": pos.y / 1000000, "unit": "mm"},
                            "rotation": module.GetOrientation().AsDegrees(),
                            "layer": self.board.GetLayerName(module.GetLayer()),
                        }
                    )

            return {"success": True, "matchCount": len(matches), "components": matches}

        except Exception as e:
            logger.error(f"Error finding components: {str(e)}")
            return failed("Failed to find components", e)
