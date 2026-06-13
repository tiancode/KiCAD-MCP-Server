"""Pad query commands: get_component_pads, get_pad_position.

Split out of the former monolithic commands/component.py.
"""

import logging
from typing import Any, Dict, List, Optional

import pcbnew

logger = logging.getLogger("kicad_interface")


class PadsMixin:
    def get_component_pads(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get all pads for a component with their positions and net connections"""
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

            from utils.units import nm_to_unit, normalize_unit

            unit = normalize_unit(params.get("unit", "mm"))

            # Find the component
            module = self.board.FindFootprintByReference(reference)
            if not module:
                return {
                    "success": False,
                    "message": "Component not found",
                    "errorDetails": f"Could not find component: {reference}",
                }

            pads = []
            for pad in module.Pads():
                pos = pad.GetPosition()
                size = pad.GetSize()

                # Get pad shape as string
                shape_map = {
                    pcbnew.PAD_SHAPE_CIRCLE: "circle",
                    pcbnew.PAD_SHAPE_RECT: "rect",
                    pcbnew.PAD_SHAPE_OVAL: "oval",
                    pcbnew.PAD_SHAPE_TRAPEZOID: "trapezoid",
                    pcbnew.PAD_SHAPE_ROUNDRECT: "roundrect",
                    pcbnew.PAD_SHAPE_CHAMFERED_RECT: "chamfered_rect",
                    pcbnew.PAD_SHAPE_CUSTOM: "custom",
                }
                shape = shape_map.get(pad.GetShape(), "unknown")

                # Get pad type
                type_map = {
                    pcbnew.PAD_ATTRIB_PTH: "through_hole",
                    pcbnew.PAD_ATTRIB_SMD: "smd",
                    pcbnew.PAD_ATTRIB_CONN: "connector",
                    pcbnew.PAD_ATTRIB_NPTH: "npth",
                }
                pad_type = type_map.get(pad.GetAttribute(), "unknown")

                pads.append(
                    {
                        "name": pad.GetName(),
                        "number": pad.GetNumber(),
                        "position": {
                            "x": nm_to_unit(pos.x, unit),
                            "y": nm_to_unit(pos.y, unit),
                            "unit": unit,
                        },
                        "net": pad.GetNetname(),
                        "netCode": pad.GetNetCode(),
                        "shape": shape,
                        "type": pad_type,
                        "size": {
                            "x": nm_to_unit(size.x, unit),
                            "y": nm_to_unit(size.y, unit),
                            "unit": unit,
                        },
                        "drillSize": (
                            nm_to_unit(pad.GetDrillSize().x, unit)
                            if pad.GetDrillSize().x > 0
                            else None
                        ),
                    }
                )

            # Get component position for reference
            comp_pos = module.GetPosition()

            return {
                "success": True,
                "reference": reference,
                "componentPosition": {
                    "x": nm_to_unit(comp_pos.x, unit),
                    "y": nm_to_unit(comp_pos.y, unit),
                    "unit": unit,
                },
                "padCount": len(pads),
                "pads": pads,
            }

        except Exception as e:
            logger.error(f"Error getting component pads: {str(e)}")
            return {
                "success": False,
                "message": "Failed to get component pads",
                "errorDetails": str(e),
            }

    def get_pad_position(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get the position of a specific pad on a component"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            reference = params.get("reference")
            # The TS schema names this argument ``pad`` (and that's what
            # MCP clients send); the SWIG handler originally read
            # ``padName`` / ``padNumber``.  Accept all three so the
            # documented name works and legacy callers don't break.
            pad_name = params.get("pad") or params.get("padName") or params.get("padNumber")

            if not reference:
                return {
                    "success": False,
                    "message": "Missing reference",
                    "errorDetails": "reference parameter is required",
                }
            if not pad_name:
                return {
                    "success": False,
                    "message": "Missing pad identifier",
                    "errorDetails": "pad (or padName / padNumber) parameter is required",
                }

            # Find the component
            module = self.board.FindFootprintByReference(reference)
            if not module:
                return {
                    "success": False,
                    "message": "Component not found",
                    "errorDetails": f"Could not find component: {reference}",
                }

            # Find the specific pad
            pad = module.FindPadByNumber(str(pad_name))
            if not pad:
                # List available pads in error message
                available_pads = [p.GetNumber() for p in module.Pads()]
                return {
                    "success": False,
                    "message": "Pad not found",
                    "errorDetails": f"Pad '{pad_name}' not found on {reference}. Available pads: {', '.join(available_pads)}",
                }

            pos = pad.GetPosition()
            size = pad.GetSize()

            return {
                "success": True,
                "reference": reference,
                "padName": pad.GetNumber(),
                "position": {"x": pos.x / 1000000, "y": pos.y / 1000000, "unit": "mm"},
                "net": pad.GetNetname(),
                "netCode": pad.GetNetCode(),
                "size": {"x": size.x / 1000000, "y": size.y / 1000000, "unit": "mm"},
            }

        except Exception as e:
            logger.error(f"Error getting pad position: {str(e)}")
            return {
                "success": False,
                "message": "Failed to get pad position",
                "errorDetails": str(e),
            }
