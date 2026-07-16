"""Shared helpers for the component command mixins.

Small, pure-ish helpers reused across the per-area mixins so identical
board-lookup and bounding-box construction don't drift between commands.
"""

from typing import Any, Dict, Optional, Tuple


def resolve_footprint(
    board: Any, params: Dict[str, Any]
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """Resolve the ``reference`` param to a footprint on ``board``.

    Returns ``(module, None)`` on success, or ``(None, error_response)`` when
    the ``reference`` param is missing or no footprint matches — the error dict
    is the exact structured response the callers returned inline.
    """
    reference = params.get("reference")
    if not reference:
        return None, {
            "success": False,
            "message": "Missing reference",
            "errorDetails": "reference parameter is required",
        }

    module = board.FindFootprintByReference(reference)
    if not module:
        return None, {
            "success": False,
            "message": "Component not found",
            "errorDetails": f"Could not find component: {reference}",
        }

    return module, None


def bbox_to_dict(box: Any) -> Dict[str, Any]:
    """Convert a pcbnew bounding box to the mm-unit dict the API returns.

    Works for any object exposing ``GetLeft/GetTop/GetRight/GetBottom`` (both
    ``GetBoundingBox()`` boxes and courtyard ``BBox()`` boxes).
    """
    return {
        "min_x": box.GetLeft() / 1000000,
        "min_y": box.GetTop() / 1000000,
        "max_x": box.GetRight() / 1000000,
        "max_y": box.GetBottom() / 1000000,
        "width": (box.GetRight() - box.GetLeft()) / 1000000,
        "height": (box.GetBottom() - box.GetTop()) / 1000000,
        "unit": "mm",
    }
