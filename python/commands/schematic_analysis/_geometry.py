"""Symbol bbox / pin-position / AABB geometry helpers.

Split out of the former monolithic commands/schematic_analysis.py.
"""

import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from commands.pin_locator import PinLocator

logger = logging.getLogger("kicad_interface")


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def compute_symbol_bbox(
    schematic_path: Path,
    reference: str,
    locator: PinLocator,
) -> Optional[Tuple[float, float, float, float]]:
    """
    Compute bounding box of a symbol from its pin positions.

    Returns (min_x, min_y, max_x, max_y) in mm, or None if no pins found.
    """
    pins = locator.get_all_symbol_pins(schematic_path, reference)
    if not pins:
        return None
    xs = [p[0] for p in pins.values()]
    ys = [p[1] for p in pins.values()]
    return (min(xs), min(ys), max(xs), max(ys))


def _line_segment_intersects_aabb(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    box_min_x: float,
    box_min_y: float,
    box_max_x: float,
    box_max_y: float,
) -> bool:
    """
    Test whether line segment (x1,y1)→(x2,y2) intersects an axis-aligned bounding box.

    Uses the Liang-Barsky clipping algorithm.
    """
    dx = x2 - x1
    dy = y2 - y1

    p = [-dx, dx, -dy, dy]
    q = [x1 - box_min_x, box_max_x - x1, y1 - box_min_y, box_max_y - y1]

    t_min = 0.0
    t_max = 1.0

    for i in range(4):
        if abs(p[i]) < 1e-12:
            # Parallel to this edge
            if q[i] < 0:
                return False
        else:
            t = q[i] / p[i]
            if p[i] < 0:
                t_min = max(t_min, t)
            else:
                t_max = min(t_max, t)
            if t_min > t_max:
                return False

    return True


def _point_in_rect(
    px: float,
    py: float,
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
) -> bool:
    """Check if a point is within a rectangle."""
    return min_x <= px <= max_x and min_y <= py <= max_y


def _distance(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    """Euclidean distance between two points."""
    return math.sqrt((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2)


def _aabb_overlap(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
) -> bool:
    """Check if two axis-aligned bounding boxes overlap.

    Each bbox is (min_x, min_y, max_x, max_y).
    """
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _transform_local_point(
    lx: float,
    ly: float,
    sym_x: float,
    sym_y: float,
    rotation: float,
    mirror_x: bool,
    mirror_y: bool,
) -> Tuple[float, float]:
    """
    Transform a point from local symbol coordinates to absolute schematic
    coordinates using KiCad's transform order:
    negate-y (lib y-up → schematic y-down) → mirror → rotate → translate.
    """
    # Library symbols use y-up; schematic uses y-down
    ly = -ly

    # Apply mirroring in local coords
    if mirror_x:
        ly = -ly
    if mirror_y:
        lx = -lx

    # Apply rotation
    if rotation != 0:
        lx, ly = PinLocator.rotate_point(lx, ly, rotation)

    return (sym_x + lx, sym_y + ly)


def _compute_symbol_bbox_direct(
    sym: Dict[str, Any],
    pin_defs: Dict[str, Dict],
    margin: float = 0.0,
    graphics_points: Optional[List[Tuple[float, float]]] = None,
    graphics_by_unit: Optional[Dict[int, List[Tuple[float, float]]]] = None,
) -> Optional[Tuple[float, float, float, float]]:
    """
    Compute bounding box of a symbol from its graphics and pin definitions.

    When graphics_points are available (from lib_symbol body shapes), uses
    those for the bbox and unions with pin positions. Falls back to
    pin-only estimation with degenerate expansion when no graphics data
    is available.

    Args:
        sym: Parsed symbol dict with x, y, rotation, mirror_x, mirror_y, unit.
        pin_defs: Pin definitions from PinLocator.get_symbol_pins().
        margin: Shrink bbox by this amount on each side (mm).
        graphics_points: Local-coordinate points from symbol body graphics
            (all units — legacy path).
        graphics_by_unit: Local-coordinate body points keyed by unit. When
            provided, only unit 0 (common) plus THIS instance's ``sym["unit"]``
            are used, so a multi-unit part's box no longer includes another
            unit's body drawn at the shared origin (F4). Overrides
            ``graphics_points``.

    Returns (min_x, min_y, max_x, max_y) in mm, or None if no pins.
    """
    pin_positions = _compute_pin_positions_direct(sym, pin_defs)
    if not pin_positions:
        return None

    if graphics_by_unit is not None:
        sym_unit = sym.get("unit", 1)
        graphics_points = list(graphics_by_unit.get(0, [])) + list(
            graphics_by_unit.get(sym_unit, [])
        )

    if graphics_points:
        # Transform graphics points to absolute coordinates
        sym_x, sym_y = sym["x"], sym["y"]
        rotation = sym["rotation"]
        mirror_x = sym.get("mirror_x", False)
        mirror_y = sym.get("mirror_y", False)

        abs_points = [
            _transform_local_point(lx, ly, sym_x, sym_y, rotation, mirror_x, mirror_y)
            for lx, ly in graphics_points
        ]

        # Union with pin positions so pins extending beyond body are included
        all_xs = [p[0] for p in abs_points] + [p[0] for p in pin_positions.values()]
        all_ys = [p[1] for p in abs_points] + [p[1] for p in pin_positions.values()]

        min_x, min_y = min(all_xs), min(all_ys)
        max_x, max_y = max(all_xs), max(all_ys)
    else:
        # Fallback: pin-only estimation with degenerate expansion
        xs = [p[0] for p in pin_positions.values()]
        ys = [p[1] for p in pin_positions.values()]
        min_x, min_y, max_x, max_y = min(xs), min(ys), max(xs), max(ys)

        min_body = 1.5  # mm minimum half-extent for component body
        if max_x - min_x < 2 * min_body:
            cx = (min_x + max_x) / 2
            min_x = cx - min_body
            max_x = cx + min_body
        if max_y - min_y < 2 * min_body:
            cy = (min_y + max_y) / 2
            min_y = cy - min_body
            max_y = cy + min_body

    # Shrink bbox by margin
    min_x += margin
    min_y += margin
    max_x -= margin
    max_y -= margin

    # Skip degenerate bboxes
    if max_x <= min_x or max_y <= min_y:
        return None

    return (min_x, min_y, max_x, max_y)


# ---------------------------------------------------------------------------
# Pin-position helpers
# ---------------------------------------------------------------------------


def _compute_pin_positions_direct(
    sym: Dict[str, Any], pin_defs: Dict[str, Dict]
) -> Dict[str, List[float]]:
    """
    Compute absolute schematic pin positions for a symbol instance directly from
    its parsed position/rotation/mirror data and pin definitions in local coords.

    Unlike PinLocator.get_all_symbol_pins, this does NOT do a reference-name
    lookup in the schematic, so it works correctly when multiple symbols share
    the same reference designator (e.g. unannotated "Q?").

    For a multi-unit part each unit is a separate instance; a pin is included
    only when it belongs to unit 0 (common) or this instance's ``sym["unit"]``,
    so unit B's pins — drawn at the shared library origin — don't get attributed
    to unit A's instance (F4). Pins with no unit tag are always included.

    KiCad transform order: mirror (in local coords) → rotate → translate.
    """
    sym_x = sym["x"]
    sym_y = sym["y"]
    rotation = sym["rotation"]
    mirror_x = sym.get("mirror_x", False)
    mirror_y = sym.get("mirror_y", False)
    sym_unit = sym.get("unit", 1)

    result: Dict[str, List[float]] = {}
    for pin_num, pin_data in pin_defs.items():
        pin_unit = pin_data.get("unit")
        if pin_unit not in (None, 0) and pin_unit != sym_unit:
            continue  # belongs to a different unit's instance
        rel_x = float(pin_data["x"])
        rel_y = float(pin_data["y"])

        # Apply mirroring in local symbol coordinates
        if mirror_x:
            rel_y = -rel_y
        if mirror_y:
            rel_x = -rel_x

        # Apply symbol rotation
        if rotation != 0:
            rel_x, rel_y = PinLocator.rotate_point(rel_x, rel_y, rotation)

        result[pin_num] = [sym_x + rel_x, sym_y + rel_y]
    return result
