"""Component query commands: properties, listing, find.

Split out of the former monolithic commands/component.py.
"""

import logging
from typing import Any, Dict, List, Optional

import pcbnew

logger = logging.getLogger("kicad_interface")


class QueryMixin:
    def get_component_properties(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get detailed properties of a component"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            reference = params.get("reference")
            if not reference:
                return {
                    "success": False,
                    "message": "Missing reference",
                    "errorDetails": "reference parameter is required",
                }

            # Find the component
            module = self.board.FindFootprintByReference(reference)
            if not module:
                return {
                    "success": False,
                    "message": "Component not found",
                    "errorDetails": f"Could not find component: {reference}",
                }

            # Get position in mm
            pos = module.GetPosition()
            x_mm = pos.x / 1000000
            y_mm = pos.y / 1000000

            # Get bounding box
            bbox = module.GetBoundingBox()
            bbox_data = {
                "min_x": bbox.GetLeft() / 1000000,
                "min_y": bbox.GetTop() / 1000000,
                "max_x": bbox.GetRight() / 1000000,
                "max_y": bbox.GetBottom() / 1000000,
                "width": (bbox.GetRight() - bbox.GetLeft()) / 1000000,
                "height": (bbox.GetBottom() - bbox.GetTop()) / 1000000,
                "unit": "mm",
            }

            # Try to get courtyard bounds (preferred for placement clearance)
            courtyard_data = None
            try:
                for layer_id in [pcbnew.F_CrtYd, pcbnew.B_CrtYd]:
                    courtyard = module.GetCourtyard(layer_id)
                    if courtyard and courtyard.OutlineCount() > 0:
                        cbox = courtyard.BBox()
                        courtyard_data = {
                            "min_x": cbox.GetLeft() / 1000000,
                            "min_y": cbox.GetTop() / 1000000,
                            "max_x": cbox.GetRight() / 1000000,
                            "max_y": cbox.GetBottom() / 1000000,
                            "width": (cbox.GetRight() - cbox.GetLeft()) / 1000000,
                            "height": (cbox.GetBottom() - cbox.GetTop()) / 1000000,
                            "unit": "mm",
                        }
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
            return {
                "success": False,
                "message": "Failed to get component properties",
                "errorDetails": str(e),
            }

    def get_component_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get a list of all components on the board"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            components = []
            for module in self.board.GetFootprints():
                pos = module.GetPosition()
                x_mm = pos.x / 1000000
                y_mm = pos.y / 1000000

                bbox = module.GetBoundingBox()
                bbox_data = {
                    "min_x": bbox.GetLeft() / 1000000,
                    "min_y": bbox.GetTop() / 1000000,
                    "max_x": bbox.GetRight() / 1000000,
                    "max_y": bbox.GetBottom() / 1000000,
                    "width": (bbox.GetRight() - bbox.GetLeft()) / 1000000,
                    "height": (bbox.GetBottom() - bbox.GetTop()) / 1000000,
                    "unit": "mm",
                }

                components.append(
                    {
                        "reference": module.GetReference(),
                        "value": module.GetValue(),
                        "footprint": module.GetFPIDAsString(),
                        "position": {"x": x_mm, "y": y_mm, "unit": "mm"},
                        "rotation": module.GetOrientation().AsDegrees(),
                        "layer": self.board.GetLayerName(module.GetLayer()),
                        "boundingBox": bbox_data,
                    }
                )

            from utils.pagination import paginate

            components, page = paginate(components, params)
            return {"success": True, "components": components, **page}

        except Exception as e:
            logger.error(f"Error getting component list: {str(e)}")
            return {
                "success": False,
                "message": "Failed to get component list",
                "errorDetails": str(e),
            }

    def find_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Find components matching search criteria (reference, value, or footprint pattern)"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            # Get search parameters
            reference_pattern = params.get("reference", "").lower()
            value_pattern = params.get("value", "").lower()
            footprint_pattern = params.get("footprint", "").lower()

            if not reference_pattern and not value_pattern and not footprint_pattern:
                return {
                    "success": False,
                    "message": "Missing search criteria",
                    "errorDetails": "At least one of reference, value, or footprint pattern is required",
                }

            matches = []
            for module in self.board.GetFootprints():
                ref = module.GetReference().lower()
                val = module.GetValue().lower()
                fp = module.GetFPIDAsString().lower()

                # Check if component matches all provided patterns
                match = True
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
            return {
                "success": False,
                "message": "Failed to find components",
                "errorDetails": str(e),
            }
