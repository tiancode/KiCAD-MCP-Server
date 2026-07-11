"""Connectivity-driven auto-placement heuristic (pure algorithm — NO pcbnew).

This module operates on plain data (dataclasses / dicts) so the orchestrator
can feed it from either the SWIG or the IPC backend, and unit tests need no
stubs.  All units are millimetres; positions are component *centers*.

The placement strategy is a greedy connectivity heuristic:

1. Build an affinity graph: the edge weight between two components is the
   number of nets they share, excluding power nets (power nets connect
   everything and would collapse the layout).
2. Seed the layout with the highest-total-affinity component at the area
   center.  Fixed components keep their positions and still exert affinity.
3. Greedily place the component with the highest summed affinity to the
   already-placed set at the free grid position that minimizes
   ``sum(affinity x distance-to-partner-center)``, searched over spiral
   rings of grid candidates around the affinity-weighted partner centroid.
4. Decoupling caps are placed immediately after their IC, in the closest
   free ring around it, overriding general affinity.
5. Zero-affinity components are packed row-wise into the remaining space.
6. The algorithm never fails hard: components that cannot fit are reported
   in ``unplaced`` with a reason and processing continues.
"""

import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

_EPS = 1e-9

_DEFAULT_POWER_NET_PATTERNS: Tuple[str, ...] = (
    "GND",
    "VCC",
    "VDD",
    "+3V3",
    "+5V",
    "VSS",
    "+12V",
    "-12V",
)

#: Net-name tokens that mark a net as "ground-ish" for decoupling detection.
_GROUND_NET_TOKENS: Tuple[str, ...] = ("GND", "VSS")

_CAP_REF_RE = re.compile(r"C\d+\Z")
_IC_REF_RE = re.compile(r"U\d+\Z")
_CAP_CODE_RE = re.compile(r"\d{3}\Z")
_CAP_VALUE_RE = re.compile(r"(\d+(?:\.\d+)?|\.\d+)\s*(p|n|u|µ|μ|m)?\s*F?\Z", re.IGNORECASE)

#: Largest capacitance (farads) still considered a decoupling cap: 10 uF.
_MAX_DECOUPLING_FARADS = 10e-6

Point = Tuple[float, float]
Rect = Tuple[float, float, float, float]  # (min_x, min_y, max_x, max_y)


@dataclass
class PlaceableComponent:
    """One footprint as seen by the placement algorithm (all units mm)."""

    reference: str
    width: float  # courtyard/bbox width (mm), already rotation-normalized
    height: float
    nets: FrozenSet[str]  # nets touched by this component's pads (power nets included)
    fixed: bool = False  # already placed & locked: keep position
    x: Optional[float] = None  # current position (center), None if unplaced
    y: Optional[float] = None
    is_decoupling: bool = False  # small cap (value pattern 100n/1u/10u..., ref C*)
    decouples: Optional[str] = None  # reference of the IC it should hug, may be None


def _is_power_net(net: str, patterns: Tuple[str, ...]) -> bool:
    """True when *net* matches (contains, case-insensitive) any power pattern."""
    upper = net.upper()
    return any(p.upper() in upper for p in patterns)


def _is_ground_net(net: str) -> bool:
    """True when *net* looks like a ground net (GND/VSS family)."""
    return _is_power_net(net, _GROUND_NET_TOKENS)


def _parse_capacitance_farads(value: Any) -> Optional[float]:
    """Parse a capacitor value string into farads, or None if unparseable.

    Handles "100n", "0.1u", "1µF", "10uF", "100nF", plain numbers, and
    3-digit EIA codes ("104" -> 10 x 10^4 pF = 100 nF).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _CAP_CODE_RE.match(text):
        # EIA code: two significant digits x 10^exponent, in pF.
        return float(int(text[:2]) * (10 ** int(text[2]))) * 1e-12
    match = _CAP_VALUE_RE.match(text)
    if not match:
        return None
    number = float(match.group(1))
    suffix = (match.group(2) or "").lower().replace("µ", "u").replace("μ", "u")
    multiplier = {"": 1.0, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3}[suffix]
    return number * multiplier


def detect_decoupling(components: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """Identify decoupling-cap candidates and the IC each one should hug.

    Args:
        components: Raw dicts ``{"reference", "value", "nets": set(...)}``.

    Returns:
        ``{cap_ref: ic_ref | None}`` containing only decoupling-cap
        candidates: reference matches ``C\\d+`` and value parses as a
        capacitance <= 10 uF.  The IC is the ``U*`` component sharing the
        cap's non-ground net with the fewest other components (the local
        power rail); ``None`` when no ``U*`` shares any non-ground net.
    """
    nets_by_ref: Dict[str, Set[str]] = {}
    values_by_ref: Dict[str, Any] = {}
    members_by_net: Dict[str, Set[str]] = {}
    for comp in components:
        ref = str(comp["reference"])
        nets: Iterable[str] = comp.get("nets") or ()
        net_set = set(nets)
        nets_by_ref[ref] = net_set
        values_by_ref[ref] = comp.get("value")
        for net in net_set:
            members_by_net.setdefault(net, set()).add(ref)

    result: Dict[str, Optional[str]] = {}
    for ref in sorted(nets_by_ref):
        if not _CAP_REF_RE.match(ref):
            continue
        farads = _parse_capacitance_farads(values_by_ref[ref])
        if farads is None or farads > _MAX_DECOUPLING_FARADS * (1.0 + _EPS):
            continue
        best: Optional[Tuple[int, str, str]] = None
        for net in sorted(nets_by_ref[ref]):
            if _is_ground_net(net):
                continue
            members = members_by_net.get(net, set())
            for other in sorted(members):
                if other == ref or not _IC_REF_RE.match(other):
                    continue
                key = (len(members), net, other)
                if best is None or key < best:
                    best = key
        result[ref] = best[2] if best is not None else None
    return result


class _Placer:
    """Occupancy bookkeeping and grid-position search for one placement run."""

    #: After the first feasible ring, keep searching this many further rings
    #: for a lower-cost position before committing.
    _EXTRA_RINGS = 6

    def __init__(
        self, origin: Point, size: Tuple[float, float], spacing: float, grid: float
    ) -> None:
        self.ox, self.oy = float(origin[0]), float(origin[1])
        self.bw, self.bh = float(size[0]), float(size[1])
        self.spacing = float(spacing)
        self.grid = max(float(grid), 1e-3)
        self.rects: List[Rect] = []

    def register(self, x: float, y: float, width: float, height: float) -> None:
        """Record a placed courtyard rect (center + size)."""
        half_w, half_h = width / 2.0, height / 2.0
        self.rects.append((x - half_w, y - half_h, x + half_w, y + half_h))

    def _fits(self, x: float, y: float, width: float, height: float) -> bool:
        """True when the courtyard at (x, y) is inside the area and clear of
        every placed courtyard by at least ``spacing`` on one axis."""
        half_w, half_h = width / 2.0, height / 2.0
        min_x, min_y = x - half_w, y - half_h
        max_x, max_y = x + half_w, y + half_h
        if min_x < self.ox - _EPS or min_y < self.oy - _EPS:
            return False
        if max_x > self.ox + self.bw + _EPS or max_y > self.oy + self.bh + _EPS:
            return False
        gap = self.spacing - _EPS
        for o_min_x, o_min_y, o_max_x, o_max_y in self.rects:
            if (
                min_x < o_max_x + gap
                and o_min_x < max_x + gap
                and min_y < o_max_y + gap
                and o_min_y < max_y + gap
            ):
                return False
        return True

    def _snap(self, value: float, origin: float) -> float:
        return origin + round((value - origin) / self.grid) * self.grid

    @staticmethod
    def _ring_offsets(ring: int) -> List[Tuple[int, int]]:
        """Grid-step offsets whose Chebyshev distance equals *ring*."""
        if ring == 0:
            return [(0, 0)]
        points: List[Tuple[int, int]] = []
        for i in range(-ring, ring + 1):
            points.append((i, -ring))
            points.append((i, ring))
        for j in range(-ring + 1, ring):
            points.append((-ring, j))
            points.append((ring, j))
        points.sort(key=lambda p: (p[1], p[0]))
        return points

    def search(
        self,
        target: Point,
        width: float,
        height: float,
        cost_fn: Callable[[float, float], float],
    ) -> Optional[Point]:
        """Best free grid position near *target* by *cost_fn* (spiral rings).

        Scans rings of grid candidates around the snapped target; after the
        first feasible ring, a few extra rings are examined and the lowest
        cost position wins (tie-break: smaller y, then smaller x).  Returns
        None when no candidate anywhere in the area fits.
        """
        tx = self._snap(target[0], self.ox)
        ty = self._snap(target[1], self.oy)
        max_dx = max(abs(tx - self.ox), abs(self.ox + self.bw - tx))
        max_dy = max(abs(ty - self.oy), abs(self.oy + self.bh - ty))
        max_ring = int(math.ceil(max(max_dx, max_dy) / self.grid)) + 1
        best: Optional[Tuple[float, float, float]] = None  # (cost, y, x)
        first_feasible: Optional[int] = None
        for ring in range(max_ring + 1):
            if first_feasible is not None and ring > first_feasible + self._EXTRA_RINGS:
                break
            for step_x, step_y in self._ring_offsets(ring):
                cx = tx + step_x * self.grid
                cy = ty + step_y * self.grid
                if not self._fits(cx, cy, width, height):
                    continue
                key = (cost_fn(cx, cy), cy, cx)
                if best is None or key < best:
                    best = key
                if first_feasible is None:
                    first_feasible = ring
        if best is None:
            return None
        return (best[2], best[1])

    def row_pack(self, width: float, height: float) -> Optional[Point]:
        """First free grid position scanning row-wise from the top-left."""
        steps_x = int(math.floor(self.bw / self.grid + _EPS))
        steps_y = int(math.floor(self.bh / self.grid + _EPS))
        for jy in range(steps_y + 1):
            cy = self.oy + jy * self.grid
            for ix in range(steps_x + 1):
                cx = self.ox + ix * self.grid
                if self._fits(cx, cy, width, height):
                    return (cx, cy)
        return None


def _weighted_cost(partners: List[Tuple[int, Point]]) -> Callable[[float, float], float]:
    """Cost = sum(affinity x euclidean distance to each partner center)."""

    def cost(x: float, y: float) -> float:
        return sum(w * math.hypot(x - px, y - py) for w, (px, py) in partners)

    return cost


def _point_cost(target: Point) -> Callable[[float, float], float]:
    """Cost = euclidean distance to a single target point."""

    def cost(x: float, y: float) -> float:
        return math.hypot(x - target[0], y - target[1])

    return cost


def _centroid(partners: List[Tuple[int, Point]]) -> Point:
    """Affinity-weighted centroid of partner positions."""
    total = float(sum(w for w, _ in partners))
    return (
        sum(w * p[0] for w, p in partners) / total,
        sum(w * p[1] for w, p in partners) / total,
    )


def _estimate_hpwl(
    components: List[PlaceableComponent],
    positions: Dict[str, Point],
    power_patterns: Tuple[str, ...],
) -> float:
    """Half-perimeter wirelength over non-power nets using component centers."""
    points_by_net: Dict[str, List[Point]] = {}
    for comp in components:
        pos = positions.get(comp.reference)
        if pos is None:
            continue
        for net in comp.nets:
            if _is_power_net(net, power_patterns):
                continue
            points_by_net.setdefault(net, []).append(pos)
    total = 0.0
    for points in points_by_net.values():
        if len(points) < 2:
            continue
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total


def auto_place(
    components: List[PlaceableComponent],
    *,
    board_origin: Tuple[float, float],
    board_size: Tuple[float, float],
    spacing_mm: float = 1.0,
    grid_mm: float = 0.5,
    cluster_power_nets: bool = False,
    power_net_patterns: Tuple[str, ...] = _DEFAULT_POWER_NET_PATTERNS,
) -> Dict[str, Any]:
    """Greedy connectivity-driven auto-placement (all units mm).

    Args:
        components: Components to place.  ``fixed`` components with a
            position keep it and still exert affinity on the rest.
        board_origin: Placement area top-left corner (mm).
        board_size: Placement area (width, height) in mm.
        spacing_mm: Minimum gap enforced between courtyards.
        grid_mm: Placement positions snap to this grid (relative to origin).
        cluster_power_nets: When False (default), power nets are excluded
            from the affinity graph — they connect everything and would
            collapse the layout.
        power_net_patterns: Case-insensitive substrings identifying power
            nets.

    Returns:
        ``{"success": True, "placements": [{"reference", "x", "y"}],
        "unplaced": [{"reference", "reason"}], "stats": {"placed",
        "unplaced", "estWirelengthBefore", "estWirelengthAfter"}}``.
        Placements include fixed components (unchanged).  Never fails hard:
        components that cannot fit are listed in ``unplaced`` with a reason.
    """
    comps: Dict[str, PlaceableComponent] = {c.reference: c for c in components}
    refs = sorted(comps)
    origin = (float(board_origin[0]), float(board_origin[1]))
    size = (float(board_size[0]), float(board_size[1]))

    # ------------------------------------------------------------------ 1.
    # Affinity graph (shared non-power nets).
    affinity: Dict[str, Dict[str, int]] = {r: {} for r in refs}
    for i, ref_a in enumerate(refs):
        for ref_b in refs[i + 1 :]:
            shared = comps[ref_a].nets & comps[ref_b].nets
            if not cluster_power_nets:
                shared = frozenset(n for n in shared if not _is_power_net(n, power_net_patterns))
            weight = len(shared)
            if weight:
                affinity[ref_a][ref_b] = weight
                affinity[ref_b][ref_a] = weight
    total_affinity = {r: sum(affinity[r].values()) for r in refs}

    placer = _Placer(origin, size, spacing_mm, grid_mm)
    placed: Dict[str, Point] = {}
    unplaced: List[Dict[str, str]] = []
    no_fit_reason = f"no free grid position in placement area ({size[0]} x {size[1]} mm)"

    def register(ref: str, x: float, y: float) -> None:
        placed[ref] = (x, y)
        placer.register(x, y, comps[ref].width, comps[ref].height)

    def area_of(ref: str) -> float:
        return comps[ref].width * comps[ref].height

    def placed_partners(ref: str) -> List[Tuple[int, Point]]:
        return [(w, placed[p]) for p, w in sorted(affinity[ref].items()) if p in placed]

    # ------------------------------------------------------------------ 2.
    # Fixed components stay put (still exert affinity via `placed`).
    for ref in refs:
        comp = comps[ref]
        if comp.fixed and comp.x is not None and comp.y is not None:
            register(ref, float(comp.x), float(comp.y))

    # ------------------------------------------------------------------ 4.
    # Decoupling caps are deferred and placed right after their IC.
    caps_by_ic: Dict[str, List[str]] = {}
    deferred: Set[str] = set()
    for ref in refs:
        comp = comps[ref]
        if ref in placed:
            continue
        if comp.is_decoupling and comp.decouples and comp.decouples in comps:
            if comp.decouples != ref:
                caps_by_ic.setdefault(comp.decouples, []).append(ref)
                deferred.add(ref)

    def place_caps_for(ic_ref: str) -> None:
        ic_pos = placed.get(ic_ref)
        if ic_pos is None:
            return
        for cap_ref in caps_by_ic.pop(ic_ref, []):
            cap = comps[cap_ref]
            pos = placer.search(ic_pos, cap.width, cap.height, _point_cost(ic_pos))
            if pos is not None:
                register(cap_ref, pos[0], pos[1])
            else:
                unplaced.append({"reference": cap_ref, "reason": no_fit_reason})
            deferred.discard(cap_ref)

    for ic_ref in sorted(caps_by_ic):
        if ic_ref in placed:
            place_caps_for(ic_ref)

    # ------------------------------------------------------------------ 3.
    # Greedy connectivity placement of the remaining pool.
    center = (origin[0] + size[0] / 2.0, origin[1] + size[1] / 2.0)
    pool: List[str] = [r for r in refs if r not in placed and r not in deferred]
    zero_affinity_queue: List[str] = []

    while pool:
        scored = sorted(
            (
                -sum(w for p, w in affinity[r].items() if p in placed),
                -area_of(r),
                r,
            )
            for r in pool
        )
        neg_score, _, ref = scored[0]
        if -neg_score > 0:
            partners = placed_partners(ref)
            target = _centroid(partners)
            cost_fn = _weighted_cost(partners)
        else:
            # No link to anything placed: seed a new cluster at the area
            # center, or drain the rest into the row-packing queue.
            seeds = [r for r in pool if total_affinity[r] > 0]
            if not seeds:
                zero_affinity_queue.extend(pool)
                break
            ref = min(seeds, key=lambda r: (-total_affinity[r], -area_of(r), r))
            target = center
            cost_fn = _point_cost(center)
        comp = comps[ref]
        pos = placer.search(target, comp.width, comp.height, cost_fn)
        if pos is not None:
            register(ref, pos[0], pos[1])
            place_caps_for(ref)
        else:
            unplaced.append({"reference": ref, "reason": no_fit_reason})
        pool.remove(ref)

    # Deferred caps whose IC never got placed fall back to the general flow.
    for ref in sorted(deferred):
        if ref in placed:
            continue
        partners = placed_partners(ref)
        if not partners:
            zero_affinity_queue.append(ref)
            continue
        comp = comps[ref]
        pos = placer.search(_centroid(partners), comp.width, comp.height, _weighted_cost(partners))
        if pos is not None:
            register(ref, pos[0], pos[1])
        else:
            unplaced.append({"reference": ref, "reason": no_fit_reason})

    # ------------------------------------------------------------------ 5.
    # Zero-affinity components: pack row-wise into the remaining space.
    for ref in sorted(set(zero_affinity_queue), key=lambda r: (-area_of(r), r)):
        comp = comps[ref]
        pos = placer.row_pack(comp.width, comp.height)
        if pos is not None:
            register(ref, pos[0], pos[1])
        else:
            unplaced.append({"reference": ref, "reason": no_fit_reason})

    # ------------------------------------------------------------------ 6.
    # Result + HPWL estimates.
    before: Optional[float] = None
    if all(c.x is not None and c.y is not None for c in components):
        initial = {c.reference: (float(c.x), float(c.y)) for c in components}  # type: ignore[arg-type]
        before = _estimate_hpwl(components, initial, power_net_patterns)
    after = _estimate_hpwl(components, placed, power_net_patterns)

    return {
        "success": True,
        "placements": [
            {"reference": ref, "x": placed[ref][0], "y": placed[ref][1]} for ref in sorted(placed)
        ],
        "unplaced": sorted(unplaced, key=lambda u: u["reference"]),
        "stats": {
            "placed": len(placed),
            "unplaced": len(unplaced),
            "estWirelengthBefore": before,
            "estWirelengthAfter": after,
        },
    }
