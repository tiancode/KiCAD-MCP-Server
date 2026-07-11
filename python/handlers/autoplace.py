"""Connectivity-driven auto-placement handler.

Extracts footprints (courtyard size, nets, current position) from the SWIG
board, runs the pure greedy placement in commands/component/_autoplace.py,
and applies the returned positions. dryRun previews without moving.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger("kicad_interface")

_NM = 1_000_000


def handle_auto_place_components(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Auto-place components by connectivity (greedy affinity clustering).

    Params: components (refs to place; default all unlocked), fixedRefs
    (stay put but attract), spacing (mm), grid (mm), area {x1,y1,x2,y2}
    (default board outline), dryRun (default false).
    """
    logger.info("Auto-placing components by connectivity")
    try:
        from commands.component._autoplace import (
            PlaceableComponent,
            auto_place,
            detect_decoupling,
        )

        if not iface.board:
            return {
                "success": False,
                "message": "No board is loaded",
                "errorDetails": "Load or create a board first",
            }

        only_refs = set(params.get("components") or [])
        fixed_refs = set(params.get("fixedRefs") or [])
        dry_run = bool(params.get("dryRun", False))

        raw: List[Dict[str, Any]] = []
        modules: Dict[str, Any] = {}
        for fp in iface.board.GetFootprints():
            ref = fp.GetReference()
            modules[ref] = fp
            nets = set()
            for pad in fp.Pads():
                name = pad.GetNetname()
                if name:
                    nets.add(name)
            bb = fp.GetBoundingBox()
            pos = fp.GetPosition()
            raw.append(
                {
                    "reference": ref,
                    "value": fp.GetValue(),
                    "nets": nets,
                    "width": bb.GetWidth() / _NM,
                    "height": bb.GetHeight() / _NM,
                    "x": pos.x / _NM,
                    "y": pos.y / _NM,
                    "locked": bool(fp.IsLocked()) if hasattr(fp, "IsLocked") else False,
                }
            )
        if not raw:
            return {"success": False, "message": "Board has no footprints to place"}

        decoupling = detect_decoupling(raw)

        components = []
        for item in raw:
            ref = item["reference"]
            fixed = ref in fixed_refs or item["locked"] or (only_refs and ref not in only_refs)
            components.append(
                PlaceableComponent(
                    reference=ref,
                    width=max(item["width"], 0.1),
                    height=max(item["height"], 0.1),
                    nets=frozenset(item["nets"]),
                    fixed=bool(fixed),
                    x=item["x"],
                    y=item["y"],
                    is_decoupling=ref in decoupling,
                    decouples=decoupling.get(ref),
                )
            )

        area = params.get("area")
        if area:
            origin = (float(area["x1"]), float(area["y1"]))
            size = (float(area["x2"]) - origin[0], float(area["y2"]) - origin[1])
        else:
            bbox = iface.board.GetBoardEdgesBoundingBox()
            origin = (bbox.GetLeft() / _NM, bbox.GetTop() / _NM)
            size = (bbox.GetWidth() / _NM, bbox.GetHeight() / _NM)
        if size[0] <= 0 or size[1] <= 0:
            return {
                "success": False,
                "message": "No placement area: board has no outline and no area was given",
            }

        result = auto_place(
            components,
            board_origin=origin,
            board_size=size,
            spacing_mm=float(params.get("spacing", 1.0)),
            grid_mm=float(params.get("grid", 0.5)),
        )

        moved = 0
        if not dry_run:
            import pcbnew

            for placement in result.get("placements", []):
                fp = modules.get(placement["reference"])
                if fp is None:
                    continue
                fp.SetPosition(
                    pcbnew.VECTOR2I(int(placement["x"] * _NM), int(placement["y"] * _NM))
                )
                moved += 1

        return {
            "success": True,
            "dryRun": dry_run,
            "moved": moved,
            **result,
        }
    except Exception as e:  # API boundary; bucket: catch + return
        logger.error(f"Error auto-placing components: {e}", exc_info=True)
        return {"success": False, "message": f"Failed to auto-place components: {e}"}
