"""Module-level pure helpers for the routing commands.

Split out of the former monolithic commands/routing.py so the mixin
modules can share them without a circular import.
"""

from typing import Any, Dict, List, Optional

# Sane upper bound (mm) for any user-supplied copper width — trace width or a
# net class's trace width.  A track wider than this is almost certainly a
# fat-fingered value (999 mm was seen in the wild, wider than the whole board);
# reject it with a clear, unit-named message instead of silently creating a
# giant copper slab.  Generous on purpose so legitimate power/bus widths pass.
MAX_TRACK_WIDTH_MM = 50.0


def _track_width_error(width: Any, field: str = "width") -> Optional[Dict[str, Any]]:
    """Validate a user-supplied track width (mm); return a refusal dict or None.

    Bounds are ``0 < width <= MAX_TRACK_WIDTH_MM``.  ``None`` is treated as
    "not supplied" (callers only validate an explicitly-passed width) and
    passes.  Non-numeric, non-positive, or over-cap values are refused with a
    truthful ``VALIDATION`` errorCode and a message naming the limit and unit.
    Shared by route_trace, route_smart (explicit width) and create_netclass
    (traceWidth) so the bound is identical everywhere a width is accepted.
    """
    if width is None:
        return None
    try:
        w = float(width)
    except (TypeError, ValueError):
        return {
            "success": False,
            "message": f"{field} must be a number in mm",
            "errorCode": "VALIDATION",
        }
    if w <= 0:
        return {
            "success": False,
            "message": f"{field} must be greater than 0 mm (got {w:g} mm)",
            "errorCode": "VALIDATION",
        }
    if w > MAX_TRACK_WIDTH_MM:
        return {
            "success": False,
            "message": (
                f"{field} of {w:g} mm is out of range — the maximum allowed is "
                f"{MAX_TRACK_WIDTH_MM:g} mm. Pass a width in mm within "
                f"(0, {MAX_TRACK_WIDTH_MM:g}]."
            ),
            "errorCode": "VALIDATION",
        }
    return None


def _refuse_with_obstacles(
    from_ref: str,
    from_pad: str,
    to_ref: str,
    to_pad: str,
    obstacles: List[str],
) -> Dict[str, Any]:
    """Refusal response for ``route_pad_to_pad`` when a straight segment
    would cross a third-party pad.

    Surfaced as ``success: False`` with ``hasObstacles: True`` so the
    agent can distinguish this recoverable, geometry-only failure from
    an "actually broken" error.  Carries the obstacle list and a
    pointer to the ``force`` opt-out so the caller can either reroute
    manually or override knowing the cost (DRC violations).
    """
    return {
        "success": False,
        "hasObstacles": True,
        # Truthful code: this is a deliberate geometry refusal (the straight
        # trace would short through other pads), not an internal error — so an
        # agent can branch on SHORT_REFUSED and offer force=true / manual reroute.
        "errorCode": "SHORT_REFUSED",
        "obstacleCount": len(obstacles),
        "obstaclesCrossed": obstacles,
        "message": (
            f"Refused: straight trace from {from_ref}.{from_pad} → "
            f"{to_ref}.{to_pad} crosses {len(obstacles)} other pad(s). "
            "Inserting it would short the trace through them and produce "
            "tracks_crossing / net-shorting DRC violations."
        ),
        "hint": (
            "route_pad_to_pad is a straight-line connector, not an "
            "autorouter — it has no obstacle avoidance.  Either plan the "
            "path manually as several route_trace segments that go around "
            "the obstacles, or call again with force=true to insert "
            "anyway (you will then need to fix the resulting DRC errors)."
        ),
    }


def _nets_equivalent(a: Optional[str], b: Optional[str]) -> bool:
    """Whether two net names refer to the same net.

    Tolerates the sheet-root ``/`` prefix so a caller passing the bare name
    (``GND``) is not falsely flagged as shorting the board's hierarchical
    ``/GND``.  Empty / ``None`` never matches anything.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    return a.lstrip("/") == b.lstrip("/")


def _endpoint_conflict_messages(endpoints: List[Any], net: str, pad_boxes: List[Any]) -> List[str]:
    """Pure cross-net endpoint check shared by the SWIG and IPC paths.

    ``endpoints``   ``[(x, y), ...]`` — the trace's endpoints.
    ``net``         the net the trace will carry.
    ``pad_boxes``   ``[(ref, pad_num, pad_net, (left, top, right, bottom)), ...]``
                    in the SAME coordinate space as ``endpoints`` (all nm or
                    all mm — callers keep the units consistent).

    Returns one message per distinct foreign-net pad that contains an
    endpoint — a pad whose non-empty net differs from ``net``, i.e. a hard
    short.  Empty when ``net`` is falsy (nothing to compare) or nothing
    conflicts.  Numeric-only comparison (guards against MagicMock coords in
    tests / dehydrated SWIG proxies).
    """
    conflicts: List[str] = []
    if not net:
        return conflicts
    seen = set()
    for ref, pad_num, pad_net, box in pad_boxes:
        if not pad_net or _nets_equivalent(pad_net, net):
            continue
        try:
            left, top, right, bottom = box
        except (TypeError, ValueError):
            continue
        for point in endpoints:
            try:
                px, py = point
            except (TypeError, ValueError):
                continue
            if not (isinstance(px, (int, float)) and isinstance(py, (int, float))):
                continue
            if left <= px <= right and top <= py <= bottom:
                key = (ref, pad_num, pad_net)
                if key not in seen:
                    seen.add(key)
                    conflicts.append(
                        f"Trace endpoint lands on pad {ref}.{pad_num} (net "
                        f"'{pad_net}') but the trace net is '{net}' — connecting "
                        f"them would short '{net}' to '{pad_net}'."
                    )
                break
    return conflicts


def endpoint_net_conflicts(board: Any, endpoints: List[Any], net: Optional[str]) -> List[str]:
    """Cross-net shorts on a SWIG board: endpoints landing on a foreign pad.

    ``endpoints`` are ``(x_nm, y_nm)`` points (pad centres or raw trace
    endpoints); ``net`` is the net the trace will carry.  Scans every pad's
    bounding box (nm) via the shared :func:`_endpoint_conflict_messages` core
    and returns human-readable conflict strings — empty when clean or ``net``
    is falsy.  Never raises: pad iteration is best-effort (a board with no
    footprints, or a non-iterable mock, yields ``[]``).
    """
    if not net:
        return []
    pad_boxes: List[Any] = []
    try:
        for fp in board.GetFootprints():
            try:
                ref = fp.GetReference()
            except Exception:
                ref = "?"
            for pad in fp.Pads():
                try:
                    pad_num = str(pad.GetNumber())
                except Exception:
                    continue
                if not pad_num:
                    continue  # mechanical / unnumbered pad — no electrical role
                try:
                    pad_net = pad.GetNetname()
                except Exception:
                    continue
                if not pad_net or _nets_equivalent(pad_net, net):
                    continue
                try:
                    bbox = pad.GetBoundingBox()
                    box = (
                        float(bbox.GetLeft()),
                        float(bbox.GetTop()),
                        float(bbox.GetRight()),
                        float(bbox.GetBottom()),
                    )
                except Exception:
                    continue
                pad_boxes.append((ref, pad_num, pad_net, box))
    except Exception:
        return []
    return _endpoint_conflict_messages(endpoints, net, pad_boxes)


def _refuse_cross_net_short(net: Optional[str], conflicts: List[str]) -> Dict[str, Any]:
    """Refusal for a routing op whose endpoint lands on a different-net pad.

    Distinct ``errorCode`` ``CROSS_NET_SHORT`` (vs ``SHORT_REFUSED`` for a
    third-pad crossing) so an agent can tell "you asked me to connect two
    different nets" apart from "the straight line clips a bystander pad".
    ``force=true`` overrides.
    """
    return {
        "success": False,
        "hasCrossNetShort": True,
        "errorCode": "CROSS_NET_SHORT",
        "conflictCount": len(conflicts),
        "crossNetConflicts": conflicts,
        "message": (
            f"Refused: trace net '{net}' would be joined to a different net at "
            f"{len(conflicts)} endpoint(s) — a hard short. " + " ".join(conflicts)
        ),
        "hint": (
            "The two endpoints are on different nets; connecting them shorts "
            "them together and produces net-shorting DRC violations. Check the "
            "pad net assignments, or pass force=true to route anyway (you must "
            "then fix the resulting short)."
        ),
    }


def _point_to_segment_distance_nm(px: int, py: int, x1: int, y1: int, x2: int, y2: int) -> float:
    """Shortest distance (nm) from point (px,py) to segment (x1,y1)-(x2,y2).

    Pure integer-friendly variant of the standard projection formula;
    used in the hot loop of GND-stitching collision detection so we
    avoid building VECTOR2I objects per call.
    """
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        ex: float = px - x1
        ey: float = py - y1
        return (ex * ex + ey * ey) ** 0.5
    denom = dx * dx + dy * dy
    t = ((px - x1) * dx + (py - y1) * dy) / denom
    if t < 0:
        t = 0
    elif t > 1:
        t = 1
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    ex = px - proj_x
    ey = py - proj_y
    return (ex * ex + ey * ey) ** 0.5
