"""
Board outline command implementations for KiCAD interface
"""

import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Set

import pcbnew
from utils.responses import failed, no_board_loaded
from utils.units import unit_to_nm_scale

logger = logging.getLogger("kicad_interface")

# Process-wide cache for the resolved MountingHole.pretty directory so we don't
# hit the filesystem on every add_mounting_hole call.
_MOUNTINGHOLE_DIR_CACHE: Dict[str, Optional[str]] = {}

# Matches the "plain" MountingHole footprints — a bare diameter, optionally with
# a screw-size suffix (…_M3, …_M2.5) but WITHOUT the _Pad / _DIN965 / _ISO7380 /
# _Via / _TopOnly / _TopBottom variants. Capture group 1 is the diameter in mm.
_PLAIN_MH_RE = re.compile(r"^MountingHole_(\d+(?:\.\d+)?)mm(?:_M\d+(?:\.\d+)?)?$")


def _locate_mountinghole_pretty() -> Optional[str]:
    """Return the path to the stock ``MountingHole.pretty`` dir, or None.

    Mirrors LibraryManager's footprint-dir probing (env override → standard
    install paths → Flatpak runtime glob) so the resolver works across installs
    without a hard dependency on the library manager.
    """
    if "dir" in _MOUNTINGHOLE_DIR_CACHE:
        return _MOUNTINGHOLE_DIR_CACHE["dir"]

    candidates = []
    for var in ("KICAD10_FOOTPRINT_DIR", "KICAD9_FOOTPRINT_DIR", "KICAD8_FOOTPRINT_DIR"):
        val = os.environ.get(var)
        if val:
            candidates.append(val)
    candidates += [
        "/usr/share/kicad/footprints",
        "/usr/local/share/kicad/footprints",
        "C:/Program Files/KiCad/10.0/share/kicad/footprints",
        "C:/Program Files/KiCad/9.0/share/kicad/footprints",
        "C:/Program Files/KiCad/8.0/share/kicad/footprints",
        "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
    ]
    try:
        flatpak = sorted(
            Path("/var/lib/flatpak/runtime/org.kicad.KiCad.Library.Footprints").glob(
                "*/stable/*/files/footprints"
            )
        )
        if flatpak:
            candidates.append(str(flatpak[-1]))
    except OSError:
        pass

    result = None
    for base in candidates:
        pretty = os.path.join(base, "MountingHole.pretty")
        if os.path.isdir(pretty):
            result = pretty
            break
    _MOUNTINGHOLE_DIR_CACHE["dir"] = result
    return result


def _list_mountinghole_footprints() -> Set[str]:
    """Set of footprint names (without .kicad_mod) in the stock MountingHole lib."""
    pretty = _locate_mountinghole_pretty()
    if not pretty:
        return set()
    try:
        return {fn[:-10] for fn in os.listdir(pretty) if fn.endswith(".kicad_mod")}
    except OSError:
        return set()


def _resolve_mountinghole_footprint(diameter: float) -> Optional[str]:
    """Map a hole diameter (mm) to an existing stock MountingHole footprint name.

    Prefers the bare ``MountingHole_<d>mm``; otherwise the plain screw-size
    variant for that exact diameter (e.g. 3.2 → ``MountingHole_3.2mm_M3``, which
    is what the stock KiCAD 10 lib actually ships — there is no bare 3.2mm).
    Falls back to the closest existing plain diameter. Returns None only when the
    stock library can't be located at all, so the caller keeps the legacy
    synthetic name.
    """
    names = _list_mountinghole_footprints()
    if not names:
        return None

    d = f"{diameter:g}"  # 3.2 → "3.2", 3.0 → "3"
    exact_plain = f"MountingHole_{d}mm"
    if exact_plain in names:
        return exact_plain

    # Exact diameter but only a screw-size variant exists (3.2mm_M3, 4.3mm_M4…).
    same_diameter = [
        n for n in names if (m := _PLAIN_MH_RE.match(n)) and float(m.group(1)) == float(diameter)
    ]
    if same_diameter:
        # Shortest = the plain screw variant (…_M3) over any longer relatives.
        return min(same_diameter, key=len)

    # No exact match — pick the closest existing plain diameter.
    best: Optional[str] = None
    best_delta: Optional[float] = None
    for n in names:
        m = _PLAIN_MH_RE.match(n)
        if not m:
            continue
        delta = abs(float(m.group(1)) - float(diameter))
        if best_delta is None or delta < best_delta or (delta == best_delta and len(n) < len(best)):
            best_delta = delta
            best = n
    return best


class BoardOutlineCommands:
    """Handles board outline operations"""

    def __init__(self, board: Optional[pcbnew.BOARD] = None):
        """Initialize with optional board instance"""
        self.board = board

    def add_board_outline(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add a board outline to the PCB"""
        try:
            if not self.board:
                return no_board_loaded()

            # Claude sends dimensions nested inside a "params" key:
            # {"shape": "rectangle", "params": {"x": 0, "y": 0, "width": 38, ...}}
            # Unwrap the inner dict if present so we read dimensions from the right level.
            inner = params.get("params", params)

            shape = params.get("shape", "rectangle")
            width = inner.get("width")
            height = inner.get("height")
            radius = inner.get("radius")
            # Accept both "cornerRadius" and "radius" regardless of shape name.
            # The AI often sends shape=”rectangle” with radius=2.5 — we treat that as rounded_rectangle.
            corner_radius = inner.get("cornerRadius", inner.get("radius", 0))
            if shape == "rectangle" and corner_radius > 0:
                shape = "rounded_rectangle"
            points = inner.get("points", [])
            unit = inner.get("unit", "mm")

            # Position: accept top-left corner (x/y) or center (centerX/centerY).
            # Default: top-left at (0,0) so the board occupies positive coordinate space
            # and is consistent with component placement coordinates.
            x = inner.get("x")
            y = inner.get("y")
            if x is not None or y is not None:
                ox = x if x is not None else 0.0
                oy = y if y is not None else 0.0
                center_x = ox + (width or 0) / 2.0
                center_y = oy + (height or 0) / 2.0
            else:
                raw_cx = inner.get("centerX")
                raw_cy = inner.get("centerY")
                if raw_cx is not None or raw_cy is not None:
                    center_x = raw_cx if raw_cx is not None else 0.0
                    center_y = raw_cy if raw_cy is not None else 0.0
                else:
                    # No position given → place top-left at (0,0)
                    center_x = (width or 0) / 2.0
                    center_y = (height or 0) / 2.0

            if shape not in ["rectangle", "circle", "polygon", "rounded_rectangle"]:
                return {
                    "success": False,
                    "message": "Invalid shape",
                    "errorDetails": f"Shape '{shape}' not supported",
                }

            # Convert to internal units (nanometers)
            scale = unit_to_nm_scale(unit)

            # Create drawing for edge cuts
            edge_layer = self.board.GetLayerID("Edge.Cuts")

            if shape == "rectangle":
                if width is None or height is None:
                    return {
                        "success": False,
                        "message": "Missing dimensions",
                        "errorDetails": "Both width and height are required for rectangle",
                    }

                width_nm = int(width * scale)
                height_nm = int(height * scale)
                center_x_nm = int(center_x * scale)
                center_y_nm = int(center_y * scale)

                # Create rectangle
                top_left = pcbnew.VECTOR2I(
                    center_x_nm - width_nm // 2, center_y_nm - height_nm // 2
                )
                top_right = pcbnew.VECTOR2I(
                    center_x_nm + width_nm // 2, center_y_nm - height_nm // 2
                )
                bottom_right = pcbnew.VECTOR2I(
                    center_x_nm + width_nm // 2, center_y_nm + height_nm // 2
                )
                bottom_left = pcbnew.VECTOR2I(
                    center_x_nm - width_nm // 2, center_y_nm + height_nm // 2
                )

                # Add lines for rectangle
                self._add_edge_line(top_left, top_right, edge_layer)
                self._add_edge_line(top_right, bottom_right, edge_layer)
                self._add_edge_line(bottom_right, bottom_left, edge_layer)
                self._add_edge_line(bottom_left, top_left, edge_layer)

            elif shape == "rounded_rectangle":
                if width is None or height is None:
                    return {
                        "success": False,
                        "message": "Missing dimensions",
                        "errorDetails": "Both width and height are required for rounded rectangle",
                    }

                width_nm = int(width * scale)
                height_nm = int(height * scale)
                center_x_nm = int(center_x * scale)
                center_y_nm = int(center_y * scale)
                corner_radius_nm = int(corner_radius * scale)

                # Create rounded rectangle
                self._add_rounded_rect(
                    center_x_nm,
                    center_y_nm,
                    width_nm,
                    height_nm,
                    corner_radius_nm,
                    edge_layer,
                )

            elif shape == "circle":
                if radius is None:
                    return {
                        "success": False,
                        "message": "Missing radius",
                        "errorDetails": "Radius is required for circle",
                    }

                center_x_nm = int(center_x * scale)
                center_y_nm = int(center_y * scale)
                radius_nm = int(radius * scale)

                # Create circle
                circle = pcbnew.PCB_SHAPE(self.board)
                circle.SetShape(pcbnew.SHAPE_T_CIRCLE)
                circle.SetCenter(pcbnew.VECTOR2I(center_x_nm, center_y_nm))
                circle.SetEnd(pcbnew.VECTOR2I(center_x_nm + radius_nm, center_y_nm))
                circle.SetLayer(edge_layer)
                circle.SetWidth(0)  # Zero width for edge cuts
                self.board.Add(circle)

            elif shape == "polygon":
                if not points or len(points) < 3:
                    return {
                        "success": False,
                        "message": "Missing points",
                        "errorDetails": "At least 3 points are required for polygon",
                    }

                # Convert points to nm
                polygon_points = []
                for point in points:
                    x_nm = int(point["x"] * scale)
                    y_nm = int(point["y"] * scale)
                    polygon_points.append(pcbnew.VECTOR2I(x_nm, y_nm))

                # Add lines for polygon
                for i in range(len(polygon_points)):
                    self._add_edge_line(
                        polygon_points[i],
                        polygon_points[(i + 1) % len(polygon_points)],
                        edge_layer,
                    )

            return {
                "success": True,
                "message": f"Added board outline: {shape}",
                "outline": {
                    "shape": shape,
                    "width": width,
                    "height": height,
                    "center": {"x": center_x, "y": center_y, "unit": unit},
                    "radius": radius,
                    "cornerRadius": corner_radius,
                    "points": points,
                },
            }

        except Exception as e:
            logger.error(f"Error adding board outline: {str(e)}")
            return failed("Failed to add board outline", e)

    def add_mounting_hole(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add a mounting hole to the PCB"""
        try:
            if not self.board:
                return no_board_loaded()

            position = params.get("position")
            diameter = params.get("diameter")
            pad_diameter = params.get("padDiameter")
            plated = params.get("plated", False)
            footprint_lib_id = params.get("footprintLibId")

            if not position or not diameter:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "position and diameter are required",
                }

            # Convert to internal units (nanometers)
            scale = unit_to_nm_scale(position.get("unit", "mm"))
            x_nm = int(position["x"] * scale)
            y_nm = int(position["y"] * scale)
            diameter_nm = int(diameter * scale)
            if pad_diameter:
                pad_diameter_nm = int(pad_diameter * scale)
            elif plated:
                # Plated hole: default to a ~1 mm annular copper ring so there
                # is copper to solder / ground to.
                pad_diameter_nm = diameter_nm + scale
            else:
                # Bare NPTH mounting hole, KiCad-library convention: pad size ==
                # hole size — no annular ring and no oversized mask aperture.
                # (The old default padded +1 mm, leaving a copper-less mask ring
                # that reads as a footprint anomaly to DRC's lib checks.)
                pad_diameter_nm = diameter_nm

            # Create footprint for mounting hole with unique reference
            existing_mh = [
                fp.GetReference()
                for fp in self.board.GetFootprints()
                if fp.GetReference().startswith("MH")
            ]
            next_num = 1
            while f"MH{next_num}" in existing_mh:
                next_num += 1

            module = pcbnew.FOOTPRINT(self.board)
            module.SetReference(f"MH{next_num}")
            module.SetValue(f"MountingHole_{diameter}mm")

            # Set a real library:name FPID. Without this, the footprint is
            # written as `(footprint "" ...)` and KiCad's GUI Move tool refuses
            # to select it (no library link → not draggable in the editor).
            if not footprint_lib_id:
                # Resolve to a footprint name that ACTUALLY exists in the stock
                # MountingHole lib — the bare "MountingHole_<d>mm" is absent for
                # several common sizes (3.2 / 4.3 / 5.3 / 6.4 ship only as
                # …_M3 / _M4 / …), which otherwise trips DRC's lib_footprint_issues.
                resolved = _resolve_mountinghole_footprint(diameter)
                if resolved:
                    footprint_lib_id = f"MountingHole:{resolved}"
                else:
                    # Stock lib not found at all → keep the legacy synthetic name.
                    # Strip trailing zeros so 3.2 → "3.2" not "3.20".
                    footprint_lib_id = f"MountingHole:MountingHole_{diameter:g}mm"
            if ":" in footprint_lib_id:
                lib_name, fp_name = footprint_lib_id.split(":", 1)
            else:
                lib_name = "MountingHole"
                fp_name = footprint_lib_id
            module.SetFPID(pcbnew.LIB_ID(lib_name, fp_name))

            # Create the pad for the hole
            pad = pcbnew.PAD(module)
            pad.SetNumber(1)
            pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
            pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH if plated else pcbnew.PAD_ATTRIB_NPTH)
            pad.SetSize(pcbnew.VECTOR2I(pad_diameter_nm, pad_diameter_nm))
            pad.SetDrillSize(pcbnew.VECTOR2I(diameter_nm, diameter_nm))
            pad.SetPosition(pcbnew.VECTOR2I(0, 0))  # Position relative to module

            if not plated:
                # NPTH must not include *.Cu in pad layers. The default LSET
                # for a circular pad is *.Cu + *.Mask; on a NPTH with
                # padDiameter > diameter that produces phantom copper annular
                # rings on every Cu layer, which trip clearance DRC against
                # neighbouring nets.
                mask_only = pcbnew.LSET()
                mask_only.AddLayer(pcbnew.F_Mask)
                mask_only.AddLayer(pcbnew.B_Mask)
                pad.SetLayerSet(mask_only)

            module.Add(pad)

            # F.Courtyard keepout circle (KiCad-library convention). Without a
            # courtyard the footprint is (a) invisible to check_courtyard_overlaps'
            # exact-polygon path, forcing the text-inflated bbox fallback, and
            # (b) flagged by KiCad DRC's missing-courtyard / lib_footprint_issues
            # checks. Radius = the larger of hole/pad radius + a 0.25 mm margin,
            # drawn with the standard 0.05 mm courtyard line. The margin/line
            # widths are always in mm, independent of the position unit.
            nm_per_mm = 1_000_000
            courtyard_radius_nm = max(pad_diameter_nm, diameter_nm) // 2 + int(0.25 * nm_per_mm)
            courtyard = pcbnew.PCB_SHAPE(module)
            courtyard.SetShape(pcbnew.SHAPE_T_CIRCLE)
            courtyard.SetLayer(pcbnew.F_CrtYd)
            courtyard.SetCenter(pcbnew.VECTOR2I(0, 0))
            courtyard.SetStart(pcbnew.VECTOR2I(0, 0))
            courtyard.SetEnd(pcbnew.VECTOR2I(courtyard_radius_nm, 0))
            courtyard.SetWidth(int(0.05 * nm_per_mm))
            module.Add(courtyard)

            # F.Fab hole outline (radius = hole radius, 0.1 mm line) — mirrors
            # the fabrication marker on KiCad's own MountingHole footprints.
            fab = pcbnew.PCB_SHAPE(module)
            fab.SetShape(pcbnew.SHAPE_T_CIRCLE)
            fab.SetLayer(pcbnew.F_Fab)
            fab.SetCenter(pcbnew.VECTOR2I(0, 0))
            fab.SetStart(pcbnew.VECTOR2I(0, 0))
            fab.SetEnd(pcbnew.VECTOR2I(diameter_nm // 2, 0))
            fab.SetWidth(int(0.1 * nm_per_mm))
            module.Add(fab)

            # Position the mounting hole
            module.SetPosition(pcbnew.VECTOR2I(x_nm, y_nm))

            # Add to board
            self.board.Add(module)

            return {
                "success": True,
                "message": "Added mounting hole",
                "mountingHole": {
                    "position": position,
                    "diameter": diameter,
                    "padDiameter": round(pad_diameter_nm / scale, 4),
                    "plated": plated,
                    "footprintLibId": f"{lib_name}:{fp_name}",
                    "courtyard": True,
                },
            }

        except Exception as e:
            logger.error(f"Error adding mounting hole: {str(e)}")
            return failed("Failed to add mounting hole", e)

    def add_text(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add text annotation to the PCB"""
        try:
            if not self.board:
                return no_board_loaded()

            text = params.get("text")
            position = params.get("position")
            layer = params.get("layer", "F.SilkS")
            size = params.get("size", 1.0)
            thickness = params.get("thickness", 0.15)
            rotation = params.get("rotation", 0)

            # Auto-mirror back-layer text (P13, KiCad convention): silkscreen (or
            # any copper/fab) text on a B.* layer is read through the board, so it
            # must be mirrored — un-mirrored back text trips DRC's
            # ``nonmirrored_text_on_back_layer``.  Default: mirror any B.* layer.
            # An explicit ``mirror`` boolean overrides in either direction.
            mirror_param = params.get("mirror")
            mirror_auto = mirror_param is None
            is_back_layer = str(layer).startswith("B.")
            mirror = is_back_layer if mirror_auto else bool(mirror_param)

            if not text or not position:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "text and position are required",
                }

            # Convert to internal units (nanometers)
            scale = unit_to_nm_scale(position.get("unit", "mm"))
            x_nm = int(position["x"] * scale)
            y_nm = int(position["y"] * scale)
            size_nm = int(size * scale)
            thickness_nm = int(thickness * scale)

            # Get layer ID
            layer_id = self.board.GetLayerID(layer)
            if layer_id < 0:
                return {
                    "success": False,
                    "message": "Invalid layer",
                    "errorDetails": f"Layer '{layer}' does not exist",
                }

            # Create text
            pcb_text = pcbnew.PCB_TEXT(self.board)
            pcb_text.SetText(text)
            pcb_text.SetPosition(pcbnew.VECTOR2I(x_nm, y_nm))
            pcb_text.SetLayer(layer_id)
            pcb_text.SetTextSize(pcbnew.VECTOR2I(size_nm, size_nm))
            pcb_text.SetTextThickness(thickness_nm)

            # Set rotation angle - KiCAD 9.0 uses EDA_ANGLE
            try:
                # Try KiCAD 9.0+ API (EDA_ANGLE)
                angle = pcbnew.EDA_ANGLE(rotation, pcbnew.DEGREES_T)
                pcb_text.SetTextAngle(angle)
            except (AttributeError, TypeError):
                # Fall back to older API (decidegrees as integer)
                pcb_text.SetTextAngle(int(rotation * 10))

            pcb_text.SetMirrored(mirror)

            # Add to board
            self.board.Add(pcb_text)

            return {
                "success": True,
                "message": "Added text annotation",
                "text": {
                    "text": text,
                    "position": position,
                    "layer": layer,
                    "size": size,
                    "thickness": thickness,
                    "rotation": rotation,
                    "mirror": mirror,
                    # Tell the caller whether the mirror state was auto-applied
                    # (back-layer convention) or came from an explicit override.
                    "mirrorAuto": mirror_auto and is_back_layer,
                },
            }

        except Exception as e:
            logger.error(f"Error adding text: {str(e)}")
            return failed("Failed to add text", e)

    def _add_edge_line(self, start: pcbnew.VECTOR2I, end: pcbnew.VECTOR2I, layer: int) -> None:
        """Add a line to the edge cuts layer"""
        line = pcbnew.PCB_SHAPE(self.board)
        line.SetShape(pcbnew.SHAPE_T_SEGMENT)
        line.SetStart(start)
        line.SetEnd(end)
        line.SetLayer(layer)
        line.SetWidth(0)  # Zero width for edge cuts
        self.board.Add(line)

    def _add_rounded_rect(
        self,
        center_x_nm: int,
        center_y_nm: int,
        width_nm: int,
        height_nm: int,
        radius_nm: int,
        layer: int,
    ) -> None:
        """Add a rounded rectangle to the edge cuts layer"""
        if radius_nm <= 0:
            # If no radius, create regular rectangle
            top_left = pcbnew.VECTOR2I(center_x_nm - width_nm // 2, center_y_nm - height_nm // 2)
            top_right = pcbnew.VECTOR2I(center_x_nm + width_nm // 2, center_y_nm - height_nm // 2)
            bottom_right = pcbnew.VECTOR2I(
                center_x_nm + width_nm // 2, center_y_nm + height_nm // 2
            )
            bottom_left = pcbnew.VECTOR2I(center_x_nm - width_nm // 2, center_y_nm + height_nm // 2)

            self._add_edge_line(top_left, top_right, layer)
            self._add_edge_line(top_right, bottom_right, layer)
            self._add_edge_line(bottom_right, bottom_left, layer)
            self._add_edge_line(bottom_left, top_left, layer)
            return

        # Calculate corner centers
        half_width = width_nm // 2
        half_height = height_nm // 2

        # Ensure radius is not larger than half the smallest dimension
        max_radius = min(half_width, half_height)
        if radius_nm > max_radius:
            radius_nm = max_radius

        # Calculate corner centers
        top_left_center = pcbnew.VECTOR2I(
            center_x_nm - half_width + radius_nm, center_y_nm - half_height + radius_nm
        )
        top_right_center = pcbnew.VECTOR2I(
            center_x_nm + half_width - radius_nm, center_y_nm - half_height + radius_nm
        )
        bottom_right_center = pcbnew.VECTOR2I(
            center_x_nm + half_width - radius_nm, center_y_nm + half_height - radius_nm
        )
        bottom_left_center = pcbnew.VECTOR2I(
            center_x_nm - half_width + radius_nm, center_y_nm + half_height - radius_nm
        )

        # Add arcs for corners
        self._add_corner_arc(top_left_center, radius_nm, 180, 270, layer)
        self._add_corner_arc(top_right_center, radius_nm, 270, 0, layer)
        self._add_corner_arc(bottom_right_center, radius_nm, 0, 90, layer)
        self._add_corner_arc(bottom_left_center, radius_nm, 90, 180, layer)

        # Add lines for straight edges
        # Top edge
        self._add_edge_line(
            pcbnew.VECTOR2I(top_left_center.x, top_left_center.y - radius_nm),
            pcbnew.VECTOR2I(top_right_center.x, top_right_center.y - radius_nm),
            layer,
        )
        # Right edge
        self._add_edge_line(
            pcbnew.VECTOR2I(top_right_center.x + radius_nm, top_right_center.y),
            pcbnew.VECTOR2I(bottom_right_center.x + radius_nm, bottom_right_center.y),
            layer,
        )
        # Bottom edge
        self._add_edge_line(
            pcbnew.VECTOR2I(bottom_right_center.x, bottom_right_center.y + radius_nm),
            pcbnew.VECTOR2I(bottom_left_center.x, bottom_left_center.y + radius_nm),
            layer,
        )
        # Left edge
        self._add_edge_line(
            pcbnew.VECTOR2I(bottom_left_center.x - radius_nm, bottom_left_center.y),
            pcbnew.VECTOR2I(top_left_center.x - radius_nm, top_left_center.y),
            layer,
        )

    def _add_corner_arc(
        self,
        center: pcbnew.VECTOR2I,
        radius: int,
        start_angle: float,
        end_angle: float,
        layer: int,
    ) -> None:
        """Add an arc for a rounded corner"""
        # Create arc for corner
        arc = pcbnew.PCB_SHAPE(self.board)
        arc.SetShape(pcbnew.SHAPE_T_ARC)
        arc.SetCenter(center)

        # Calculate start and end points
        start_x = center.x + int(radius * math.cos(math.radians(start_angle)))
        start_y = center.y + int(radius * math.sin(math.radians(start_angle)))
        end_x = center.x + int(radius * math.cos(math.radians(end_angle)))
        end_y = center.y + int(radius * math.sin(math.radians(end_angle)))

        arc.SetStart(pcbnew.VECTOR2I(start_x, start_y))
        arc.SetEnd(pcbnew.VECTOR2I(end_x, end_y))
        arc.SetLayer(layer)
        arc.SetWidth(0)  # Zero width for edge cuts
        self.board.Add(arc)
