"""IPC fast-path: trace / via / net routing handlers.

Split out of the former handlers/ipc_fastpath.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.ipc_fastpath")

from ..transactions import visibility_suffix
from ._common import _TO_MM_SCALE, extract_xy, swig_fallback_mutation, to_mm

_NM_PER_MM = 1_000_000.0


def _ipc_pad_boxes_mm(iface: "KiCADInterface") -> list:
    """``(ref, pad_num, net, (left, top, right, bottom))`` in mm for every pad
    on the live IPC board — input for the shared cross-net short check (B4).

    Best-effort: returns ``[]`` on any failure so the guard degrades to the
    pre-guard behaviour (route allowed) rather than breaking a legitimate
    route when the kipy pad geometry can't be read.
    """
    boxes: list = []
    try:
        board = iface.ipc_board_api._get_board()  # noqa: SLF001 — our wrapper's accessor
        footprints = board.get_footprints()
    except Exception:
        return boxes
    for fp in footprints:
        try:
            ref = fp.reference_field.text.value
        except Exception:
            ref = "?"
        try:
            definition = getattr(fp, "definition", None)
            pads = (
                list(definition.pads) if definition is not None else list(getattr(fp, "pads", []))
            )
        except Exception:
            continue
        for pad in pads:
            try:
                pad_net = pad.net.name if getattr(pad, "net", None) else ""
            except Exception:
                pad_net = ""
            if not pad_net:
                continue
            try:
                num = str(pad.number)
            except Exception:
                num = ""
            try:
                pos = pad.position
                cx, cy = pos.x / _NM_PER_MM, pos.y / _NM_PER_MM
            except Exception:
                continue
            half_x = half_y = 0.0
            try:
                size = pad.padstack.copper_layers[0].size
                half_x = (size.x / _NM_PER_MM) / 2.0
                half_y = (size.y / _NM_PER_MM) / 2.0
            except Exception:
                pass
            boxes.append((ref, num, pad_net, (cx - half_x, cy - half_y, cx + half_x, cy + half_y)))
    return boxes


def handle_route_trace(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for route_trace — adds track with real-time UI update."""
    try:
        # Extract parameters matching the existing route_trace interface.
        # Accept both nested {"start": {"x":..,"y":..}} and flat startX/startY shapes.
        # The IPC API speaks mm — honour the optional per-point unit like the
        # SWIG path does, or mil/inch coordinates land 25.4x/1000x off.
        start_x, start_y, start_unit = extract_xy(
            params, key="start", flat_x="startX", flat_y="startY"
        )
        end_x, end_y, end_unit = extract_xy(params, key="end", flat_x="endX", flat_y="endY")
        start_x, start_y = to_mm(start_x, start_unit), to_mm(start_y, start_unit)
        end_x, end_y = to_mm(end_x, end_unit), to_mm(end_y, end_unit)
        layer = params.get("layer", "F.Cu")
        width = params.get("width", 0.25)
        net = params.get("net")
        force = bool(params.get("force", False))

        # Refuse a cross-net short (B4, IPC parity): if a trace endpoint lands
        # on a pad belonging to a DIFFERENT net, stamping this net onto it
        # bridges the two nets.  Best-effort over the live IPC pads; force=true
        # overrides.  Shares the pure conflict core with the SWIG path.
        if net and not force:
            from commands.routing._helpers import (
                _endpoint_conflict_messages,
                _refuse_cross_net_short,
            )

            conflicts = _endpoint_conflict_messages(
                [(start_x, start_y), (end_x, end_y)],
                net,
                _ipc_pad_boxes_mm(iface),
            )
            if conflicts:
                return _refuse_cross_net_short(net, conflicts)

        success = iface.ipc_board_api.add_track(
            start_x=start_x,
            start_y=start_y,
            end_x=end_x,
            end_y=end_y,
            width=width,
            layer=layer,
            net_name=net,
        )

        return {
            "success": success,
            "message": (
                f"Added trace {visibility_suffix(iface)}" if success else "Failed to add trace"
            ),
            "trace": {
                "start": {"x": start_x, "y": start_y, "unit": "mm"},
                "end": {"x": end_x, "y": end_y, "unit": "mm"},
                "layer": layer,
                "width": width,
                "net": net,
            },
        }
    except Exception as e:
        logger.error(f"IPC route_trace error: {e}")
        return {"success": False, "message": str(e)}


def handle_route_arc_trace(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for route_arc_trace — adds copper arc with real-time UI update."""
    try:
        start = params.get("start", {})
        mid = params.get("mid", {})
        end = params.get("end", {})
        layer = params.get("layer", "F.Cu")
        width = params.get("width", 0.25)
        net = params.get("net")

        # Honour the optional per-point unit — the IPC API expects mm.
        start_unit = start.get("unit", "mm")
        mid_unit = mid.get("unit", "mm")
        end_unit = end.get("unit", "mm")
        start_x = to_mm(start.get("x", 0), start_unit)
        start_y = to_mm(start.get("y", 0), start_unit)
        mid_x = to_mm(mid.get("x", 0), mid_unit)
        mid_y = to_mm(mid.get("y", 0), mid_unit)
        end_x = to_mm(end.get("x", 0), end_unit)
        end_y = to_mm(end.get("y", 0), end_unit)

        if not hasattr(iface.ipc_board_api, "add_arc_track"):
            return {
                "success": False,
                "message": "IPC backend does not support arc track on this installation",
            }

        success = iface.ipc_board_api.add_arc_track(
            start_x=start_x,
            start_y=start_y,
            mid_x=mid_x,
            mid_y=mid_y,
            end_x=end_x,
            end_y=end_y,
            width=width,
            layer=layer,
            net_name=net,
        )

        return {
            "success": success,
            "message": (
                f"Added arc trace {visibility_suffix(iface)}"
                if success
                else "Failed to add arc trace"
            ),
            "arc": {
                "start": {"x": start_x, "y": start_y, "unit": "mm"},
                "mid": {"x": mid_x, "y": mid_y, "unit": "mm"},
                "end": {"x": end_x, "y": end_y, "unit": "mm"},
                "layer": layer,
                "width": width,
                "net": net,
            },
        }
    except Exception as e:
        logger.error(f"IPC route_arc_trace error: {e}")
        return {"success": False, "message": str(e)}


def handle_add_via(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for add_via — adds via with real-time UI update."""
    try:
        x, y, unit = extract_xy(params)
        # The IPC API expects mm; convert mil/inch like the SWIG path does.
        x, y = to_mm(x, unit), to_mm(y, unit)

        size = params.get("size", 0.8)
        drill = params.get("drill", 0.4)
        net = params.get("net")
        from_layer = params.get("from_layer", "F.Cu")
        to_layer = params.get("to_layer", "B.Cu")

        success = iface.ipc_board_api.add_via(
            x=x, y=y, diameter=size, drill=drill, net_name=net, via_type="through"
        )

        return {
            "success": success,
            "message": (
                f"Added via {visibility_suffix(iface)}" if success else "Failed to add via"
            ),
            "via": {
                "position": {"x": x, "y": y, "unit": "mm"},
                "size": size,
                "drill": drill,
                "from_layer": from_layer,
                "to_layer": to_layer,
                "net": net,
            },
        }
    except Exception as e:
        logger.error(f"IPC add_via error: {e}")
        return {"success": False, "message": str(e)}


def handle_add_net(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for add_net."""
    # Note: Net creation via IPC is limited - nets are typically created
    # when components are placed. Return success for compatibility.
    name = params.get("name")
    logger.info(f"IPC add_net: {name} (nets auto-created with components)")
    return {
        "success": True,
        "message": f"Net '{name}' will be created when components are connected",
        "net": {"name": name},
    }


def handle_delete_trace(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for delete_trace.

    IPC doesn't support direct trace deletion yet, so fall back to SWIG —
    via swig_fallback_mutation, which keeps the cross-backend bookkeeping
    (conflict gate, auto-save, _swig_writes_landed) that the dispatcher
    only applies on its own SWIG branch.
    """
    logger.info("delete_trace: Falling back to SWIG (IPC doesn't support trace deletion)")
    return swig_fallback_mutation(
        iface, "delete_trace", iface.routing_commands.delete_trace, params
    )


def handle_query_traces(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for query_traces — reads traces from the live KiCAD board."""
    try:
        net_name = params.get("net")
        layer_filter = params.get("layer")
        bbox = params.get("boundingBox")
        include_vias = params.get("includeVias", False)

        def point_in_bbox(point: Dict[str, Any]) -> bool:
            if not bbox:
                return True
            # Use the shared scale table so "mil" isn't silently treated as mm —
            # the prior branch only recognised "inch".
            unit_scale = _TO_MM_SCALE.get(bbox.get("unit", "mm"), 1.0)
            x1 = bbox.get("x1", 0) * unit_scale
            y1 = bbox.get("y1", 0) * unit_scale
            x2 = bbox.get("x2", 0) * unit_scale
            y2 = bbox.get("y2", 0) * unit_scale
            low_x, high_x = sorted((x1, x2))
            low_y, high_y = sorted((y1, y2))
            return low_x <= point.get("x", 0) <= high_x and low_y <= point.get("y", 0) <= high_y

        tracks = list(iface.ipc_board_api.get_tracks())
        vias_raw = list(iface.ipc_board_api.get_vias()) if include_vias else []

        # Resolve the net filter against the board's real nets so a bare "GND"
        # matches tracks/vias on a hierarchical "/GND" (Bug 2 — parity with
        # copper_pour).  Read-only: never refuses, only annotates.  When the
        # board nets can't be enumerated, resolve against the copper's own nets.
        target_net = net_name
        net_annotations: Dict[str, Any] = {}
        if net_name:
            from commands.routing._zones import resolve_query_net_filter

            from ._zones import _ipc_available_net_names

            available = _ipc_available_net_names(iface)
            if available is None:
                names = [t.get("net", "") for t in tracks]
                names += [v.get("net", "") for v in vias_raw]
                available = [n for n in names if n]
            if available:
                target_net, net_annotations = resolve_query_net_filter(net_name, available)

        # Output unit: the TS schema documents `unit` for trace coordinates
        # but it was silently ignored (always mm).
        out_unit = str(params.get("unit", "mm")).lower()
        if out_unit not in _TO_MM_SCALE:
            out_unit = "mm"
        from_mm = 1.0 / _TO_MM_SCALE[out_unit]  # mm -> requested unit

        def convert(value: Any) -> Any:
            return value if from_mm == 1.0 else value * from_mm

        traces = []
        for track in tracks:
            if target_net and track.get("net") != target_net:
                continue

            layer = iface._normalize_ipc_layer_name(track.get("layer", ""))
            if layer_filter and layer != layer_filter:
                continue

            start = track.get("start", {})
            end = track.get("end", {})
            if bbox and not (point_in_bbox(start) or point_in_bbox(end)):
                continue

            dx = end.get("x", 0) - start.get("x", 0)
            dy = end.get("y", 0) - start.get("y", 0)
            traces.append(
                {
                    "uuid": track.get("id", ""),
                    "net": track.get("net", ""),
                    "netCode": track.get("netCode", 0),
                    "layer": layer,
                    "width": convert(track.get("width", 0)),
                    "start": {
                        "x": convert(start.get("x", 0)),
                        "y": convert(start.get("y", 0)),
                        "unit": out_unit,
                    },
                    "end": {
                        "x": convert(end.get("x", 0)),
                        "y": convert(end.get("y", 0)),
                        "unit": out_unit,
                    },
                    "length": convert((dx**2 + dy**2) ** 0.5),
                }
            )

        # Pagination parity with the SWIG query_traces (D2): honour
        # limit/offset and emit the IDENTICAL total/count/offset/limit/truncated
        # metadata.  The SWIG path paginates ONLY traces (vias are returned in
        # full), so match that exactly — `traceCount` becomes the full total and
        # `traces` the current page.  Filtering already happened above, so the
        # slice is over the matching traces just like SWIG.
        from utils.pagination import paginate

        traces, page = paginate(traces, params)
        result: Dict[str, Any] = {
            "success": True,
            "traceCount": page["total"],
            "traces": traces,
            **page,
            **net_annotations,
        }

        if include_vias:
            vias = []
            for via in vias_raw:
                if target_net and via.get("net") != target_net:
                    continue
                position = via.get("position", {})
                if bbox and not point_in_bbox(position):
                    continue
                vias.append(
                    {
                        "uuid": via.get("id", ""),
                        "position": {
                            "x": convert(position.get("x", 0)),
                            "y": convert(position.get("y", 0)),
                            "unit": out_unit,
                        },
                        "net": via.get("net", ""),
                        "netCode": via.get("netCode", 0),
                        "diameter": convert(via.get("diameter", 0)),
                        "drill": convert(via.get("drill", 0)),
                    }
                )
            result["viaCount"] = len(vias)
            result["vias"] = vias

        return result
    except Exception as e:
        logger.error(f"IPC query_traces error: {e}")
        return {"success": False, "message": str(e)}


def handle_get_nets_list(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """IPC handler for get_nets_list — gets nets with real-time data."""
    try:
        nets = iface.ipc_board_api.get_nets()

        return {"success": True, "nets": nets, "count": len(nets)}
    except Exception as e:
        logger.error(f"IPC get_nets_list error: {e}")
        return {"success": False, "message": str(e)}
