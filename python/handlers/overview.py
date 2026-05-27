"""
Aggregate "overview" handlers that fan out to existing per-category query
handlers and merge their results into a single response.

The plain list_* tools require four separate MCP round-trips just to
understand a small schematic — that round-trip cost and the token usage
of the duplicated boilerplate is why the user asked for these aggregators.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


def _safe_call(label: str, fn, params: Dict[str, Any]) -> Dict[str, Any]:
    """Run a sub-handler that takes ``(params)``. Never raise — overview
    should always return *something* even if one slice fails."""
    try:
        return fn(params)
    except Exception as e:
        logger.warning(f"Overview slice {label!r} failed: {e}")
        return {"success": False, "message": f"{label} failed: {e}"}


def _safe_call_iface(label: str, fn, iface, params: Dict[str, Any]) -> Dict[str, Any]:
    """Variant for handlers with the ``(iface, params)`` signature."""
    try:
        return fn(iface, params)
    except Exception as e:
        logger.warning(f"Overview slice {label!r} failed: {e}")
        return {"success": False, "message": f"{label} failed: {e}"}


def handle_get_schematic_overview(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """One-shot snapshot of a schematic: components, wires, labels, nets.

    Saves three MCP round-trips compared to calling each list_* tool
    individually. Each sub-result keeps its native shape under a top-level
    key, plus a ``summary`` block with counts for quick agent inspection.
    """
    from handlers.schematic_query import (
        handle_list_schematic_components,
        handle_list_schematic_labels,
        handle_list_schematic_nets,
        handle_list_schematic_wires,
    )

    slices = {
        "components": _safe_call_iface(
            "components", handle_list_schematic_components, iface, params
        ),
        "wires": _safe_call_iface("wires", handle_list_schematic_wires, iface, params),
        "labels": _safe_call_iface("labels", handle_list_schematic_labels, iface, params),
        "nets": _safe_call_iface("nets", handle_list_schematic_nets, iface, params),
    }

    failed: List[str] = [k for k, v in slices.items() if not v.get("success")]
    summary = {
        "component_count": _count(slices["components"], "components"),
        "wire_count": _count(slices["wires"], "wires"),
        "label_count": _count(slices["labels"], "labels"),
        "net_count": _count(slices["nets"], "nets"),
        "failed_slices": failed,
    }

    return {
        "success": not failed,
        "message": (
            "Schematic overview"
            if not failed
            else f"Schematic overview (slices failed: {', '.join(failed)})"
        ),
        "summary": summary,
        **slices,
    }


def handle_get_pcb_overview(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """One-shot snapshot of the PCB: components, tracks, vias, zones, layers.

    Mirrors get_schematic_overview for the PCB side so agents can scan the
    state without four separate calls.
    """
    rc = iface.routing_commands
    cc = iface.component_commands
    bc = iface.board_commands

    components = _safe_call("components", cc.get_component_list, {})
    tracks = _safe_call("tracks", rc.query_traces, {})
    zones = _safe_call("zones", rc.query_zones, {})
    nets = _safe_call("nets", rc.get_nets_list, {})
    layers: Dict[str, Any] = {"success": True}
    get_board_info = getattr(bc, "get_board_info", None)
    if callable(get_board_info):
        layers = _safe_call("board_info", get_board_info, {})

    failed: List[str] = []
    for key, val in (
        ("components", components),
        ("tracks", tracks),
        ("zones", zones),
        ("nets", nets),
        ("board_info", layers),
    ):
        if not val.get("success"):
            failed.append(key)

    summary = {
        "component_count": _count(components, "components"),
        "track_count": _count(tracks, "tracks") or _count(tracks, "traces"),
        "zone_count": _count(zones, "zones"),
        "net_count": _count(nets, "nets"),
        "failed_slices": failed,
    }

    return {
        "success": not failed,
        "message": (
            "PCB overview"
            if not failed
            else f"PCB overview (slices failed: {', '.join(failed)})"
        ),
        "summary": summary,
        "components": components,
        "tracks": tracks,
        "zones": zones,
        "nets": nets,
        "board_info": layers,
    }


def _count(result: Dict[str, Any], key: str) -> int:
    """Best-effort count extraction from a sub-handler result."""
    if not isinstance(result, dict) or not result.get("success"):
        return 0
    if isinstance(result.get("count"), int):
        return result["count"]
    arr = result.get(key)
    if isinstance(arr, list):
        return len(arr)
    return 0
