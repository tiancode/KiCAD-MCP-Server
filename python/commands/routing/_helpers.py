"""Module-level pure helpers for the routing commands.

Split out of the former monolithic commands/routing.py so the mixin
modules can share them without a circular import.
"""

from typing import Any, Dict, List, Optional

# Sane upper bound (mm) for any user-supplied copper width — trace width or a
# net class's trace width.  A track wider than this is almost certainly a
# fat-fingered value (999 mm was seen in the wild, wider than the whole board);
# reject it with a clear, unit-named message instead of silently creating a
# giant copper slab.  Generous on purpose so legitimate power/bus widths pass.
MAX_TRACK_WIDTH_MM = 50.0


def _track_width_error(width: Any, field: str = "width") -> Optional[Dict[str, Any]]:
    """Validate a user-supplied track width (mm); return a refusal dict or None.

    Bounds are ``0 < width <= MAX_TRACK_WIDTH_MM``.  ``None`` is treated as
    "not supplied" (callers only validate an explicitly-passed width) and
    passes.  Non-numeric, non-positive, or over-cap values are refused with a
    truthful ``VALIDATION`` errorCode and a message naming the limit and unit.
    Shared by route_trace, route_smart (explicit width) and create_netclass
    (traceWidth) so the bound is identical everywhere a width is accepted.
    """
    if width is None:
        return None
    try:
        w = float(width)
    except (TypeError, ValueError):
        return {
            "success": False,
            "message": f"{field} must be a number in mm",
            "errorCode": "VALIDATION",
        }
    if w <= 0:
        return {
            "success": False,
            "message": f"{field} must be greater than 0 mm (got {w:g} mm)",
            "errorCode": "VALIDATION",
        }
    if w > MAX_TRACK_WIDTH_MM:
        return {
            "success": False,
            "message": (
                f"{field} of {w:g} mm is out of range — the maximum allowed is "
                f"{MAX_TRACK_WIDTH_MM:g} mm. Pass a width in mm within "
                f"(0, {MAX_TRACK_WIDTH_MM:g}]."
            ),
            "errorCode": "VALIDATION",
        }
    return None


def _refuse_with_obstacles(
    from_ref: str,
    from_pad: str,
    to_ref: str,
    to_pad: str,
    obstacles: List[str],
) -> Dict[str, Any]:
    """Refusal response for ``route_pad_to_pad`` when a straight segment
    would cross a third-party pad.

    Surfaced as ``success: False`` with ``hasObstacles: True`` so the
    agent can distinguish this recoverable, geometry-only failure from
    an "actually broken" error.  Carries the obstacle list and a
    pointer to the ``force`` opt-out so the caller can either reroute
    manually or override knowing the cost (DRC violations).
    """
    return {
        "success": False,
        "hasObstacles": True,
        # Truthful code: this is a deliberate geometry refusal (the straight
        # trace would short through other pads), not an internal error — so an
        # agent can branch on SHORT_REFUSED and offer force=true / manual reroute.
        "errorCode": "SHORT_REFUSED",
        "obstacleCount": len(obstacles),
        "obstaclesCrossed": obstacles,
        "message": (
            f"Refused: straight trace from {from_ref}.{from_pad} → "
            f"{to_ref}.{to_pad} crosses {len(obstacles)} other pad(s). "
            "Inserting it would short the trace through them and produce "
            "tracks_crossing / net-shorting DRC violations."
        ),
        "hint": (
            "route_pad_to_pad is a straight-line connector, not an "
            "autorouter — it has no obstacle avoidance.  Either plan the "
            "path manually as several route_trace segments that go around "
            "the obstacles, or call again with force=true to insert "
            "anyway (you will then need to fix the resulting DRC errors)."
        ),
    }


def _point_to_segment_distance_nm(px: int, py: int, x1: int, y1: int, x2: int, y2: int) -> float:
    """Shortest distance (nm) from point (px,py) to segment (x1,y1)-(x2,y2).

    Pure integer-friendly variant of the standard projection formula;
    used in the hot loop of GND-stitching collision detection so we
    avoid building VECTOR2I objects per call.
    """
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        ex: float = px - x1
        ey: float = py - y1
        return (ex * ex + ey * ey) ** 0.5
    denom = dx * dx + dy * dy
    t = ((px - x1) * dx + (py - y1) * dy) / denom
    if t < 0:
        t = 0
    elif t > 1:
        t = 1
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    ex = px - proj_x
    ey = py - proj_y
    return (ex * ex + ey * ey) ** 0.5
