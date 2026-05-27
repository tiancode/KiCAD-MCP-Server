"""
Selection / interaction handlers (IPC-only).

These wrap KiCad's IPC selection API and let the AI drive what's selected
in the running editor — useful for "show me what you're about to change"
handoffs and for ``interactive_move`` which puts the cursor on an item
and lets the user finish positioning by hand.

All commands here need a running KiCAD with the IPC API server enabled.
SWIG has no equivalent.

Identification: most commands accept items by either ``ids`` (KIID
strings — preferred) OR ``references`` (footprint reference designators
like ``["R1", "U2"]``).  The resolver below converts references to IDs
by scanning live footprints once, so callers don't have to round-trip.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


def _ipc_unavailable(reason: str = "") -> Dict[str, Any]:
    base = (
        "Selection commands require the IPC backend. Launch KiCAD with "
        "Preferences > Plugins > Enable IPC API Server, then retry."
    )
    return {"success": False, "message": f"{base} ({reason})" if reason else base}


def _require_ipc(iface: "KiCADInterface") -> Dict[str, Any]:
    """Gate selection ops on IPC + an open PCB editor frame."""
    gate = iface.require_ipc_board_op(allow_launch=True)
    if not gate:
        return {}
    if gate.get("needs_pcb_editor"):
        return gate
    return _ipc_unavailable(gate.get("_ipc_reason", ""))


def _resolve_ids(iface: "KiCADInterface", params: Dict[str, Any]) -> List[str]:
    """Combine ``ids`` and ``references`` from params into one ID list.

    ``ids`` pass through verbatim.  ``references`` (footprint reference
    designators) are looked up against the live board's footprints — any
    that don't match are silently dropped so a partial hit still works;
    the handler reports back ``resolved`` vs ``requested`` counts.
    """
    raw_ids = params.get("ids") or []
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    out = [str(x) for x in raw_ids if x]

    refs = params.get("references") or []
    if isinstance(refs, str):
        refs = [refs]
    if refs:
        try:
            board = iface.ipc_board_api._get_board()  # type: ignore[union-attr]
            wanted = {str(r) for r in refs}
            for fp in board.get_footprints():
                try:
                    rf = fp.reference_field
                    if rf and rf.text.value in wanted:
                        out.append(str(fp.id))
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"Reference resolution failed: {e}")
    # Preserve first-seen order while deduplicating.
    seen = set()
    deduped: List[str] = []
    for item_id in out:
        if item_id not in seen:
            seen.add(item_id)
            deduped.append(item_id)
    return deduped


def handle_get_selection(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Return whatever is currently selected in the KiCAD board editor."""
    gate = _require_ipc(iface)
    if gate:
        return gate
    try:
        items = iface.ipc_board_api.get_selection()
        return {"success": True, "items": items, "count": len(items)}
    except Exception as e:
        logger.error(f"get_selection failed: {e}")
        return {"success": False, "message": str(e)}


def handle_clear_selection(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Deselect everything in the editor."""
    gate = _require_ipc(iface)
    if gate:
        return gate
    try:
        ok = iface.ipc_board_api.clear_selection()
        return {"success": bool(ok), "message": "Selection cleared" if ok else "Failed"}
    except Exception as e:
        logger.error(f"clear_selection failed: {e}")
        return {"success": False, "message": str(e)}


def handle_add_to_selection(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Add items to the selection by KIID and/or footprint reference."""
    gate = _require_ipc(iface)
    if gate:
        return gate
    ids = _resolve_ids(iface, params)
    if not ids:
        return {
            "success": False,
            "message": "Provide at least one item via 'ids' or 'references'.",
        }
    return iface.ipc_board_api.add_to_selection(ids)


def handle_remove_from_selection(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Remove items from the selection by KIID and/or footprint reference."""
    gate = _require_ipc(iface)
    if gate:
        return gate
    ids = _resolve_ids(iface, params)
    if not ids:
        return {
            "success": False,
            "message": "Provide at least one item via 'ids' or 'references'.",
        }
    return iface.ipc_board_api.remove_from_selection(ids)


def handle_hit_test(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Return board items underneath a coordinate.

    Forms accepted (in order of preference):
        {"position": {"x": 12, "y": 8, "unit": "mm"}}
        {"x": 12, "y": 8}
        {"position": {...}, "id": "<KIID>"}    # test only this item
        {"position": {...}, "reference": "R1"} # test only this footprint
    """
    gate = _require_ipc(iface)
    if gate:
        return gate

    position = params.get("position")
    if isinstance(position, dict):
        x = float(position.get("x", 0))
        y = float(position.get("y", 0))
        unit = str(position.get("unit", "mm"))
    else:
        x = float(params.get("x", 0))
        y = float(params.get("y", 0))
        unit = str(params.get("unit", "mm"))

    tolerance = float(params.get("tolerance", 0))

    # Resolve a specific item to test, if narrowed.
    item_id = params.get("id") or params.get("itemId")
    if not item_id and params.get("reference"):
        ids = _resolve_ids(iface, {"references": [params["reference"]]})
        item_id = ids[0] if ids else None

    return iface.ipc_board_api.hit_test(x=x, y=y, item_id=item_id, tolerance=tolerance, unit=unit)


def handle_interactive_move(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Start KiCad's interactive move on the supplied items.

    Blocks the editor's cursor on the items until the user clicks or
    presses Escape — further mutating API calls return AS_BUSY in the
    meantime.  Pure handoff: don't chain another MCP call before the
    user releases.
    """
    gate = _require_ipc(iface)
    if gate:
        return gate
    ids = _resolve_ids(iface, params)
    if not ids:
        return {
            "success": False,
            "message": "Provide at least one item via 'ids' or 'references'.",
        }
    return iface.ipc_board_api.interactive_move(ids)
