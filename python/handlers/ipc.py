"""
IPC-specific MCP commands — real-time board mutations / queries that go
straight to KiCAD's IPC API (`iface.ipc_board_api`) rather than the
SWIG pcbnew bindings.  Every handler refuses cleanly when IPC isn't
available, matching the original inline behaviour.

These deliberately stay separate from the regular routing/component
domain modules because the IPC commands are an alternate path (not
IPC-implementations of the SWIG commands) and have their own MCP tool
names prefixed `ipc_*`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

from handlers.ipc_gate import require_ipc

logger = logging.getLogger(__name__)


def _ipc_unavailable(reason: str = "") -> Dict[str, Any]:
    return {
        "success": False,
        "message": f"IPC backend not available{(': ' + reason) if reason else ''}",
    }


def _require_ipc(iface: "KiCADInterface", *, read_only: bool = False) -> Dict[str, Any]:
    """Ensure IPC + the PCB editor frame are both reachable.

    Mirrors the other handler modules: pass the editor-gate response through
    unchanged (so ``needs_pcb_editor: True`` reaches the agent), and wrap
    other IPC-unavailable cases through ``_ipc_unavailable`` so the raw
    reason text stays a short tail clause rather than getting doubly
    prefixed by the upstream "IPC backend not available:" envelope.

    ``read_only=True`` (ipc_list_components / ipc_get_tracks / ipc_get_vias)
    skips the cross-backend conflict refusal — reads can't lose data; the
    dispatcher stamps staleVsDisk on the result.
    """
    return require_ipc(iface, _ipc_unavailable, read_only=read_only)


def handle_ipc_add_track(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Add a track using IPC backend (real-time)."""
    gate = _require_ipc(iface)
    if gate:
        return gate
    try:
        success = iface.ipc_board_api.add_track(
            start_x=params.get("startX", 0),
            start_y=params.get("startY", 0),
            end_x=params.get("endX", 0),
            end_y=params.get("endY", 0),
            width=params.get("width", 0.25),
            layer=params.get("layer", "F.Cu"),
            net_name=params.get("net"),
        )
        return {
            "success": success,
            "message": ("Track added (visible in KiCAD UI)" if success else "Failed to add track"),
            "realtime": True,
        }
    except Exception as e:
        logger.error(f"Error adding track via IPC: {e}")
        return {"success": False, "message": str(e)}


def handle_ipc_add_via(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Add a via using IPC backend (real-time)."""
    gate = _require_ipc(iface)
    if gate:
        return gate
    try:
        success = iface.ipc_board_api.add_via(
            x=params.get("x", 0),
            y=params.get("y", 0),
            diameter=params.get("diameter", 0.8),
            drill=params.get("drill", 0.4),
            net_name=params.get("net"),
            via_type=params.get("type", "through"),
        )
        return {
            "success": success,
            "message": ("Via added (visible in KiCAD UI)" if success else "Failed to add via"),
            "realtime": True,
        }
    except Exception as e:
        logger.error(f"Error adding via via IPC: {e}")
        return {"success": False, "message": str(e)}


def handle_ipc_add_text(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Add text using IPC backend (real-time)."""
    gate = _require_ipc(iface)
    if gate:
        return gate
    try:
        success = iface.ipc_board_api.add_text(
            text=params.get("text", ""),
            x=params.get("x", 0),
            y=params.get("y", 0),
            layer=params.get("layer", "F.SilkS"),
            size=params.get("size", 1.0),
            rotation=params.get("rotation", 0),
        )
        return {
            "success": success,
            "message": ("Text added (visible in KiCAD UI)" if success else "Failed to add text"),
            "realtime": True,
        }
    except Exception as e:
        logger.error(f"Error adding text via IPC: {e}")
        return {"success": False, "message": str(e)}


def handle_ipc_list_components(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """List components using IPC backend."""
    gate = _require_ipc(iface, read_only=True)
    if gate:
        return gate
    try:
        components = iface.ipc_board_api.list_components()
        from utils.pagination import paginate

        components, page = paginate(components, params)
        return {"success": True, "components": components, **page}
    except Exception as e:
        logger.error(f"Error listing components via IPC: {e}")
        return {"success": False, "message": str(e)}


def handle_ipc_get_tracks(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Get tracks using IPC backend."""
    gate = _require_ipc(iface, read_only=True)
    if gate:
        return gate
    try:
        tracks = iface.ipc_board_api.get_tracks()
        return {"success": True, "tracks": tracks, "count": len(tracks)}
    except Exception as e:
        logger.error(f"Error getting tracks via IPC: {e}")
        return {"success": False, "message": str(e)}


def handle_ipc_get_vias(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Get vias using IPC backend."""
    gate = _require_ipc(iface, read_only=True)
    if gate:
        return gate
    try:
        vias = iface.ipc_board_api.get_vias()
        return {"success": True, "vias": vias, "count": len(vias)}
    except Exception as e:
        logger.error(f"Error getting vias via IPC: {e}")
        return {"success": False, "message": str(e)}


def handle_ipc_save_board(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Save board using IPC backend."""
    gate = _require_ipc(iface)
    if gate:
        return gate
    try:
        success = iface.ipc_board_api.save()
        return {
            "success": success,
            "message": "Board saved" if success else "Failed to save board",
        }
    except Exception as e:
        logger.error(f"Error saving board via IPC: {e}")
        return {"success": False, "message": str(e)}
