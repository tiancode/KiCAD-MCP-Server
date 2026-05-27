"""
Board metadata handlers: origins + title block (IPC-only).

These cover board-level state that Gerber / drill / pick-and-place
exports key off of (drill/aux origin) and the documentation block KiCad
writes into every fabrication PDF (title / company / revision / date /
9 comment slots).

All commands require KiCAD running with the IPC API server enabled.
SWIG has equivalents but exposes them through a different surface;
keeping these IPC-only means the change is live in the UI instantly
and uses kipy's stable API.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Union

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


def _ipc_unavailable(reason: str = "") -> Dict[str, Any]:
    base = (
        "Board metadata commands require the IPC backend. Launch KiCAD "
        "with Preferences > Plugins > Enable IPC API Server, then retry."
    )
    return {
        "success": False,
        "message": f"{base} ({reason})" if reason else base,
    }


def _require_ipc(iface: "KiCADInterface") -> Dict[str, Any]:
    if iface.use_ipc and iface.ipc_board_api:
        return {}
    ok, reason = iface.ensure_ipc(allow_launch=True)
    if ok:
        return {}
    return _ipc_unavailable(reason)


def _xy_from_params(params: Dict[str, Any]) -> tuple:
    """Pull (x, y, unit) from either ``{position: {x,y,unit}}`` or flat top-level.

    The caller is responsible for ensuring coordinates were actually
    provided — use :func:`_has_coords` first.  This helper still defaults
    to ``(0, 0, "mm")`` when nothing is set so it can't raise; that
    fallback is only safe after a presence check upstream.
    """
    nested = params.get("position")
    if isinstance(nested, dict):
        return (
            float(nested.get("x", 0)),
            float(nested.get("y", 0)),
            str(nested.get("unit", "mm")),
        )
    return (
        float(params.get("x", 0)),
        float(params.get("y", 0)),
        str(params.get("unit", "mm")),
    )


def _has_coords(params: Dict[str, Any]) -> bool:
    """True when the caller supplied a coordinate via either form.

    Accepts ``{"position": {"x": _, "y": _}}`` (both keys present) or
    flat ``{"x": _, "y": _}`` (both at top level).  Partial coords —
    only x, only y, position dict missing x/y — return False so the
    handler can reject rather than silently filling in 0.
    """
    pos = params.get("position")
    if isinstance(pos, dict) and "x" in pos and "y" in pos:
        return True
    return "x" in params and "y" in params


def handle_get_origin(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Return the requested board origin.

    Params:
        type: "grid" | "drill" (default "drill" — the one Gerber / PnP use)
        unit: "mm" | "inch" (default "mm")
    """
    gate = _require_ipc(iface)
    if gate:
        return gate
    origin_type = str(params.get("type", "drill"))
    unit = str(params.get("unit", "mm"))
    return iface.ipc_board_api.get_origin(origin_type=origin_type, unit=unit)


def handle_set_origin(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Set the grid or drill/place origin to ``(x, y)``.

    Params:
        type: "grid" | "drill" (required)
        position: {x, y, unit?} OR flat x/y/unit (one form required)

    Refuses calls without coordinates — silently snapping to (0, 0) on
    a missing-arg mistake would invalidate every Gerber/PnP the user
    exports next.
    """
    gate = _require_ipc(iface)
    if gate:
        return gate
    origin_type = params.get("type")
    if not isinstance(origin_type, str) or not origin_type:
        return {
            "success": False,
            "message": "'type' parameter is required: 'grid' or 'drill'",
        }
    if not _has_coords(params):
        return {
            "success": False,
            "message": (
                "set_origin requires a coordinate: pass `position: {x, y}` "
                "or both flat `x` and `y` parameters."
            ),
        }
    x, y, unit = _xy_from_params(params)
    return iface.ipc_board_api.set_origin(origin_type=origin_type, x=x, y=y, unit=unit)


def handle_get_title_block_info(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Return the board title block (title / company / revision / date /
    comments dict keyed '1'..'9')."""
    gate = _require_ipc(iface)
    if gate:
        return gate
    return iface.ipc_board_api.get_title_block_info()


def _normalize_comments(
    raw: Union[None, Dict[Any, Any], List[Any]],
) -> Dict[int, str]:
    """Accept either a dict ``{1: "...", "5": "..."}`` or a list
    ``["...", "..."]`` (positional, index 0 → slot 1).  Returns a clean
    ``{int slot: str text}`` dict that ``set_title_block_info`` can merge.

    Null / ``None`` values are treated as "no change" (skipped) — the
    public contract is that explicit empty string clears a slot.  If
    null also cleared, JSON encoders that emit ``null`` for "unset"
    would silently wipe slots the caller never meant to touch.

    Out-of-range slots are dropped (the API layer logs a warning).
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        result: Dict[int, str] = {}
        for k, v in raw.items():
            if v is None:
                continue  # null = no change, not clear
            try:
                slot = int(k)
            except (TypeError, ValueError):
                continue
            result[slot] = str(v)
        return result
    if isinstance(raw, list):
        return {i + 1: str(v) for i, v in enumerate(raw) if v is not None}
    return {}


def handle_set_title_block_info(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Partial-update the board title block.

    Any of ``title / date / revision / company / comments`` may be omitted;
    omitted fields keep their current value.  ``comments`` accepts either:
      - a dict ``{1: "text", "5": "more"}`` — only listed slots overwritten
      - a list ``["a", "b"]`` — positional, index 0 → slot 1

    Pass an explicit empty string to *clear* a field/slot.  Null / JSON
    ``null`` is treated as "leave unchanged" — same as omitting the key.
    """
    gate = _require_ipc(iface)
    if gate:
        return gate
    comments = _normalize_comments(params.get("comments"))
    return iface.ipc_board_api.set_title_block_info(
        title=params.get("title"),
        date=params.get("date"),
        revision=params.get("revision"),
        company=params.get("company"),
        comments=comments if comments else None,
    )
