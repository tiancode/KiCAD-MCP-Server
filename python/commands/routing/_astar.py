"""Pure grid A* router backing the ``route_smart`` obstacle-avoiding tool.

This module is deliberately independent of ``pcbnew`` (stdlib only) so the
pathfinding core is unit-testable without a KiCAD installation.  The
orchestrating command extracts board geometry (pads, tracks, vias) into
:class:`RouteObstacle` rectangles, calls :func:`route_grid_astar`, and turns
the returned segments/vias back into real board items.

All units are millimetres (floats).  The search runs on a uniform grid over a
3D state space ``(x_index, y_index, layer_index)`` with 8-directional movement
on a layer and layer changes (through vias) between layers.
"""

from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

_SQRT2 = math.sqrt(2.0)
_EPS = 1e-9

# Fixed neighbour order (orthogonals first) so the search is deterministic.
_DIRECTIONS: Tuple[Tuple[int, int], ...] = (
    (1, 0),
    (-1, 0),
    (0, 1),
    (0, -1),
    (1, 1),
    (1, -1),
    (-1, 1),
    (-1, -1),
)

_State = Tuple[int, int, int]  # (x_index, y_index, layer_index)


@dataclass
class RouteObstacle:
    """Axis-aligned keep-out rectangle on a layer ('*' = all copper layers)."""

    x1: float
    y1: float
    x2: float
    y2: float
    layer: str  # e.g. "F.Cu", "B.Cu", or "*" for through-hole items
    net: Optional[str] = None  # obstacle's net; same-net obstacles are passable


@dataclass
class RouteResult:
    """Outcome of a grid A* routing attempt (all coordinates in mm)."""

    success: bool
    segments: List[Dict[str, Any]]  # [{"start": {"x","y"}, "end": {"x","y"}, "layer": str}]
    vias: List[Dict[str, Any]]  # [{"x", "y"}]
    length_mm: float
    message: str = ""
    explored: int = 0  # nodes expanded, for diagnostics


def _fail(message: str, explored: int = 0) -> RouteResult:
    """Build a failed RouteResult with an explanatory message."""
    return RouteResult(
        success=False,
        segments=[],
        vias=[],
        length_mm=0.0,
        message=message,
        explored=explored,
    )


def _blocking_obstacle(
    x: float,
    y: float,
    layer: str,
    obstacles: Sequence[RouteObstacle],
    net: Optional[str],
    margin: float,
) -> Optional[RouteObstacle]:
    """Return the first foreign-net obstacle whose inflated rect contains (x, y) on *layer*."""
    for ob in obstacles:
        if net is not None and ob.net == net:
            continue  # same-net obstacles are passable
        if ob.layer != "*" and ob.layer != layer:
            continue
        ox1, ox2 = min(ob.x1, ob.x2), max(ob.x1, ob.x2)
        oy1, oy2 = min(ob.y1, ob.y2), max(ob.y1, ob.y2)
        if ox1 - margin <= x <= ox2 + margin and oy1 - margin <= y <= oy2 + margin:
            return ob
    return None


def route_grid_astar(
    start: Tuple[float, float],
    end: Tuple[float, float],
    *,
    net: Optional[str],
    layers: Sequence[str],
    obstacles: Sequence[RouteObstacle],
    bounds: Tuple[float, float, float, float],
    grid_mm: float = 0.25,
    clearance_mm: float = 0.2,
    trace_width_mm: float = 0.25,
    via_cost: float = 20.0,
    max_nodes: int = 200_000,
) -> RouteResult:
    """Route from *start* to *end* with obstacle-avoiding grid A*.

    The search snaps both endpoints to a uniform ``grid_mm`` grid inside
    *bounds* and explores the 3D state space ``(x, y, layer)``: 8-directional
    moves on a layer (diagonals cost sqrt(2)) plus layer changes costing
    ``via_cost`` grid steps, each of which emits a through via (the via cell
    must be free on all layers).  A cell is blocked on a layer when it lies
    inside a foreign-net obstacle inflated by ``clearance_mm +
    trace_width_mm / 2``; obstacles on the routed *net* are passable.  Cells
    within one grid step of the endpoints are exempt from blocking so the
    route can escape dense pad fields.

    Returns a :class:`RouteResult`; on failure ``success`` is False and
    ``message`` explains why (endpoint outside bounds, blocked endpoint, node
    budget exhausted, or no path).
    """
    if not layers:
        return _fail("no routing layers supplied")
    if grid_mm <= 0:
        return _fail("grid_mm must be positive")

    bx1, bx2 = min(bounds[0], bounds[2]), max(bounds[0], bounds[2])
    by1, by2 = min(bounds[1], bounds[3]), max(bounds[1], bounds[3])
    sx, sy = start
    ex, ey = end

    if not (bx1 - _EPS <= sx <= bx2 + _EPS and by1 - _EPS <= sy <= by2 + _EPS):
        return _fail(f"start point ({sx:g}, {sy:g}) is outside the routable bounds")
    if not (bx1 - _EPS <= ex <= bx2 + _EPS and by1 - _EPS <= ey <= by2 + _EPS):
        return _fail(f"end point ({ex:g}, {ey:g}) is outside the routable bounds")

    nx = int(math.floor((bx2 - bx1) / grid_mm + _EPS)) + 1
    ny = int(math.floor((by2 - by1) / grid_mm + _EPS)) + 1

    def to_index(x: float, y: float) -> Tuple[int, int]:
        i = min(nx - 1, max(0, int(round((x - bx1) / grid_mm))))
        j = min(ny - 1, max(0, int(round((y - by1) / grid_mm))))
        return i, j

    si, sj = to_index(sx, sy)
    ei, ej = to_index(ex, ey)

    margin = clearance_mm + trace_width_mm / 2.0
    layer_index = {name: k for k, name in enumerate(layers)}
    n_layers = len(layers)

    # Endpoint blockage checks use the exact points, before any exemption.
    blocker = _blocking_obstacle(sx, sy, layers[0], obstacles, net, margin)
    if blocker is not None:
        owner = f" by an obstacle on net '{blocker.net}'" if blocker.net else " by an obstacle"
        return _fail(f"start point ({sx:g}, {sy:g}) is blocked on {layers[0]}{owner}")
    end_blockers = [_blocking_obstacle(ex, ey, name, obstacles, net, margin) for name in layers]
    if all(b is not None for b in end_blockers):
        first = end_blockers[0]
        owner = (
            f" by an obstacle on net '{first.net}'"
            if first is not None and first.net
            else " by an obstacle"
        )
        return _fail(f"end point ({ex:g}, {ey:g}) is blocked on all layers{owner}")

    # Rasterize each foreign-net obstacle's inflated rect into per-layer
    # blocked-cell sets once, so neighbour expansion is a set lookup.
    blocked: List[Set[Tuple[int, int]]] = [set() for _ in layers]
    for ob in obstacles:
        if net is not None and ob.net == net:
            continue
        if ob.layer != "*" and ob.layer not in layer_index:
            continue
        ox1, ox2 = min(ob.x1, ob.x2), max(ob.x1, ob.x2)
        oy1, oy2 = min(ob.y1, ob.y2), max(ob.y1, ob.y2)
        i0 = max(0, int(math.ceil((ox1 - margin - bx1) / grid_mm - _EPS)))
        i1 = min(nx - 1, int(math.floor((ox2 + margin - bx1) / grid_mm + _EPS)))
        j0 = max(0, int(math.ceil((oy1 - margin - by1) / grid_mm - _EPS)))
        j1 = min(ny - 1, int(math.floor((oy2 + margin - by1) / grid_mm + _EPS)))
        if i0 > i1 or j0 > j1:
            continue
        cells = [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]
        if ob.layer == "*":
            for layer_set in blocked:
                layer_set.update(cells)
        else:
            blocked[layer_index[ob.layer]].update(cells)

    # Cells within one grid step of either endpoint are always passable so
    # the router can escape/enter dense pad clusters.
    exempt: Set[Tuple[int, int]] = set()
    for ci, cj in ((si, sj), (ei, ej)):
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                exempt.add((ci + di, cj + dj))

    def passable(i: int, j: int, li: int) -> bool:
        if (i, j) in exempt:
            return True
        return (i, j) not in blocked[li]

    def heuristic(i: int, j: int) -> float:
        # Octile distance in grid steps; admissible for 8-dir moves, layer-blind.
        dx = abs(i - ei)
        dy = abs(j - ej)
        return max(dx, dy) + (_SQRT2 - 1.0) * min(dx, dy)

    start_state: _State = (si, sj, 0)
    counter = itertools.count()  # monotonic tie-breaker for determinism
    g_best: Dict[_State, float] = {start_state: 0.0}
    parents: Dict[_State, _State] = {}
    heap: List[Tuple[float, int, float, _State]] = [
        (heuristic(si, sj), next(counter), 0.0, start_state)
    ]
    explored = 0
    goal_state: Optional[_State] = None

    while heap:
        _f, _tie, g, state = heapq.heappop(heap)
        if g > g_best.get(state, math.inf) + _EPS:
            continue  # stale queue entry
        i, j, li = state
        if (i, j) == (ei, ej):
            goal_state = state
            break
        explored += 1
        if explored > max_nodes:
            return _fail("routing area too complex; increase grid_mm or maxNodes", explored)

        for di, dj in _DIRECTIONS:
            ni, nj = i + di, j + dj
            if not (0 <= ni < nx and 0 <= nj < ny):
                continue
            if not passable(ni, nj, li):
                continue
            if di != 0 and dj != 0:
                # Forbid cutting a blocked corner on diagonal moves.
                if not (passable(i + di, j, li) and passable(i, j + dj, li)):
                    continue
            new_g = g + (_SQRT2 if di != 0 and dj != 0 else 1.0)
            new_state: _State = (ni, nj, li)
            if new_g < g_best.get(new_state, math.inf) - _EPS:
                g_best[new_state] = new_g
                parents[new_state] = state
                heapq.heappush(heap, (new_g + heuristic(ni, nj), next(counter), new_g, new_state))

        # Layer change: a through via spans the whole stackup, so the cell
        # must be free on every layer.
        if n_layers > 1 and all(passable(i, j, k) for k in range(n_layers)):
            for nl in range(n_layers):
                if nl == li:
                    continue
                new_g = g + via_cost
                new_state = (i, j, nl)
                if new_g < g_best.get(new_state, math.inf) - _EPS:
                    g_best[new_state] = new_g
                    parents[new_state] = state
                    heapq.heappush(heap, (new_g + heuristic(i, j), next(counter), new_g, new_state))

    if goal_state is None:
        return _fail("no path found between start and end; the area may be fully blocked", explored)

    # Reconstruct, then collapse collinear runs into minimal segments and
    # emit a via at every layer change.
    path: List[_State] = [goal_state]
    while path[-1] != start_state:
        path.append(parents[path[-1]])
    path.reverse()

    def to_mm(i: int, j: int) -> Tuple[float, float]:
        return round(bx1 + i * grid_mm, 6), round(by1 + j * grid_mm, 6)

    segments: List[Dict[str, Any]] = []
    vias: List[Dict[str, Any]] = []
    length_mm = 0.0

    def close_segment(anchor: _State, last: _State) -> None:
        nonlocal length_mm
        if (anchor[0], anchor[1]) == (last[0], last[1]):
            return  # zero-length run (e.g. via immediately after a via/start)
        ax, ay = to_mm(anchor[0], anchor[1])
        lx, ly = to_mm(last[0], last[1])
        segments.append(
            {
                "start": {"x": ax, "y": ay},
                "end": {"x": lx, "y": ly},
                "layer": layers[anchor[2]],
            }
        )
        length_mm += math.hypot(lx - ax, ly - ay)

    anchor = path[0]
    prev = path[0]
    run_dir: Optional[Tuple[int, int]] = None
    for node in path[1:]:
        if node[2] != prev[2]:
            close_segment(anchor, prev)
            vx, vy = to_mm(node[0], node[1])
            vias.append({"x": vx, "y": vy})
            anchor = node
            run_dir = None
        else:
            step = (node[0] - prev[0], node[1] - prev[1])
            if run_dir is None:
                run_dir = step
            elif step != run_dir:
                close_segment(anchor, prev)
                anchor = prev
                run_dir = step
        prev = node
    close_segment(anchor, prev)

    length_mm = round(length_mm, 6)
    message = (
        f"routed {length_mm:g} mm in {len(segments)} segment(s) with {len(vias)} via(s)"
        f" ({explored} nodes explored)"
    )
    return RouteResult(
        success=True,
        segments=segments,
        vias=vias,
        length_mm=length_mm,
        message=message,
        explored=explored,
    )


def obstacles_from_board_items(items: Sequence[Dict[str, Any]]) -> List[RouteObstacle]:
    """Convert extracted board-item dicts into :class:`RouteObstacle` rectangles.

    Each item is a dict like ``{"type": "pad"|"track"|"via", "x1", "y1",
    "x2", "y2", "layer", "net"}`` (tracks arrive as one bounding rect per
    segment).  Vias always span the whole stackup so they get layer ``"*"``;
    pads flagged ``through_hole`` (or supplied without a copper layer) do too.
    Coordinates are normalised so ``x1 <= x2`` and ``y1 <= y2``.
    """
    result: List[RouteObstacle] = []
    for item in items:
        item_type = str(item.get("type", "")).lower()
        layer = item.get("layer") or "*"
        if item_type == "via":
            layer = "*"
        elif item_type == "pad" and item.get("through_hole"):
            layer = "*"
        x1, x2 = float(item["x1"]), float(item["x2"])
        y1, y2 = float(item["y1"]), float(item["y2"])
        result.append(
            RouteObstacle(
                x1=min(x1, x2),
                y1=min(y1, y2),
                x2=max(x1, x2),
                y2=max(y1, y2),
                layer=str(layer),
                net=item.get("net"),
            )
        )
    return result
