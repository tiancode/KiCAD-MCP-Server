"""Connectivity-driven auto-placement handler.

Extracts footprints (courtyard size, nets, current position) from the SWIG
board, runs the pure greedy placement in commands/component/_autoplace.py,
and applies the returned positions. dryRun previews without moving.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List

from utils.responses import no_board_loaded

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
            return no_board_loaded()

        only_refs = set(params.get("components") or [])
        fixed_refs = set(params.get("fixedRefs") or [])
        dry_run = bool(params.get("dryRun", False))

        import pcbnew

        def _keepout_box(fp: Any) -> Any:
            """Courtyard bbox when present (preferred for placement clearance,
            matching component/_query.py), else the raw bounding box."""
            try:
                for layer_id in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
                    courtyard = fp.GetCourtyard(layer_id)
                    if courtyard and courtyard.OutlineCount() > 0:
                        return courtyard.BBox()
            except (AttributeError, RuntimeError, TypeError):
                pass
            return fp.GetBoundingBox()

        raw: List[Dict[str, Any]] = []
        modules: Dict[str, Any] = {}
        # The placement algorithm works in CENTER coordinates, but
        # SetPosition/GetPosition move the footprint ANCHOR — which is not
        # the box center for pin-1-origin parts or asymmetric silkscreen.
        # Track each footprint's (anchor - box_center) offset so extraction
        # feeds centers in and write-back converts centers to anchors.
        anchor_offsets: Dict[str, Any] = {}
        for fp in iface.board.GetFootprints():
            ref = fp.GetReference()
            modules[ref] = fp
            nets = set()
            for pad in fp.Pads():
                name = pad.GetNetname()
                if name:
                    nets.add(name)
            bb = _keepout_box(fp)
            pos = fp.GetPosition()
            center_x = (bb.GetLeft() + bb.GetRight()) / 2.0 / _NM
            center_y = (bb.GetTop() + bb.GetBottom()) / 2.0 / _NM
            anchor_offsets[ref] = (pos.x / _NM - center_x, pos.y / _NM - center_y)
            raw.append(
                {
                    "reference": ref,
                    "value": fp.GetValue(),
                    "nets": nets,
                    "width": (bb.GetRight() - bb.GetLeft()) / _NM,
                    "height": (bb.GetBottom() - bb.GetTop()) / _NM,
                    "x": center_x,
                    "y": center_y,
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
            for placement in result.get("placements", []):
                fp = modules.get(placement["reference"])
                if fp is None:
                    continue
                # placement x/y is the keepout-box CENTER; convert back to the
                # footprint anchor using the offset recorded at extraction.
                off_x, off_y = anchor_offsets.get(placement["reference"], (0.0, 0.0))
                fp.SetPosition(
                    pcbnew.VECTOR2I(
                        int((placement["x"] + off_x) * _NM),
                        int((placement["y"] + off_y) * _NM),
                    )
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
