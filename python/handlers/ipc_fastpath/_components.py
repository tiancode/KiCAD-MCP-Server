"""IPC fast-path: component placement / query handlers.

Split out of the former handlers/ipc_fastpath.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.ipc_fastpath")

from ._common import extract_xy, to_mm


def handle_place_component(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for place_component — places component with real-time UI update.

    Refuses to run inside an open IPC transaction.  ``place_component``
    loads library footprints through pcbnew SWIG, which writes the
    placement directly to the .kicad_pcb file and then calls
    ``board.revert()`` to re-sync the IPC view.  That revert invalidates
    the open commit handle, *and* the placement is already persisted to
    disk — so a subsequent ``rollback_transaction`` can't undo it.  The
    atomicity contract would silently break, so fail fast instead.
    """
    api = iface.ipc_board_api
    if api is not None and getattr(api, "_current_commit", None) is not None:
        return {
            "success": False,
            "message": (
                "place_component cannot run inside an IPC transaction: it "
                "uses a SWIG fallback to load library footprints which writes "
                "directly to disk and reloads the in-memory board, "
                "invalidating the open commit. Commit or rollback the "
                "transaction first, then place the component."
            ),
        }
    try:
        reference = params.get("reference", params.get("componentId", ""))
        # The MCP schema's primary footprint-library field is `componentId`
        # ("Lib:Footprint"); `footprint` is an optional override.  Without
        # this fallback the footprint arrived empty and placement failed.
        footprint = params.get("footprint") or params.get("componentId", "")
        # ipc_backend expects mm — normalise whatever the caller sent.
        x, y, unit = extract_xy(params)
        x, y = to_mm(x, unit), to_mm(y, unit)
        rotation = params.get("rotation", 0)
        layer = params.get("layer", "F.Cu")
        value = params.get("value", "")

        success = iface.ipc_board_api.place_component(
            reference=reference,
            footprint=footprint,
            x=x,
            y=y,
            rotation=rotation,
            layer=layer,
            value=value,
        )

        return {
            "success": success,
            "message": (
                f"Placed component {reference} (visible in KiCAD UI)"
                if success
                else "Failed to place component"
            ),
            "component": {
                "reference": reference,
                "footprint": footprint,
                "position": {"x": x, "y": y, "unit": "mm"},
                "rotation": rotation,
                "layer": layer,
            },
        }
    except Exception as e:
        logger.error(f"IPC place_component error: {e}")
        return {"success": False, "message": str(e)}


def handle_move_component(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for move_component — moves component with real-time UI update."""
    try:
        reference = params.get("reference", params.get("componentId", ""))
        # ipc_backend.move_component expects mm — normalise the caller's unit.
        x, y, unit = extract_xy(params)
        x, y = to_mm(x, unit), to_mm(y, unit)
        rotation = params.get("rotation")

        success = iface.ipc_board_api.move_component(
            reference=reference, x=x, y=y, rotation=rotation
        )

        return {
            "success": success,
            "message": (
                f"Moved component {reference} (visible in KiCAD UI)"
                if success
                else "Failed to move component"
            ),
        }
    except Exception as e:
        logger.error(f"IPC move_component error: {e}")
        return {"success": False, "message": str(e)}


def handle_delete_component(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for delete_component — deletes component with real-time UI update."""
    try:
        reference = params.get("reference", params.get("componentId", ""))

        success = iface.ipc_board_api.delete_component(reference=reference)

        return {
            "success": success,
            "message": (
                f"Deleted component {reference} (visible in KiCAD UI)"
                if success
                else "Failed to delete component"
            ),
        }
    except Exception as e:
        logger.error(f"IPC delete_component error: {e}")
        return {"success": False, "message": str(e)}


def handle_get_component_list(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for get_component_list.

    Every field in a returned component comes from the live IPC view of the
    board — never from the SWIG on-disk copy.  An earlier version patched
    missing ``boundingBox`` values from ``iface.board`` (SWIG), but SWIG
    holds the pre-IPC-mutation positions, so a component that just moved via
    ``move_component`` came back with a fresh ``position`` and a stale
    ``boundingBox`` pointing at where it used to be.  When IPC can't supply
    a box, leave it ``null`` rather than mix two sources in one record.
    """
    try:
        components = iface.ipc_board_api.list_components()
        from utils.pagination import paginate

        components, page = paginate(components, params)
        return {"success": True, "components": components, **page}
    except Exception as e:
        logger.error(f"IPC get_component_list error: {e}")
        return {"success": False, "message": str(e)}


def handle_rotate_component(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for rotate_component — rotates component with real-time UI update."""
    try:
        reference = params.get("reference", params.get("componentId", ""))
        angle = params.get("angle", params.get("rotation", 90))

        # Get current component to find its position
        components = iface.ipc_board_api.list_components()
        target = None
        for comp in components:
            if comp.get("reference") == reference:
                target = comp
                break

        if not target:
            return {"success": False, "message": f"Component {reference} not found"}

        # Use angle as absolute rotation (matches schema description)
        new_rotation = angle % 360

        # Use move_component with new rotation (position stays the same)
        success = iface.ipc_board_api.move_component(
            reference=reference,
            x=target.get("position", {}).get("x", 0),
            y=target.get("position", {}).get("y", 0),
            rotation=new_rotation,
        )

        return {
            "success": success,
            "message": (
                f"Rotated component {reference} by {angle}° (visible in KiCAD UI)"
                if success
                else "Failed to rotate component"
            ),
            "newRotation": new_rotation,
        }
    except Exception as e:
        logger.error(f"IPC rotate_component error: {e}")
        return {"success": False, "message": str(e)}


def handle_get_component_properties(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """IPC handler for get_component_properties — gets detailed component info.

    Like ``handle_get_component_list``, this returns a pure IPC view.  The
    earlier SWIG-fallback for ``boundingBox`` / ``courtyard`` mixed live
    positions with on-disk geometry — a component that just moved via
    ``move_component`` came back with the new ``position`` and the old
    ``boundingBox``.  When IPC doesn't have the box, leave it ``null``
    rather than serve two coordinate frames in one record.
    """
    try:
        reference = params.get("reference", params.get("componentId", ""))

        components = iface.ipc_board_api.list_components()
        target = None
        for comp in components:
            if comp.get("reference") == reference:
                target = comp
                break

        if not target:
            return {"success": False, "message": f"Component {reference} not found"}

        return {"success": True, "component": target}
    except Exception as e:
        logger.error(f"IPC get_component_properties error: {e}")
        return {"success": False, "message": str(e)}


def handle_get_component_pads(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for get_component_pads — pad geometry + nets read live from
    KiCad.  The SWIG handler reads ``iface.board`` and fails "No board is
    loaded" when the user has the board open in KiCad but never ran
    open_project through the MCP; this path reads it over IPC instead.
    """
    try:
        reference = params.get("reference", params.get("componentId", ""))
        if not reference:
            return {"success": False, "message": "reference parameter is required"}

        result = iface.ipc_board_api.get_component_pads(reference, params.get("unit", "mm"))
        if result is None:
            return {"success": False, "message": f"Component {reference} not found"}

        return {"success": True, **result}
    except Exception as e:
        logger.error(f"IPC get_component_pads error: {e}")
        return {"success": False, "message": str(e)}
