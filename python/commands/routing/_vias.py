"""Via and GND-stitching commands for RoutingCommands.

Split out of the former monolithic commands/routing.py."""

import logging
from typing import Any, Dict, List

import pcbnew
from utils.responses import failed, no_board_loaded
from utils.units import unit_to_nm_scale

from ._helpers import _point_to_segment_distance_nm

logger = logging.getLogger("kicad_interface")


class ViaMixin:
    def add_via(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add a via at the specified location"""
        try:
            if not self.board:
                return no_board_loaded()

            position = params.get("position")
            size = params.get("size")
            drill = params.get("drill")
            net = params.get("net")
            from_layer = params.get("from_layer", "F.Cu")
            to_layer = params.get("to_layer", "B.Cu")

            if not position:
                return {
                    "success": False,
                    "message": "Missing position",
                    "errorDetails": "position parameter is required",
                }

            via = pcbnew.PCB_VIA(self.board)

            # Set position — the MCP schema marks unit optional, so default mm
            unit = position.get("unit", "mm")
            scale = unit_to_nm_scale(unit)
            x_nm = int(position["x"] * scale)
            y_nm = int(position["y"] * scale)
            via.SetPosition(pcbnew.VECTOR2I(x_nm, y_nm))

            # Set size and drill (default to board's current via settings)
            design_settings = self.board.GetDesignSettings()
            via.SetWidth(int(size * 1000000) if size else design_settings.GetCurrentViaSize())
            via.SetDrill(int(drill * 1000000) if drill else design_settings.GetCurrentViaDrill())

            from_id = self.board.GetLayerID(from_layer)
            to_id = self.board.GetLayerID(to_layer)
            if from_id < 0 or to_id < 0:
                return {
                    "success": False,
                    "message": "Invalid layer",
                    "errorDetails": "Specified layers do not exist",
                }
            via.SetLayerPair(from_id, to_id)

            if net:
                netinfo = self.board.GetNetInfo()
                nets_map = netinfo.NetsByName()
                if nets_map.has_key(net):
                    net_obj = nets_map[net]
                    via.SetNet(net_obj)

            self.board.Add(via)

            return {
                "success": True,
                "message": "Added via",
                "via": {
                    "position": {
                        "x": position["x"],
                        "y": position["y"],
                        "unit": unit,
                    },
                    "size": via.GetWidth(pcbnew.F_Cu) / 1000000,
                    "drill": via.GetDrill() / 1000000,
                    "from_layer": from_layer,
                    "to_layer": to_layer,
                    "net": net,
                },
            }

        except Exception as e:
            logger.error(f"Error adding via: {str(e)}")
            return failed("Failed to add via", e)

    # -----------------------------------------------------------------------
    # add_gnd_stitching_vias
    #
    # Originally prototyped in morningfire-pcb-automation:
    #   https://github.com/NiNjA-CodE/morningfire-pcb-automation
    #   (scripts/ground/add_gnd_vias.py — regex-on-PCB-text version)
    #
    # The version here uses the pcbnew API so it handles arbitrary
    # rotations, gets net IDs / clearances from the loaded board, and
    # works against the live in-memory board state (so two calls in
    # sequence — e.g. "around U1" then "across the board" — both see
    # the first call's placements). All copper layers are checked
    # because a through-hole via penetrates the full stackup; missing a
    # B.Cu collision check is the classic way GND-stitching tools
    # create silent shorts.
    # -----------------------------------------------------------------------
    def add_gnd_stitching_vias(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Drop GND stitching vias across the board, collision-checked on every copper layer.

        Strategies (combine freely):
          - ``grid``        Place candidates on a regular grid across the board
                            interior. Each candidate is accepted only if its
                            full keep-out radius is clear of every non-GND
                            segment / via / pad on every copper layer.
          - ``around_refs`` For each named footprint, try a small radius of
                            grid points around its anchor. Good for densifying
                            ground around noisy ICs (MCUs, switching
                            regulators, RF parts).
          - ``in_zones``    Restrict candidates to points actually inside the
                            filled polygons of GND copper zones, so each new
                            via lands on copper that's already a GND
                            equipotential. Highly recommended on boards where
                            the GND zone is fragmented — these vias
                            actually stitch the zones, not just float on
                            silkscreen.

        Args:
            gndNet: name of the ground net. Default: auto-detect from
                ``GND`` / ``GROUND`` / ``VSS`` in that order, else error.
            strategies: list of strategy names. Default ``["grid"]``.
                Pass ``["grid", "around_refs", "in_zones"]`` for the kitchen
                sink — collision check + intra-call dedupe means the
                strategies compose safely.
            viaSize: pad diameter mm. Default 0.6.
            viaDrill: drill diameter mm. Default 0.3.
            clearance: extra clearance beyond required mm. Default 0.2.
            spacing: grid spacing mm for ``grid`` and ``around_refs``.
                Default 5.0.
            densifyRefs: list of refs for ``around_refs``. Default [].
            densifyRadius: how many grid cells around each ref to try.
                Default 2 (5x5 candidate field per ref).
            edgeClearance: copper-to-edge clearance mm (alias: edgeMargin).
                Default 0.5. The via CENTRE keep-out adds the via radius so
                the via COPPER keeps this distance from Edge.Cuts.
            force: place vias even when the GND zones are unfilled (which
                makes stitching vias dangle). Default False — an unfilled
                GND zone is refused with needs_zone_fill.
            maxVias: maximum total placements (across all strategies).
                Default unlimited.
            dryRun: don't write, just return placements.

        Returns:
            ``{"success": True, "placed": [{"x", "y", "unit"}, ...],
                "summary": {...}}``
        """
        if not self.board:
            return no_board_loaded()

        try:
            return self._do_add_gnd_stitching(params)
        except Exception as e:
            import traceback

            logger.error(f"add_gnd_stitching_vias failed: {e}\n{traceback.format_exc()}")
            return failed("add_gnd_stitching_vias failed", e)

    def _do_add_gnd_stitching(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # --- Parse params ---
        gnd_net_name = params.get("gndNet")
        strategies = list(params.get("strategies") or ["grid"])
        for s in strategies:
            if s not in ("grid", "around_refs", "in_zones"):
                return {
                    "success": False,
                    "message": f"Unknown strategy '{s}'",
                    "errorDetails": "Valid strategies: grid, around_refs, in_zones",
                }

        via_size_mm = float(params.get("viaSize", 0.6))
        via_drill_mm = float(params.get("viaDrill", 0.3))
        if via_drill_mm >= via_size_mm:
            return {
                "success": False,
                "message": "Invalid via geometry",
                "errorDetails": "viaDrill must be smaller than viaSize",
            }
        clearance_mm = float(params.get("clearance", 0.2))
        spacing_mm = float(params.get("spacing", 5.0))
        densify_refs = list(params.get("densifyRefs") or [])
        densify_radius = int(params.get("densifyRadius", 2))
        # Copper-to-edge clearance.  ``edgeClearance`` is the canonical name
        # (it matches the DRC ``copper_edge_clearance`` rule); ``edgeMargin``
        # is the legacy alias.  Default 0.5 mm.  Historically the schema only
        # exposed ``edgeMargin`` and applied it to the via CENTRE, so a caller
        # passing ``edgeClearance`` was silently dropped by the SDK and the via
        # copper still landed via_radius closer to the edge than intended
        # (the GD32 E2E: edgeClearance:1.5 ignored, vias 0.5 mm from the edge →
        # copper_edge_clearance errors).
        edge_clearance_raw = params.get("edgeClearance")
        if edge_clearance_raw is None:
            edge_clearance_raw = params.get("edgeMargin", 0.5)
        edge_clearance_mm = max(0.0, float(edge_clearance_raw))
        force = bool(params.get("force", False))
        max_vias_raw = params.get("maxVias")
        max_vias = int(max_vias_raw) if max_vias_raw is not None else None
        dry_run = bool(params.get("dryRun", False))

        scale = 1_000_000  # mm -> nm
        via_size_nm = int(via_size_mm * scale)
        via_drill_nm = int(via_drill_mm * scale)
        via_radius_nm = via_size_nm // 2
        clearance_nm = int(clearance_mm * scale)
        spacing_nm = int(spacing_mm * scale)
        edge_clearance_nm = int(edge_clearance_mm * scale)
        # The via CENTRE keep-out from Edge.Cuts is the requested copper
        # clearance PLUS the via radius, so the via copper annulus actually
        # keeps ``edge_clearance_mm`` from the board edge.
        edge_keepout_nm = edge_clearance_nm + via_radius_nm

        # --- Resolve GND net ---
        netinfo = self.board.GetNetInfo()
        nets_by_name = netinfo.NetsByName()
        gnd_net = None
        if gnd_net_name:
            if nets_by_name.has_key(gnd_net_name):
                gnd_net = nets_by_name[gnd_net_name]
            else:
                return {
                    "success": False,
                    "message": f"Net '{gnd_net_name}' not found",
                    "errorDetails": "Pass a net that exists on this board",
                }
        else:
            for candidate in ("GND", "GROUND", "VSS", "/GND"):
                if nets_by_name.has_key(candidate):
                    gnd_net = nets_by_name[candidate]
                    gnd_net_name = candidate
                    break
            if gnd_net is None:
                return {
                    "success": False,
                    "message": "No GND net detected",
                    "errorDetails": (
                        "Pass gndNet explicitly. Auto-detect tries " "GND / GROUND / VSS / /GND."
                    ),
                }
        gnd_net_code = gnd_net.GetNetCode()

        # --- Board outline bbox (for the grid + edge guard) ---
        edge_bb = self.board.GetBoardEdgesBoundingBox()
        if edge_bb.GetWidth() <= 0 or edge_bb.GetHeight() <= 0:
            return {
                "success": False,
                "message": "Board outline is missing or empty",
                "errorDetails": "Define Edge.Cuts before stitching vias",
            }
        # GetBoardEdgesBoundingBox includes the Edge.Cuts STROKE, inflating
        # the bbox by half the line width per side — without compensation a
        # 0.1 mm outline erodes the effective copper clearance by 0.05 mm
        # (observed on a real board: requested 0.5 mm, DRC measured 0.45 mm).
        edge_stroke_half_nm = 0
        try:
            edge_layer = self.board.GetLayerID("Edge.Cuts")
            for drawing in self.board.GetDrawings():
                try:
                    if drawing.GetLayer() != edge_layer:
                        continue
                    edge_stroke_half_nm = max(edge_stroke_half_nm, int(drawing.GetWidth()) // 2)
                except Exception:
                    continue
        except Exception:
            edge_stroke_half_nm = 0

        x_min = edge_bb.GetLeft() + edge_stroke_half_nm + edge_keepout_nm
        y_min = edge_bb.GetTop() + edge_stroke_half_nm + edge_keepout_nm
        x_max = edge_bb.GetRight() - edge_stroke_half_nm - edge_keepout_nm
        y_max = edge_bb.GetBottom() - edge_stroke_half_nm - edge_keepout_nm
        if x_max <= x_min or y_max <= y_min:
            return {
                "success": False,
                "message": "Edge clearance too large for this board",
                "errorDetails": "Reduce edgeClearance or increase the outline",
            }

        # --- Gather obstacles (everything on a non-GND net we must dodge) ---
        # Tracks: list of (x1, y1, x2, y2, half_width)
        # Vias:   list of (cx, cy, radius)
        # Pads:   list of (cx, cy, half_extent) — bbox-circle approximation
        obstacle_tracks: List[tuple] = []
        obstacle_vias: List[tuple] = []
        obstacle_pads: List[tuple] = []

        for track in self.board.GetTracks():
            if track.GetNetCode() == gnd_net_code:
                continue
            # The rest of this module uses the string-class check rather
            # than `isinstance(track, pcbnew.PCB_VIA)` — match that for
            # consistency and because isinstance against the SWIG type
            # works unreliably under test stubs.
            is_via = False
            try:
                is_via = track.GetClass() == "PCB_VIA"
            except Exception:
                is_via = False
            if is_via:
                pos = track.GetPosition()
                width = track.GetWidth()
                drill = 0
                try:
                    drill = track.GetDrill()
                except Exception:
                    pass
                obstacle_vias.append((pos.x, pos.y, max(width, drill) // 2))
            else:
                s, e = track.GetStart(), track.GetEnd()
                obstacle_tracks.append((s.x, s.y, e.x, e.y, track.GetWidth() // 2))

        for fp in self.board.GetFootprints():
            for pad in fp.Pads():
                pad_net = pad.GetNetCode()
                if pad_net == gnd_net_code:
                    continue
                p = pad.GetPosition()
                sz = pad.GetSize()
                half_extent = max(sz.x, sz.y) // 2
                # Inflate for pad-shape variation (round vs rect)
                obstacle_pads.append((p.x, p.y, half_extent))

        logger.info(
            f"add_gnd_stitching_vias: {len(obstacle_tracks)} tracks, "
            f"{len(obstacle_vias)} vias, {len(obstacle_pads)} pads to avoid"
        )

        # --- GND zones + fill-order guard ---
        # A stitching via only stops "dangling" when it lands on GND copper
        # that is actually filled.  If the GND net has zones but none of them
        # are filled, placing vias now yields via_dangling + copper_edge
        # DRC errors (observed on the GD32 E2E: 13 -> 42 after stitching two
        # unfilled GND zones).  Refuse by default; ``force`` overrides.  Boards
        # with no GND zones at all skip the guard — GND is carried by
        # tracks/pours elsewhere and there is nothing to fill.
        gnd_zones = [z for z in self.board.Zones() if z.GetNetCode() == gnd_net_code]

        def _zone_is_filled(z: Any) -> bool:
            try:
                return bool(z.IsFilled())
            except Exception:
                return False

        filled_gnd_zones = [z for z in gnd_zones if _zone_is_filled(z)]
        zones_unfilled = bool(gnd_zones) and not filled_gnd_zones
        if zones_unfilled and not force:
            return {
                "success": False,
                "message": "GND zones are not filled",
                "needs_zone_fill": True,
                "errorDetails": (
                    f"Net '{gnd_net_name}' has {len(gnd_zones)} zone(s) but none are "
                    "filled, so stitching vias would dangle and violate "
                    "copper_edge_clearance. Fill the zones first — "
                    "copper_pour(action=refill, force=true) or fill in KiCad — "
                    "or pass force=true to place anyway."
                ),
                "summary": {
                    "gnd_net": gnd_net_name,
                    "gnd_zone_count": len(gnd_zones),
                    "filled_gnd_zone_count": 0,
                },
            }

        # When filled GND zones exist, restrict placement to inside their
        # filled polygons regardless of the requested strategy — a via off the
        # fill has no copper to connect to and dangles.  ``in_zones`` requests
        # this explicitly; here it becomes the default whenever a real fill is
        # present, which is what keeps the vias from dangling.
        restrict_to_fill = bool(filled_gnd_zones)
        zone_membership_zones = filled_gnd_zones or gnd_zones

        def in_any_gnd_zone(x_nm: int, y_nm: int) -> bool:
            pt = pcbnew.VECTOR2I(x_nm, y_nm)
            for z in zone_membership_zones:
                try:
                    if z.HitTestFilledArea(z.GetLayer(), pt, 0):
                        return True
                except Exception:
                    # API variant: take any zone in whose bbox we sit
                    bb = z.GetBoundingBox()
                    if (
                        bb.GetLeft() <= x_nm <= bb.GetRight()
                        and bb.GetTop() <= y_nm <= bb.GetBottom()
                    ):
                        return True
            return False

        # --- Collision check closure (all-layer) ---
        placed_via_centres: List[tuple] = []  # nm coords of vias placed this call

        def can_place(x_nm: int, y_nm: int) -> bool:
            # Boundary
            if not (x_min <= x_nm <= x_max and y_min <= y_nm <= y_max):
                return False

            # Distance against placed-this-call vias (avoid clumping)
            min_self = via_size_nm + clearance_nm
            for ox, oy in placed_via_centres:
                dx = x_nm - ox
                dy = y_nm - oy
                if dx * dx + dy * dy < min_self * min_self:
                    return False

            # Tracks
            for x1, y1, x2, y2, hw in obstacle_tracks:
                min_dist = via_radius_nm + hw + clearance_nm
                if _point_to_segment_distance_nm(x_nm, y_nm, x1, y1, x2, y2) < min_dist:
                    return False

            # Vias
            for vx, vy, vr in obstacle_vias:
                min_dist = via_radius_nm + vr + clearance_nm
                dx = x_nm - vx
                dy = y_nm - vy
                if dx * dx + dy * dy < min_dist * min_dist:
                    return False

            # Pads (bbox-circle approximation, intentionally conservative)
            for px, py, ph in obstacle_pads:
                min_dist = via_radius_nm + ph + clearance_nm
                dx = x_nm - px
                dy = y_nm - py
                if dx * dx + dy * dy < min_dist * min_dist:
                    return False

            return True

        # --- Build candidate list per strategy ---
        candidates: List[tuple] = []
        if "around_refs" in strategies:
            if not densify_refs:
                logger.warning("around_refs strategy requested but densifyRefs is empty")
            fps_by_ref = {fp.GetReference(): fp for fp in self.board.GetFootprints()}
            for ref in densify_refs:
                fp = fps_by_ref.get(ref)
                if not fp:
                    logger.warning(f"densifyRefs: {ref!r} not found")
                    continue
                cx = fp.GetPosition().x
                cy = fp.GetPosition().y
                for dx in range(-densify_radius, densify_radius + 1):
                    for dy in range(-densify_radius, densify_radius + 1):
                        candidates.append((cx + dx * spacing_nm, cy + dy * spacing_nm))

        if "grid" in strategies or "in_zones" in strategies:
            x = x_min
            while x <= x_max:
                y = y_min
                while y <= y_max:
                    candidates.append((x, y))
                    y += spacing_nm
                x += spacing_nm

        # --- Filter + place ---
        in_zones_only = ("in_zones" in strategies) or restrict_to_fill
        skipped_by_zone = 0
        skipped_by_collision = 0
        placed_meta: List[Dict[str, Any]] = []

        for cx, cy in candidates:
            if max_vias is not None and len(placed_meta) >= max_vias:
                break
            if in_zones_only and not in_any_gnd_zone(cx, cy):
                skipped_by_zone += 1
                continue
            if not can_place(cx, cy):
                skipped_by_collision += 1
                continue
            placed_via_centres.append((cx, cy))
            placed_meta.append(
                {
                    "x": round(cx / scale, 3),
                    "y": round(cy / scale, 3),
                    "unit": "mm",
                }
            )

        # --- Write to board ---
        if not dry_run:
            f_cu = self.board.GetLayerID("F.Cu")
            b_cu = self.board.GetLayerID("B.Cu")
            for cx, cy in placed_via_centres:
                via = pcbnew.PCB_VIA(self.board)
                via.SetPosition(pcbnew.VECTOR2I(cx, cy))
                via.SetWidth(via_size_nm)
                via.SetDrill(via_drill_nm)
                via.SetLayerPair(f_cu, b_cu)
                via.SetNet(gnd_net)
                self.board.Add(via)

        return {
            "success": True,
            "placed": placed_meta,
            "summary": {
                "gnd_net": gnd_net_name,
                "placed_count": len(placed_meta),
                "candidates_evaluated": len(candidates),
                "skipped_by_zone_membership": skipped_by_zone,
                "skipped_by_collision": skipped_by_collision,
                "strategies": strategies,
                "dry_run": dry_run,
                "via_size_mm": via_size_mm,
                "via_drill_mm": via_drill_mm,
                "clearance_mm": clearance_mm,
                "spacing_mm": spacing_mm,
                "edge_clearance_mm": edge_clearance_mm,
                "edge_keepout_mm": round(edge_keepout_nm / scale, 4),
                "gnd_zone_count": len(gnd_zones),
                "filled_gnd_zone_count": len(filled_gnd_zones),
                "restricted_to_fill": restrict_to_fill,
                "forced": force,
            },
        }
