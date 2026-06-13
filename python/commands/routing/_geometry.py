"""Shared geometry / pad helpers for RoutingCommands.

Split out of the former monolithic commands/routing.py."""

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

import pcbnew

logger = logging.getLogger("kicad_interface")


class GeometryMixin:
    def _netclass_track_width_mm(self, pad: Any) -> Optional[float]:
        """Return the netclass-suggested track width in mm for ``pad``'s net.

        Falls back to the board's current default track width when no
        netclass is set (or kicad's design-settings API doesn't expose
        a netclass-specific track width on the running SWIG build).
        Returning ``None`` lets the caller leave width unset and have
        route_trace pick GetCurrentTrackWidth() itself.
        """
        try:
            net = pad.GetNet()
            if net is None:
                return None
            nc = net.GetNetClass()
            if nc is None:
                return None
            getter = getattr(nc, "GetTrackWidth", None)
            if not callable(getter):
                return None
            width_nm = int(getter())
            if width_nm > 0:
                return width_nm / 1_000_000.0
        except Exception:
            # SWIG can raise on dehydrated proxies; route_trace falls back
            # to GetCurrentTrackWidth() when width is None.
            return None
        return None

    def _pads_intersecting_segment(
        self,
        start_pos: Any,
        end_pos: Any,
        exclude_pad_keys: Optional[Set[Tuple[str, str]]] = None,
    ) -> List[str]:
        """Return a list of warnings naming pads the segment would cross.

        ``exclude_pad_keys`` is a set of ``(footprint_ref, pad_number)``
        tuples — usually the trace's own endpoints, which would otherwise
        appear in every warning because the trace literally starts and
        ends inside them.  Identification by ``(ref, num)`` (not by
        ``id(pad)``) is required: SWIG creates fresh Python proxy
        objects for the same C++ pad on every ``fp.Pads()`` iteration,
        so ``id()`` would never match across calls — every trace used
        to report its own start/end pads in the warning list.

        Uses an axis-aligned bbox vs. segment intersection test — coarse
        but cheap, and good enough to flag the "trace goes straight
        through another pad" case the user reported.
        """
        warnings: List[str] = []
        exclude = exclude_pad_keys or set()
        try:
            sx, sy = float(start_pos.x), float(start_pos.y)
            ex, ey = float(end_pos.x), float(end_pos.y)
            # Quick reject for zero-length segment
            if sx == ex and sy == ey:
                return warnings
            for fp in self.board.GetFootprints():
                ref = fp.GetReference()
                for pad in fp.Pads():
                    pad_num = str(pad.GetNumber())
                    if not pad_num:
                        # Unnumbered pads (mounting holes, fiducials,
                        # NPTH) have no electrical role — crossing
                        # them isn't a routing problem, and the
                        # generated warning string `"MH1."` (trailing
                        # dot, empty number) is ugly UX besides.
                        continue
                    if (ref, pad_num) in exclude:
                        continue
                    try:
                        bbox = pad.GetBoundingBox()
                    except Exception:
                        continue
                    if self._segment_intersects_bbox(sx, sy, ex, ey, bbox):
                        warnings.append(
                            f"Trace segment passes through {ref}.{pad_num} "
                            f"— consider routing around or via a different layer"
                        )
        except Exception:
            # Pad iteration can raise on partial board state; treat the
            # warning step as best-effort.
            return warnings
        return warnings

    @staticmethod
    def _segment_intersects_bbox(sx: float, sy: float, ex: float, ey: float, bbox: Any) -> bool:
        """Liang-Barsky-ish clip: does segment (sx,sy)-(ex,ey) touch bbox?"""
        try:
            x_min = float(bbox.GetLeft())
            x_max = float(bbox.GetRight())
            y_min = float(bbox.GetTop())
            y_max = float(bbox.GetBottom())
        except Exception:
            return False
        dx = ex - sx
        dy = ey - sy
        t_min, t_max = 0.0, 1.0
        for p, q in ((-dx, sx - x_min), (dx, x_max - sx), (-dy, sy - y_min), (dy, y_max - sy)):
            if p == 0:
                if q < 0:
                    return False
                continue
            r = q / p
            if p < 0:
                if r > t_max:
                    return False
                if r > t_min:
                    t_min = r
            else:
                if r < t_min:
                    return False
                if r < t_max:
                    t_max = r
        return True

    def _get_point(self, point_spec: Dict[str, Any]) -> pcbnew.VECTOR2I:
        """Convert point specification to KiCAD point"""
        if "x" in point_spec and "y" in point_spec:
            scale = (
                1000000
                if point_spec.get("unit", "mm") == "mm"
                else (25400 if point_spec.get("unit", "mm") == "mil" else 25400000)
            )
            x_nm = int(point_spec["x"] * scale)
            y_nm = int(point_spec["y"] * scale)
            return pcbnew.VECTOR2I(x_nm, y_nm)
        elif "pad" in point_spec and "componentRef" in point_spec:
            module = self.board.FindFootprintByReference(point_spec["componentRef"])
            if module:
                pad = module.FindPadByName(point_spec["pad"])
                if pad:
                    return pad.GetPosition()
        raise ValueError("Invalid point specification")

    def _point_to_track_distance(self, point: pcbnew.VECTOR2I, track: pcbnew.PCB_TRACK) -> float:
        """Calculate distance from point to track segment"""
        start = track.GetStart()
        end = track.GetEnd()

        # Vector from start to end
        v = pcbnew.VECTOR2I(end.x - start.x, end.y - start.y)
        # Vector from start to point
        w = pcbnew.VECTOR2I(point.x - start.x, point.y - start.y)

        # Length of track squared
        c1 = v.x * v.x + v.y * v.y
        if c1 == 0:
            return self._point_distance(point, start)

        # Projection coefficient
        c2 = float(w.x * v.x + w.y * v.y) / c1

        if c2 < 0:
            return self._point_distance(point, start)
        elif c2 > 1:
            return self._point_distance(point, end)

        # Point on line
        proj = pcbnew.VECTOR2I(int(start.x + c2 * v.x), int(start.y + c2 * v.y))
        return self._point_distance(point, proj)

    def _point_distance(self, p1: pcbnew.VECTOR2I, p2: pcbnew.VECTOR2I) -> float:
        """Calculate distance between two points"""
        dx = p1.x - p2.x
        dy = p1.y - p2.y
        return (dx * dx + dy * dy) ** 0.5
