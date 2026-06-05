"""Array placement and alignment commands.

Split out of the former monolithic commands/component.py.
"""

import logging
import math
from typing import Any, Dict, List, Optional

import pcbnew

logger = logging.getLogger("kicad_interface")


class ArrayMixin:
    def place_component_array(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Place an array of components in a grid or circular pattern"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            component_id = params.get("componentId")
            pattern = params.get("pattern", "grid")  # grid or circular
            count = params.get("count")
            reference_prefix = params.get("referencePrefix", "U")
            value = params.get("value")

            if not component_id or not count:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "componentId and count are required",
                }

            if pattern == "grid":
                start_position = params.get("startPosition")
                rows = params.get("rows")
                columns = params.get("columns")
                spacing_x = params.get("spacingX")
                spacing_y = params.get("spacingY")
                rotation = params.get("rotation", 0)
                layer = params.get("layer", "F.Cu")

                if not start_position or not rows or not columns or not spacing_x or not spacing_y:
                    return {
                        "success": False,
                        "message": "Missing grid parameters",
                        "errorDetails": "For grid pattern, startPosition, rows, columns, spacingX, and spacingY are required",
                    }

                if rows * columns != count:
                    return {
                        "success": False,
                        "message": "Invalid grid parameters",
                        "errorDetails": "rows * columns must equal count",
                    }

                placed_components = self._place_grid_array(
                    component_id,
                    start_position,
                    rows,
                    columns,
                    spacing_x,
                    spacing_y,
                    reference_prefix,
                    value,
                    rotation,
                    layer,
                )

            elif pattern == "circular":
                center = params.get("center")
                radius = params.get("radius")
                angle_start = params.get("angleStart", 0)
                angle_step = params.get("angleStep")
                rotation_offset = params.get("rotationOffset", 0)
                layer = params.get("layer", "F.Cu")

                if not center or not radius or not angle_step:
                    return {
                        "success": False,
                        "message": "Missing circular parameters",
                        "errorDetails": "For circular pattern, center, radius, and angleStep are required",
                    }

                placed_components = self._place_circular_array(
                    component_id,
                    center,
                    radius,
                    count,
                    angle_start,
                    angle_step,
                    reference_prefix,
                    value,
                    rotation_offset,
                    layer,
                )

            else:
                return {
                    "success": False,
                    "message": "Invalid pattern",
                    "errorDetails": "Pattern must be 'grid' or 'circular'",
                }

            return {
                "success": True,
                "message": f"Placed {count} components in {pattern} pattern",
                "components": placed_components,
            }

        except Exception as e:
            logger.error(f"Error placing component array: {str(e)}")
            return {
                "success": False,
                "message": "Failed to place component array",
                "errorDetails": str(e),
            }

    def align_components(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Align multiple components along a line or distribute them evenly"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            references = params.get("references", [])
            alignment = params.get("alignment", "horizontal")  # horizontal, vertical, or edge
            distribution = params.get("distribution", "none")  # none, equal, or spacing
            spacing = params.get("spacing")

            if not references or len(references) < 2:
                return {
                    "success": False,
                    "message": "Missing references",
                    "errorDetails": "At least two component references are required",
                }

            # Find all referenced components
            components = []
            for ref in references:
                module = self.board.FindFootprintByReference(ref)
                if not module:
                    return {
                        "success": False,
                        "message": "Component not found",
                        "errorDetails": f"Could not find component: {ref}",
                    }
                components.append(module)

            # Perform alignment based on selected option
            if alignment == "horizontal":
                self._align_components_horizontally(components, distribution, spacing)
            elif alignment == "vertical":
                self._align_components_vertically(components, distribution, spacing)
            elif alignment == "edge":
                edge = params.get("edge")
                if not edge:
                    return {
                        "success": False,
                        "message": "Missing edge parameter",
                        "errorDetails": "Edge parameter is required for edge alignment",
                    }
                self._align_components_to_edge(components, edge)
            else:
                return {
                    "success": False,
                    "message": "Invalid alignment option",
                    "errorDetails": "Alignment must be 'horizontal', 'vertical', or 'edge'",
                }

            # Prepare result data
            aligned_components = []
            for module in components:
                pos = module.GetPosition()
                aligned_components.append(
                    {
                        "reference": module.GetReference(),
                        "position": {"x": pos.x / 1000000, "y": pos.y / 1000000, "unit": "mm"},
                        "rotation": module.GetOrientation().AsDegrees(),
                    }
                )

            return {
                "success": True,
                "message": f"Aligned {len(components)} components",
                "alignment": alignment,
                "distribution": distribution,
                "components": aligned_components,
            }

        except Exception as e:
            logger.error(f"Error aligning components: {str(e)}")
            return {
                "success": False,
                "message": "Failed to align components",
                "errorDetails": str(e),
            }

    def _place_grid_array(
        self,
        component_id: str,
        start_position: Dict[str, Any],
        rows: int,
        columns: int,
        spacing_x: float,
        spacing_y: float,
        reference_prefix: str,
        value: str,
        rotation: float,
        layer: str,
    ) -> List[Dict[str, Any]]:
        """Place components in a grid pattern and return the list of placed components"""
        placed = []

        unit = start_position.get("unit", "mm")

        for row in range(rows):
            for col in range(columns):
                # Calculate position
                x = start_position["x"] + (col * spacing_x)
                y = start_position["y"] + (row * spacing_y)

                # Generate reference
                index = row * columns + col + 1
                component_reference = f"{reference_prefix}{index}"

                # Place component
                result = self.place_component(
                    {
                        "componentId": component_id,
                        "position": {"x": x, "y": y, "unit": unit},
                        "reference": component_reference,
                        "value": value,
                        "rotation": rotation,
                        "layer": layer,
                    }
                )

                if result["success"]:
                    placed.append(result["component"])

        return placed

    def _place_circular_array(
        self,
        component_id: str,
        center: Dict[str, Any],
        radius: float,
        count: int,
        angle_start: float,
        angle_step: float,
        reference_prefix: str,
        value: str,
        rotation_offset: float,
        layer: str,
    ) -> List[Dict[str, Any]]:
        """Place components in a circular pattern and return the list of placed components"""
        placed = []

        # Get unit
        unit = center.get("unit", "mm")

        for i in range(count):
            # Calculate angle for this component
            angle = angle_start + (i * angle_step)
            angle_rad = math.radians(angle)

            # Calculate position
            x = center["x"] + (radius * math.cos(angle_rad))
            y = center["y"] + (radius * math.sin(angle_rad))

            # Generate reference
            component_reference = f"{reference_prefix}{i+1}"

            # Calculate rotation (pointing outward from center)
            component_rotation = angle + rotation_offset

            # Place component
            result = self.place_component(
                {
                    "componentId": component_id,
                    "position": {"x": x, "y": y, "unit": unit},
                    "reference": component_reference,
                    "value": value,
                    "rotation": component_rotation,
                    "layer": layer,
                }
            )

            if result["success"]:
                placed.append(result["component"])

        return placed

    def _align_components_horizontally(
        self, components: List[pcbnew.FOOTPRINT], distribution: str, spacing: Optional[float]
    ) -> None:
        """Align components horizontally and optionally distribute them"""
        if not components:
            return

        # Find the average Y coordinate
        y_sum = sum(module.GetPosition().y for module in components)
        y_avg = y_sum // len(components)

        # Sort components by X position
        components.sort(key=lambda m: m.GetPosition().x)

        # Set Y coordinate for all components
        for module in components:
            pos = module.GetPosition()
            module.SetPosition(pcbnew.VECTOR2I(pos.x, y_avg))

        # Handle distribution if requested
        if distribution == "equal" and len(components) > 1:
            # Get leftmost and rightmost X coordinates
            x_min = components[0].GetPosition().x
            x_max = components[-1].GetPosition().x

            # Calculate equal spacing
            total_space = x_max - x_min
            spacing_nm = total_space // (len(components) - 1)

            # Set X positions with equal spacing
            for i in range(1, len(components) - 1):
                pos = components[i].GetPosition()
                new_x = x_min + (i * spacing_nm)
                components[i].SetPosition(pcbnew.VECTOR2I(new_x, pos.y))

        elif distribution == "spacing" and spacing is not None:
            # Convert spacing to nanometers
            spacing_nm = int(spacing * 1000000)  # assuming mm

            # Set X positions with the specified spacing
            x_current = components[0].GetPosition().x
            for i in range(1, len(components)):
                pos = components[i].GetPosition()
                x_current += spacing_nm
                components[i].SetPosition(pcbnew.VECTOR2I(x_current, pos.y))

    def _align_components_vertically(
        self, components: List[pcbnew.FOOTPRINT], distribution: str, spacing: Optional[float]
    ) -> None:
        """Align components vertically and optionally distribute them"""
        if not components:
            return

        # Find the average X coordinate
        x_sum = sum(module.GetPosition().x for module in components)
        x_avg = x_sum // len(components)

        # Sort components by Y position
        components.sort(key=lambda m: m.GetPosition().y)

        # Set X coordinate for all components
        for module in components:
            pos = module.GetPosition()
            module.SetPosition(pcbnew.VECTOR2I(x_avg, pos.y))

        # Handle distribution if requested
        if distribution == "equal" and len(components) > 1:
            # Get topmost and bottommost Y coordinates
            y_min = components[0].GetPosition().y
            y_max = components[-1].GetPosition().y

            # Calculate equal spacing
            total_space = y_max - y_min
            spacing_nm = total_space // (len(components) - 1)

            # Set Y positions with equal spacing
            for i in range(1, len(components) - 1):
                pos = components[i].GetPosition()
                new_y = y_min + (i * spacing_nm)
                components[i].SetPosition(pcbnew.VECTOR2I(pos.x, new_y))

        elif distribution == "spacing" and spacing is not None:
            # Convert spacing to nanometers
            spacing_nm = int(spacing * 1000000)  # assuming mm

            # Set Y positions with the specified spacing
            y_current = components[0].GetPosition().y
            for i in range(1, len(components)):
                pos = components[i].GetPosition()
                y_current += spacing_nm
                components[i].SetPosition(pcbnew.VECTOR2I(pos.x, y_current))

    def _align_components_to_edge(self, components: List[pcbnew.FOOTPRINT], edge: str) -> None:
        """Align components to the specified edge of the board"""
        if not components:
            return

        # Get board bounds
        board_box = self.board.GetBoardEdgesBoundingBox()
        left = board_box.GetLeft()
        right = board_box.GetRight()
        top = board_box.GetTop()
        bottom = board_box.GetBottom()

        # Align based on specified edge
        if edge == "left":
            for module in components:
                pos = module.GetPosition()
                module.SetPosition(pcbnew.VECTOR2I(left + 2000000, pos.y))  # 2mm offset from edge
        elif edge == "right":
            for module in components:
                pos = module.GetPosition()
                module.SetPosition(pcbnew.VECTOR2I(right - 2000000, pos.y))  # 2mm offset from edge
        elif edge == "top":
            for module in components:
                pos = module.GetPosition()
                module.SetPosition(pcbnew.VECTOR2I(pos.x, top + 2000000))  # 2mm offset from edge
        elif edge == "bottom":
            for module in components:
                pos = module.GetPosition()
                module.SetPosition(pcbnew.VECTOR2I(pos.x, bottom - 2000000))  # 2mm offset from edge
        else:
            logger.warning(f"Unknown edge alignment: {edge}")
