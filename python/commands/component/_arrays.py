"""Array placement and alignment commands.

Split out of the former monolithic commands/component.py.
"""

import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

import pcbnew
from utils.responses import failed, no_board_loaded
from utils.units import unit_to_nm_scale

logger = logging.getLogger("kicad_interface")


def _to_mm(value: float, unit: Optional[str]) -> float:
    """Convert a length given in ``unit`` (mm/mil/inch) to millimetres.

    Raises :class:`~utils.units.InvalidUnitError` for an unrecognised unit
    (surfaced as a VALIDATION refusal by ``failed``).
    """
    return float(value) * unit_to_nm_scale(unit) / 1_000_000.0


def _array_failure_code(failures: List[Dict[str, Any]]) -> str:
    """Pick a truthful errorCode for an all-failed array placement.

    When every underlying place_component failure shares one code, surface it;
    otherwise fall back to VALIDATION (never INTERNAL_ERROR — the individual
    reasons are carried in ``failures``).
    """
    codes = {str(c) for f in failures if (c := f.get("errorCode"))}
    if len(codes) == 1:
        return codes.pop()
    return "VALIDATION"


class ArrayMixin:
    @staticmethod
    def _resolve_array_reference(params: Dict[str, Any]) -> Tuple[str, int]:
        """Resolve (prefix, first_index) from startReference / referencePrefix.

        ``startReference`` follows KiCad's designator convention: a trailing
        number is the first index ("C5" -> ("C", 5)), no trailing number starts
        at 1 ("U" -> ("U", 1)).  Falls back to the legacy ``referencePrefix``
        (numbering from 1), else "U".
        """
        start_reference = params.get("startReference")
        legacy_prefix = params.get("referencePrefix")
        if start_reference:
            text = str(start_reference).strip()
            m = re.match(r"^(.*?)(\d+)$", text)
            if m:
                return (m.group(1) or legacy_prefix or "U"), int(m.group(2))
            return (text or legacy_prefix or "U"), 1
        return (legacy_prefix or "U"), 1

    def place_component_array(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Place an array of components in a grid or circular pattern.

        The MCP schema advertises the grid vocabulary ``rows`` / ``columns`` /
        ``rowSpacing`` / ``columnSpacing`` / ``startReference`` /
        ``startPosition``; the legacy names (``count`` / ``spacingX`` /
        ``spacingY`` / ``referencePrefix``) are still accepted for back-compat.
        Grid ``count`` is derived from ``rows * columns``.
        """
        try:
            if not self.board:
                return no_board_loaded()

            component_id = params.get("componentId")
            pattern = params.get("pattern", "grid")  # grid or circular
            value = params.get("value")
            footprint = params.get("footprint")

            if not component_id:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "componentId is required",
                    "errorCode": "VALIDATION",
                }

            reference_prefix, start_index = self._resolve_array_reference(params)

            if pattern == "grid":
                start_position = params.get("startPosition") or params.get("position")
                rows = params.get("rows")
                columns = params.get("columns")
                # Schema: rowSpacing spaces rows apart (Y); columnSpacing spaces
                # columns apart (X).  Legacy: spacingY / spacingX.
                spacing_x = params.get("spacingX")
                if spacing_x is None:
                    spacing_x = params.get("columnSpacing")
                spacing_y = params.get("spacingY")
                if spacing_y is None:
                    spacing_y = params.get("rowSpacing")
                rotation = params.get("rotation", 0)
                layer = params.get("layer", "F.Cu")

                if (
                    not start_position
                    or not rows
                    or not columns
                    or spacing_x is None
                    or spacing_y is None
                ):
                    return {
                        "success": False,
                        "message": "Missing grid parameters",
                        "errorDetails": (
                            "For a grid array, startPosition, rows, columns, "
                            "rowSpacing and columnSpacing are required."
                        ),
                        "errorCode": "VALIDATION",
                    }

                rows = int(rows)
                columns = int(columns)
                derived_count = rows * columns
                # ``count`` is derived from rows*columns; a legacy caller may
                # still pass it, but it must agree with the grid dimensions.
                count = params.get("count")
                if count is not None and int(count) != derived_count:
                    return {
                        "success": False,
                        "message": "Invalid grid parameters",
                        "errorDetails": (
                            f"rows * columns ({derived_count}) must equal " f"count ({int(count)})."
                        ),
                        "errorCode": "VALIDATION",
                    }
                count = derived_count

                spacing_x = float(spacing_x)
                spacing_y = float(spacing_y)
                # Spacing must be positive: a 0 (or negative) spacing stacks
                # every copy on the start position — never the intent, and a
                # silent stack looks like success.
                if spacing_x <= 0 or spacing_y <= 0:
                    return {
                        "success": False,
                        "message": "Invalid grid spacing",
                        "errorDetails": (
                            "rowSpacing and columnSpacing must be greater than 0 "
                            f"(got columnSpacing={spacing_x}, rowSpacing={spacing_y})."
                        ),
                        "errorCode": "VALIDATION",
                    }

                placed_components, placement_failures = self._place_grid_array(
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
                    start_index,
                    footprint,
                )

            elif pattern == "circular":
                center = params.get("center")
                radius = params.get("radius")
                count = params.get("count")
                angle_start = params.get("angleStart", 0)
                angle_step = params.get("angleStep")
                rotation_offset = params.get("rotationOffset", 0)
                layer = params.get("layer", "F.Cu")

                if not center or not radius or not count or not angle_step:
                    return {
                        "success": False,
                        "message": "Missing circular parameters",
                        "errorDetails": (
                            "For a circular array, center, radius, count and "
                            "angleStep are required."
                        ),
                        "errorCode": "VALIDATION",
                    }
                count = int(count)
                radius = float(radius)
                # A non-positive radius collapses every copy onto the centre —
                # not a real array; refuse rather than silently stack.
                if radius <= 0:
                    return {
                        "success": False,
                        "message": "Invalid circular radius",
                        "errorDetails": f"radius must be greater than 0 (got {radius}).",
                        "errorCode": "VALIDATION",
                    }

                placed_components, placement_failures = self._place_circular_array(
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
                    start_index,
                    footprint,
                )

            else:
                return {
                    "success": False,
                    "message": "Invalid pattern",
                    "errorDetails": "Pattern must be 'grid' or 'circular'",
                    "errorCode": "VALIDATION",
                }

            placed_count = len(placed_components)
            result: Dict[str, Any] = {
                "success": placed_count > 0,
                "message": (f"Placed {placed_count} of {count} components in {pattern} pattern"),
                "components": placed_components,
            }
            if placement_failures:
                # Per-item failures are never silently dropped: surface them so
                # the caller sees which references failed and why.
                result["failures"] = placement_failures
            if placed_count == 0:
                # Nothing landed → a refusal carrying the underlying reasons
                # (VALIDATION unless every failure shares a truthful code).
                result["errorCode"] = _array_failure_code(placement_failures)
            elif placement_failures:
                result["message"] += f" ({len(placement_failures)} failed — see 'failures')"
            return result

        except Exception as e:
            logger.error(f"Error placing component array: {str(e)}")
            return failed("Failed to place component array", e)

    def align_components(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Align multiple components along a line or distribute them evenly"""
        try:
            if not self.board:
                return no_board_loaded()

            references = params.get("references", [])
            # Canonical field is alignmentType (what the TS tool sends); keep
            # `alignment` as a legacy alias so older callers keep working.
            alignment = params.get("alignmentType") or params.get("alignment") or "horizontal"
            spacing = params.get("spacing")
            # The TS tool never sends an explicit `distribution`; a supplied
            # `spacing` means "space these parts apart", so infer it.  An
            # explicit distribution (legacy callers) still wins.  Without either,
            # the parts are only aligned onto a line ("none").
            distribution = params.get("distribution")
            if distribution is None:
                distribution = "spacing" if spacing is not None else "none"
            reference_component = params.get("referenceComponent")

            if not references or len(references) < 2:
                return {
                    "success": False,
                    "message": "Missing references",
                    "errorDetails": "At least two component references are required",
                }

            # Validation-refuse an unknown alignment type (e.g. the removed
            # "grid" that was never implemented) rather than silently defaulting.
            if alignment not in ("horizontal", "vertical", "edge"):
                return {
                    "success": False,
                    "message": f"Invalid alignmentType: {alignment}",
                    "errorCode": "VALIDATION",
                    "errorDetails": "alignmentType must be 'horizontal', 'vertical', or 'edge'.",
                }

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

            # Resolve the anchor (referenceComponent): its coordinate fixes the
            # aligned axis and the spacing sequence starts from it.  It may be one
            # of the references or any other footprint on the board.
            anchor = None
            if reference_component:
                for module in components:
                    if module.GetReference() == reference_component:
                        anchor = module
                        break
                if anchor is None:
                    anchor = self.board.FindFootprintByReference(reference_component)
                if anchor is None:
                    return {
                        "success": False,
                        "message": "Reference component not found",
                        "errorCode": "VALIDATION",
                        "errorDetails": (
                            f"referenceComponent '{reference_component}' is not a "
                            f"component on the board."
                        ),
                    }

            if alignment == "horizontal":
                self._align_components_horizontally(components, distribution, spacing, anchor)
            elif alignment == "vertical":
                self._align_components_vertically(components, distribution, spacing, anchor)
            elif alignment == "edge":
                edge = params.get("edge")
                if not edge:
                    return {
                        "success": False,
                        "message": "Missing edge parameter",
                        "errorDetails": "Edge parameter is required for edge alignment",
                    }
                self._align_components_to_edge(components, edge)

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

            result: Dict[str, Any] = {
                "success": True,
                "message": f"Aligned {len(components)} components",
                "alignment": alignment,
                "alignmentType": alignment,
                "distribution": distribution,
                "components": aligned_components,
            }
            if reference_component:
                result["referenceComponent"] = reference_component
            if spacing is not None:
                result["spacing"] = spacing
            return result

        except Exception as e:
            logger.error(f"Error aligning components: {str(e)}")
            return failed("Failed to align components", e)

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
        start_index: int = 1,
        footprint: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Place a grid of components; return (placed, failures).

        Positions are computed canonically in millimetres: ``startPosition`` is
        converted from its own unit to mm, then the documented-mm spacings are
        added — so a mil/inch start does not silently rescale the mm spacing.
        """
        placed: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []

        unit = start_position.get("unit") or "mm"
        start_x_mm = _to_mm(start_position["x"], unit)
        start_y_mm = _to_mm(start_position["y"], unit)

        for row in range(rows):
            for col in range(columns):
                x = start_x_mm + (col * spacing_x)
                y = start_y_mm + (row * spacing_y)

                index = row * columns + col + start_index
                component_reference = f"{reference_prefix}{index}"

                result = self.place_component(
                    {
                        "componentId": component_id,
                        "position": {"x": x, "y": y, "unit": "mm"},
                        "reference": component_reference,
                        "value": value,
                        "rotation": rotation,
                        "layer": layer,
                        "footprint": footprint,
                    }
                )

                if result.get("success"):
                    placed.append(result["component"])
                else:
                    failures.append(
                        {
                            "reference": component_reference,
                            "message": result.get("message") or "place_component failed",
                            "errorCode": result.get("errorCode"),
                        }
                    )

        return placed, failures

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
        start_index: int = 1,
        footprint: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Place a circular array of components; return (placed, failures).

        Positions are computed canonically in millimetres: ``center`` is
        converted from its own unit to mm, then the documented-mm radius is
        applied — so a mil/inch centre does not silently rescale the mm radius.
        """
        placed: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []

        unit = center.get("unit") or "mm"
        center_x_mm = _to_mm(center["x"], unit)
        center_y_mm = _to_mm(center["y"], unit)

        for i in range(count):
            angle = angle_start + (i * angle_step)
            angle_rad = math.radians(angle)

            x = center_x_mm + (radius * math.cos(angle_rad))
            y = center_y_mm + (radius * math.sin(angle_rad))

            component_reference = f"{reference_prefix}{i + start_index}"

            # Calculate rotation (pointing outward from center)
            component_rotation = angle + rotation_offset

            result = self.place_component(
                {
                    "componentId": component_id,
                    "position": {"x": x, "y": y, "unit": "mm"},
                    "reference": component_reference,
                    "value": value,
                    "rotation": component_rotation,
                    "layer": layer,
                    "footprint": footprint,
                }
            )

            if result.get("success"):
                placed.append(result["component"])
            else:
                failures.append(
                    {
                        "reference": component_reference,
                        "message": result.get("message") or "place_component failed",
                        "errorCode": result.get("errorCode"),
                    }
                )

        return placed, failures

    def _align_components_horizontally(
        self,
        components: List[pcbnew.FOOTPRINT],
        distribution: str,
        spacing: Optional[float],
        anchor: Optional["pcbnew.FOOTPRINT"] = None,
    ) -> None:
        """Align components onto one Y line and optionally distribute them.

        When ``anchor`` is given its Y coordinate is the shared line (the fixed
        axis) and, for ``distribution == "spacing"``, the spacing sequence is
        laid out from the anchor's X so the anchor itself does not move.
        """
        if not components:
            return

        # The shared Y line: the anchor's Y when one is given, else the average.
        if anchor is not None:
            y_line = anchor.GetPosition().y
        else:
            y_line = sum(module.GetPosition().y for module in components) // len(components)

        components.sort(key=lambda m: m.GetPosition().x)

        for module in components:
            pos = module.GetPosition()
            module.SetPosition(pcbnew.VECTOR2I(pos.x, y_line))

        if distribution == "equal" and len(components) > 1:
            x_min = components[0].GetPosition().x
            x_max = components[-1].GetPosition().x

            total_space = x_max - x_min
            spacing_nm = total_space // (len(components) - 1)

            for i in range(1, len(components) - 1):
                pos = components[i].GetPosition()
                new_x = x_min + (i * spacing_nm)
                components[i].SetPosition(pcbnew.VECTOR2I(new_x, pos.y))

        elif distribution == "spacing" and spacing is not None:
            # Convert spacing to nanometers
            spacing_nm = int(spacing * 1000000)  # assuming mm

            if anchor is not None and anchor in components:
                # Keep the anchor fixed; space the others outward from it in the
                # sorted order so left/right neighbours stay on their side.
                ai = components.index(anchor)
                base_x = components[ai].GetPosition().x
                for i, module in enumerate(components):
                    pos = module.GetPosition()
                    module.SetPosition(pcbnew.VECTOR2I(base_x + (i - ai) * spacing_nm, pos.y))
            else:
                # Sequence starts at the (external) anchor's X, or the leftmost
                # component when there is no anchor.
                start_x = (
                    anchor.GetPosition().x if anchor is not None else components[0].GetPosition().x
                )
                for i, module in enumerate(components):
                    pos = module.GetPosition()
                    module.SetPosition(pcbnew.VECTOR2I(start_x + i * spacing_nm, pos.y))

    def _align_components_vertically(
        self,
        components: List[pcbnew.FOOTPRINT],
        distribution: str,
        spacing: Optional[float],
        anchor: Optional["pcbnew.FOOTPRINT"] = None,
    ) -> None:
        """Align components onto one X line and optionally distribute them.

        When ``anchor`` is given its X coordinate is the shared line (the fixed
        axis) and, for ``distribution == "spacing"``, the spacing sequence is
        laid out from the anchor's Y so the anchor itself does not move.
        """
        if not components:
            return

        # The shared X line: the anchor's X when one is given, else the average.
        if anchor is not None:
            x_line = anchor.GetPosition().x
        else:
            x_line = sum(module.GetPosition().x for module in components) // len(components)

        components.sort(key=lambda m: m.GetPosition().y)

        for module in components:
            pos = module.GetPosition()
            module.SetPosition(pcbnew.VECTOR2I(x_line, pos.y))

        if distribution == "equal" and len(components) > 1:
            y_min = components[0].GetPosition().y
            y_max = components[-1].GetPosition().y

            total_space = y_max - y_min
            spacing_nm = total_space // (len(components) - 1)

            for i in range(1, len(components) - 1):
                pos = components[i].GetPosition()
                new_y = y_min + (i * spacing_nm)
                components[i].SetPosition(pcbnew.VECTOR2I(pos.x, new_y))

        elif distribution == "spacing" and spacing is not None:
            # Convert spacing to nanometers
            spacing_nm = int(spacing * 1000000)  # assuming mm

            if anchor is not None and anchor in components:
                # Keep the anchor fixed; space the others outward from it in the
                # sorted order so top/bottom neighbours stay on their side.
                ai = components.index(anchor)
                base_y = components[ai].GetPosition().y
                for i, module in enumerate(components):
                    pos = module.GetPosition()
                    module.SetPosition(pcbnew.VECTOR2I(pos.x, base_y + (i - ai) * spacing_nm))
            else:
                # Sequence starts at the (external) anchor's Y, or the topmost
                # component when there is no anchor.
                start_y = (
                    anchor.GetPosition().y if anchor is not None else components[0].GetPosition().y
                )
                for i, module in enumerate(components):
                    pos = module.GetPosition()
                    module.SetPosition(pcbnew.VECTOR2I(pos.x, start_y + i * spacing_nm))

    def _align_components_to_edge(self, components: List[pcbnew.FOOTPRINT], edge: str) -> None:
        """Align components to the specified edge of the board"""
        if not components:
            return

        board_box = self.board.GetBoardEdgesBoundingBox()
        left = board_box.GetLeft()
        right = board_box.GetRight()
        top = board_box.GetTop()
        bottom = board_box.GetBottom()

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
