"""Courtyard-overlap DRC check and its geometry helpers.

Split out of the former monolithic commands/component.py.
"""

import logging
import math
from typing import Any, Dict

import pcbnew
from utils.responses import failed, no_board_loaded

logger = logging.getLogger("kicad_interface")


class CourtyardMixin:
    # -----------------------------------------------------------------------
    # check_courtyard_overlaps
    #
    # Originally prototyped in morningfire-pcb-automation
    #   https://github.com/NiNjA-CodE/morningfire-pcb-automation
    #   (scripts/placement/check_overlaps.py — AABB lookup-table version)
    #
    # The version here uses the real courtyard polygons from the loaded
    # board (more accurate than a static lookup), with virtual-placement
    # support so an AI can validate a proposed move before committing it.
    # -----------------------------------------------------------------------
    def check_courtyard_overlaps(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Detect courtyard overlaps between footprints (and board-edge violations).

        Each footprint has an F.Courtyard / B.Courtyard polygon that defines its
        physical keepout. KiCad's own DRC reports `courtyards_overlap` after the
        fact; this tool lets the caller check ahead of time — either against
        the current placement or against a hypothetical placement
        (``positions``) that hasn't been committed to the board yet.

        Args:
            positions: Optional dict ``{ref: [x, y]}`` or ``{ref: [x, y, rot]}``
                in mm/degrees. Virtual placements: the listed refs are
                temporarily considered to be at the given (x, y[, rot]). The
                board file is not modified.
            refs: Optional list of reference designators to limit the check
                to. Default: every footprint on the board.
            margin: Extra clearance in mm to enforce around each courtyard
                (default 0). Overlaps below this margin are flagged.
            include_boundary: If True (default), also flag courtyards that
                extend past the board outline.
            board_outline: Optional ``{"x1": ..., "y1": ..., "x2": ..., "y2":
                ..., "unit": "mm"|"inch"}`` override; otherwise the board's
                Edge.Cuts bounding box is used.

        Returns:
            ``{"success": True, "overlaps": [...], "boundary_violations": [...],
                "summary": {...}}``
            Each overlap entry has ``{a, b, overlap_x_mm, overlap_y_mm,
            overlap_area_mm2, bbox}``; each boundary entry has
            ``{ref, bbox, exceeds: {top, bottom, left, right} in mm}``.
        """
        try:
            if not self.board:
                return no_board_loaded()

            ref_filter = params.get("refs")
            if ref_filter is not None:
                ref_filter = set(ref_filter)

            margin_mm = float(params.get("margin", 0.0))
            include_boundary = bool(params.get("include_boundary", True))

            virtual = {}
            for ref, spec in (params.get("positions") or {}).items():
                if not isinstance(spec, (list, tuple)) or len(spec) not in (2, 3):
                    return {
                        "success": False,
                        "message": "Bad position spec",
                        "errorDetails": f"positions['{ref}'] must be [x, y] or [x, y, rot]; "
                        f"got {spec!r}",
                    }
                virtual[ref] = spec

            # Resolve board outline once.
            outline_bbox = self._resolve_outline_bbox(params.get("board_outline"))

            # Gather courtyard bboxes for every footprint we'll consider.
            # ``fallback_refs`` records which parts had no real courtyard
            # polygon and fell back to the (text-excluded) footprint bbox — the
            # response flags these so a caller knows the keepout is approximate.
            entries = []
            fallback_refs = set()
            for fp in self.board.GetFootprints():
                ref = fp.GetReference()
                if ref_filter is not None and ref not in ref_filter:
                    continue
                bbox, used_fallback = self._footprint_courtyard_bbox(fp, virtual.get(ref))
                if bbox is None:
                    continue
                if used_fallback:
                    fallback_refs.add(ref)
                # Expand by margin
                if margin_mm:
                    x1, y1, x2, y2 = bbox
                    bbox = (x1 - margin_mm, y1 - margin_mm, x2 + margin_mm, y2 + margin_mm)
                entries.append((ref, bbox))

            # Pairwise overlap (AABB intersect — matches KiCad DRC's
            # courtyard-overlap detection model).
            overlaps = []
            entries_sorted = sorted(entries, key=lambda e: e[0])
            for i in range(len(entries_sorted)):
                a_ref, a = entries_sorted[i]
                for j in range(i + 1, len(entries_sorted)):
                    b_ref, b = entries_sorted[j]
                    if a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]:
                        ox = min(a[2], b[2]) - max(a[0], b[0])
                        oy = min(a[3], b[3]) - max(a[1], b[1])
                        overlaps.append(
                            {
                                "a": a_ref,
                                "b": b_ref,
                                "overlap_x_mm": round(ox, 3),
                                "overlap_y_mm": round(oy, 3),
                                "overlap_area_mm2": round(ox * oy, 4),
                                "bbox": {
                                    "x1": round(max(a[0], b[0]), 3),
                                    "y1": round(max(a[1], b[1]), 3),
                                    "x2": round(min(a[2], b[2]), 3),
                                    "y2": round(min(a[3], b[3]), 3),
                                    "unit": "mm",
                                },
                                # True when either part lacked a courtyard and
                                # used the approximate bbox fallback — the
                                # overlap may be a placement artifact, not real.
                                "fallback": a_ref in fallback_refs or b_ref in fallback_refs,
                            }
                        )

            # Boundary violations
            boundary_violations = []
            if include_boundary and outline_bbox is not None:
                ox1, oy1, ox2, oy2 = outline_bbox
                for ref, bbox in entries_sorted:
                    x1, y1, x2, y2 = bbox
                    exceeds = {}
                    if x1 < ox1 - 1e-6:
                        exceeds["left"] = round(ox1 - x1, 3)
                    if x2 > ox2 + 1e-6:
                        exceeds["right"] = round(x2 - ox2, 3)
                    if y1 < oy1 - 1e-6:
                        exceeds["top"] = round(oy1 - y1, 3)
                    if y2 > oy2 + 1e-6:
                        exceeds["bottom"] = round(y2 - oy2, 3)
                    if exceeds:
                        boundary_violations.append(
                            {
                                "ref": ref,
                                "bbox": {
                                    "x1": round(x1, 3),
                                    "y1": round(y1, 3),
                                    "x2": round(x2, 3),
                                    "y2": round(y2, 3),
                                    "unit": "mm",
                                },
                                "exceeds": exceeds,
                                # True when this part lacked a courtyard and used
                                # the approximate bbox fallback — the edge
                                # violation may be a placement artifact.
                                "fallback": ref in fallback_refs,
                            }
                        )

            return {
                "success": True,
                "overlaps": overlaps,
                "boundary_violations": boundary_violations,
                "summary": {
                    "checked": len(entries_sorted),
                    "overlap_count": len(overlaps),
                    "boundary_violation_count": len(boundary_violations),
                    "margin_mm": margin_mm,
                    "virtual_placements": len(virtual),
                    # Parts with no F/B.Courtyard polygon — their keepout was
                    # approximated from the (text-excluded) footprint bbox, so
                    # any overlap/boundary flag involving them is lower-confidence.
                    "bbox_fallback_refs": sorted(fallback_refs),
                    "board_outline_mm": (
                        None
                        if outline_bbox is None
                        else {
                            "x1": round(outline_bbox[0], 3),
                            "y1": round(outline_bbox[1], 3),
                            "x2": round(outline_bbox[2], 3),
                            "y2": round(outline_bbox[3], 3),
                            "unit": "mm",
                        }
                    ),
                },
            }
        except Exception as e:
            logger.error(f"check_courtyard_overlaps failed: {e}", exc_info=True)
            return failed("check_courtyard_overlaps failed", e)

    # --- helpers for check_courtyard_overlaps ----------------------------

    @staticmethod
    def _nm_to_mm(v):
        return v / 1_000_000.0

    def _resolve_outline_bbox(self, override):
        """Return (x1, y1, x2, y2) in mm for the board outline, or None.

        Priority:
          1. caller-supplied override dict (x1,y1,x2,y2 + unit)
          2. board.GetBoardEdgesBoundingBox()
        """
        if override:
            scale = 1.0 if override.get("unit", "mm") == "mm" else 25.4
            return (
                override["x1"] * scale,
                override["y1"] * scale,
                override["x2"] * scale,
                override["y2"] * scale,
            )
        try:
            bb = self.board.GetBoardEdgesBoundingBox()
            return (
                self._nm_to_mm(bb.GetLeft()),
                self._nm_to_mm(bb.GetTop()),
                self._nm_to_mm(bb.GetRight()),
                self._nm_to_mm(bb.GetBottom()),
            )
        except (AttributeError, RuntimeError):
            # Board has no edges defined, or SWIG proxy dehydrated.  Returning
            # None lets the caller fall back to per-component bounding boxes.
            return None

    def _footprint_courtyard_bbox(self, fp, override_pos):
        """Return ``(bbox_mm, used_fallback)``, optionally relocated.

        ``bbox_mm`` is ``(x1, y1, x2, y2)`` in mm, or ``None`` when the
        footprint has no usable geometry.  ``used_fallback`` is ``True`` when
        the box came from the footprint bounding box (no real courtyard
        polygon) — the caller surfaces this so a consumer knows the keepout is
        approximate.

        Strategy:
          1. Use the F.Courtyard or B.Courtyard polygon if present (the exact
             physical keepout).
          2. Otherwise fall back to ``footprint.GetBoundingBox`` with text
             EXCLUDED.  The parameterless ``GetBoundingBox()`` INCLUDES field
             text (Reference / Value), which balloons the box for
             courtyard-less parts — a 6 mm mounting hole whose
             "MountingHole_3.2mm" Value text stretched it to ~20 mm, producing
             false overlaps and false board-edge violations.  The
             text-excluding overload's arity drifted across KiCad (9.x:
             ``GetBoundingBox(aIncludeText)``; 10.x adds a second
             ``aIncludeHiddenText`` flag), so try the widest arity first and
             degrade defensively — only falling back to the text-inclusive
             ``GetBoundingBox()`` if no text-excluding overload exists.
          3. If override_pos is given, translate (and optionally rotate) the
             bbox to land at the virtual position — preserving the bbox's
             extents relative to the new anchor.
        """
        bbox_nm = None
        used_fallback = False
        # Try the courtyard polygons first (front then back)
        for layer in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
            try:
                ct = fp.GetCourtyard(layer)
                if ct is not None and ct.OutlineCount() > 0:
                    box = ct.BBox()
                    bbox_nm = (box.GetLeft(), box.GetTop(), box.GetRight(), box.GetBottom())
                    break
            except (AttributeError, RuntimeError):
                # Courtyard layer not defined for this footprint, or SWIG
                # method-dispatch failure — try the other layer.
                continue
        if bbox_nm is None:
            box = None
            for args in ((False, False), (False,), ()):
                try:
                    box = fp.GetBoundingBox(*args)
                    break
                except TypeError:
                    # Wrong arity for this KiCad build — try the next overload.
                    continue
                except (AttributeError, RuntimeError):
                    # Footprint has no geometry at all — caller can't compute
                    # a courtyard bbox.  Return None to let it skip cleanly.
                    return None, False
            if box is None:
                return None, False
            bbox_nm = (box.GetLeft(), box.GetTop(), box.GetRight(), box.GetBottom())
            used_fallback = True

        x1, y1, x2, y2 = (self._nm_to_mm(v) for v in bbox_nm)

        if override_pos is None:
            return (x1, y1, x2, y2), used_fallback

        # Re-anchor at the virtual position. We do this by translating the
        # bbox by (new_pos - current_pos). Rotation override is honoured by
        # rotating the *local* bbox (relative to the current anchor) by the
        # delta between the override rotation and the current rotation, then
        # re-anchoring. This is conservative for non-square parts: the AABB
        # of a rotated bbox is larger than the rotated polygon, but never
        # smaller — so an overlap report is still correct (never false-negative).
        cur = fp.GetPosition()
        cur_x_mm = self._nm_to_mm(cur.x)
        cur_y_mm = self._nm_to_mm(cur.y)
        new_x = float(override_pos[0])
        new_y = float(override_pos[1])
        new_rot = float(override_pos[2]) if len(override_pos) == 3 else None

        # Local bbox (relative to current anchor)
        lx1, ly1, lx2, ly2 = x1 - cur_x_mm, y1 - cur_y_mm, x2 - cur_x_mm, y2 - cur_y_mm

        if new_rot is not None:
            cur_rot = fp.GetOrientationDegrees()
            delta = new_rot - cur_rot
            if abs(delta) > 0.01:
                lx1, ly1, lx2, ly2 = self._rotate_aabb(lx1, ly1, lx2, ly2, delta)

        return (new_x + lx1, new_y + ly1, new_x + lx2, new_y + ly2), used_fallback

    @staticmethod
    def _rotate_aabb(x1, y1, x2, y2, angle_deg):
        """Rotate the four AABB corners around origin and return the new
        axis-aligned bounding box. KiCad uses Y-down screen coords."""
        rad = math.radians(angle_deg)
        c, s = math.cos(rad), math.sin(rad)
        # Note: screen Y-down means rotation CCW visually requires the
        # standard math rotation with y negated; but for AABB extents this
        # is symmetric — we end up with the same xmin/ymin/xmax/ymax.
        corners = [(x1, y1), (x2, y1), (x1, y2), (x2, y2)]
        rotated = [(x * c - y * s, x * s + y * c) for x, y in corners]
        xs = [p[0] for p in rotated]
        ys = [p[1] for p in rotated]
        return min(xs), min(ys), max(xs), max(ys)
