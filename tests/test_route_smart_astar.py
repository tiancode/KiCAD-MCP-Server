"""Unit tests for the pure grid A* router behind the ``route_smart`` tool.

``commands.routing._astar`` is pcbnew-independent by design (stdlib only), so
these tests exercise the real search end to end: obstacle inflation and
rasterization, same-net passability, layer changes through vias, collinear
segment collapse, failure messages, and determinism.  All units are mm.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.routing._astar import (  # noqa: E402
    RouteObstacle,
    obstacles_from_board_items,
    route_grid_astar,
)

BOUNDS = (0.0, 0.0, 20.0, 20.0)
# Default inflation applied to obstacles: clearance_mm + trace_width_mm / 2.
MARGIN = 0.2 + 0.25 / 2


def _route(start, end, obstacles=(), layers=("F.Cu",), net="NET1", **kwargs):
    """Call route_grid_astar with the shared test bounds and defaults."""
    return route_grid_astar(
        start,
        end,
        net=net,
        layers=layers,
        obstacles=list(obstacles),
        bounds=BOUNDS,
        **kwargs,
    )


def _segment_hits_rect(seg, ob, margin=MARGIN, step=0.01):
    """Sample points along *seg* and report whether any falls inside the
    obstacle rectangle inflated by *margin* (with a tiny tolerance so points
    exactly on the boundary do not count as intersections)."""
    x1, y1 = seg["start"]["x"], seg["start"]["y"]
    x2, y2 = seg["end"]["x"], seg["end"]["y"]
    rx1, rx2 = min(ob.x1, ob.x2) - margin, max(ob.x1, ob.x2) + margin
    ry1, ry2 = min(ob.y1, ob.y2) - margin, max(ob.y1, ob.y2) + margin
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    samples = max(2, int(length / step) + 1)
    tol = 1e-6
    for k in range(samples + 1):
        t = k / samples
        px = x1 + (x2 - x1) * t
        py = y1 + (y2 - y1) * t
        if rx1 + tol < px < rx2 - tol and ry1 + tol < py < ry2 - tol:
            return True
    return False


def _assert_connected(result, start, end):
    """The segment chain must run unbroken from *start* to *end* (vias keep
    the same x/y, so the chain stays continuous across layer changes)."""
    segs = result.segments
    assert segs, "expected at least one segment"
    assert (segs[0]["start"]["x"], segs[0]["start"]["y"]) == pytest.approx(start)
    assert (segs[-1]["end"]["x"], segs[-1]["end"]["y"]) == pytest.approx(end)
    for prev, nxt in zip(segs, segs[1:]):
        assert (prev["end"]["x"], prev["end"]["y"]) == pytest.approx(
            (nxt["start"]["x"], nxt["start"]["y"])
        )


# ---------------------------------------------------------------------------
# Straight route in an empty area
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStraightRoute:
    def test_single_segment_and_length(self):
        result = _route((1.0, 10.0), (9.0, 10.0))
        assert result.success is True
        assert len(result.segments) == 1
        assert result.vias == []
        assert result.length_mm == pytest.approx(8.0)
        seg = result.segments[0]
        assert seg["layer"] == "F.Cu"
        assert (seg["start"]["x"], seg["start"]["y"]) == pytest.approx((1.0, 10.0))
        assert (seg["end"]["x"], seg["end"]["y"]) == pytest.approx((9.0, 10.0))

    def test_pure_diagonal_collapses_to_one_segment(self):
        result = _route((1.0, 1.0), (6.0, 6.0))
        assert result.success is True
        assert len(result.segments) == 1
        assert result.length_mm == pytest.approx(5.0 * 2**0.5)


# ---------------------------------------------------------------------------
# Obstacle avoidance
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestObstacleAvoidance:
    WALL = RouteObstacle(4.0, 6.0, 6.0, 14.0, "F.Cu", net="GND")

    def test_route_detours_around_blocking_rect(self):
        result = _route((1.0, 10.0), (9.0, 10.0), obstacles=[self.WALL])
        assert result.success is True
        assert result.length_mm > 8.0  # longer than the straight line it must avoid
        _assert_connected(result, (1.0, 10.0), (9.0, 10.0))

    def test_no_segment_intersects_inflated_obstacle(self):
        result = _route((1.0, 10.0), (9.0, 10.0), obstacles=[self.WALL])
        assert result.success is True
        for seg in result.segments:
            assert not _segment_hits_rect(seg, self.WALL), f"segment {seg} crosses the keep-out"

    def test_same_net_obstacle_is_passable(self):
        friendly = RouteObstacle(4.0, 6.0, 6.0, 14.0, "F.Cu", net="NET1")
        result = _route((1.0, 10.0), (9.0, 10.0), obstacles=[friendly], net="NET1")
        assert result.success is True
        assert len(result.segments) == 1  # straight through
        assert result.length_mm == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# Two-layer routing with vias
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTwoLayerVia:
    WALL = RouteObstacle(9.0, 0.0, 11.0, 20.0, "F.Cu", net="GND")  # blocks all of F.Cu

    def test_via_used_when_top_layer_fully_walled(self):
        result = _route((2.0, 10.0), (18.0, 10.0), obstacles=[self.WALL], layers=("F.Cu", "B.Cu"))
        assert result.success is True
        assert len(result.vias) >= 1
        assert {seg["layer"] for seg in result.segments} == {"F.Cu", "B.Cu"}
        _assert_connected(result, (2.0, 10.0), (18.0, 10.0))

    def test_via_cell_clear_on_all_layers(self):
        result = _route((2.0, 10.0), (18.0, 10.0), obstacles=[self.WALL], layers=("F.Cu", "B.Cu"))
        assert result.success is True
        # A through via spans the stackup, so it must sit outside the inflated
        # F.Cu wall even though the wall does not block B.Cu.
        for via in result.vias:
            inside_x = self.WALL.x1 - MARGIN < via["x"] < self.WALL.x2 + MARGIN
            inside_y = self.WALL.y1 - MARGIN < via["y"] < self.WALL.y2 + MARGIN
            assert not (inside_x and inside_y), f"via {via} placed inside the keep-out"


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFailureModes:
    def test_blocked_start_reports_which_end_and_net(self):
        pad = RouteObstacle(0.0, 9.0, 2.0, 11.0, "F.Cu", net="GND")
        result = _route((1.0, 10.0), (9.0, 10.0), obstacles=[pad])
        assert result.success is False
        assert result.segments == [] and result.vias == []
        assert "start" in result.message.lower()
        assert "GND" in result.message

    def test_blocked_end_reports_which_end(self):
        pad = RouteObstacle(8.0, 9.0, 10.0, 11.0, "*", net="GND")
        result = _route((1.0, 10.0), (9.0, 10.0), obstacles=[pad])
        assert result.success is False
        assert "end" in result.message.lower()
        assert "GND" in result.message

    def test_start_outside_bounds(self):
        result = _route((-5.0, 10.0), (9.0, 10.0))
        assert result.success is False
        assert "outside" in result.message.lower()

    def test_max_nodes_exhaustion(self):
        result = _route((1.0, 10.0), (19.0, 10.0), max_nodes=5)
        assert result.success is False
        assert "grid_mm" in result.message
        assert "maxNodes" in result.message
        assert result.explored > 0


# ---------------------------------------------------------------------------
# Collinear collapse
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCollinearCollapse:
    def test_l_shaped_corridor_yields_exactly_two_segments(self):
        # Walls carve a one-cell-wide L corridor: along y=1 from the start,
        # then up x=8 to the end. Every grid step inside a leg is collinear,
        # so the collapse must emit exactly two segments.
        walls = [
            RouteObstacle(0.0, 0.0, 10.0, 0.5, "F.Cu", net="GND"),
            RouteObstacle(0.0, 1.5, 7.5, 10.0, "F.Cu", net="GND"),
            RouteObstacle(8.5, 1.5, 10.0, 10.0, "F.Cu", net="GND"),
        ]
        result = route_grid_astar(
            (1.0, 1.0),
            (8.0, 8.0),
            net="NET1",
            layers=("F.Cu",),
            obstacles=walls,
            bounds=(0.0, 0.0, 10.0, 10.0),
        )
        assert result.success is True
        assert len(result.segments) == 2
        assert result.length_mm == pytest.approx(14.0)  # 7 mm across + 7 mm up
        _assert_connected(result, (1.0, 1.0), (8.0, 8.0))
        corner = result.segments[0]["end"]
        assert (corner["x"], corner["y"]) == pytest.approx((8.0, 1.0))


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeterminism:
    def test_same_inputs_give_identical_results(self):
        wall = RouteObstacle(4.0, 6.0, 6.0, 14.0, "F.Cu", net="GND")
        first = _route((1.0, 10.0), (9.0, 10.0), obstacles=[wall], layers=("F.Cu", "B.Cu"))
        second = _route((1.0, 10.0), (9.0, 10.0), obstacles=[wall], layers=("F.Cu", "B.Cu"))
        assert first.success is True
        assert first == second  # dataclass equality covers segments/vias/length/message


# ---------------------------------------------------------------------------
# obstacles_from_board_items helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestObstaclesFromBoardItems:
    def test_via_and_through_hole_pad_span_all_layers(self):
        items = [
            {"type": "via", "x1": 1, "y1": 1, "x2": 2, "y2": 2, "layer": "F.Cu", "net": "GND"},
            {
                "type": "pad",
                "x1": 0,
                "y1": 0,
                "x2": 1,
                "y2": 1,
                "layer": "F.Cu",
                "net": "SIG",
                "through_hole": True,
            },
            {"type": "pad", "x1": 0, "y1": 0, "x2": 1, "y2": 1, "net": "SIG"},  # no layer
        ]
        obstacles = obstacles_from_board_items(items)
        assert [ob.layer for ob in obstacles] == ["*", "*", "*"]
        assert obstacles[0].net == "GND"

    def test_track_keeps_layer_and_normalises_coords(self):
        items = [
            {"type": "track", "x1": 3, "y1": 4, "x2": 1, "y2": 2, "layer": "B.Cu", "net": "SIG"}
        ]
        (ob,) = obstacles_from_board_items(items)
        assert ob.layer == "B.Cu"
        assert (ob.x1, ob.y1, ob.x2, ob.y2) == (1.0, 2.0, 3.0, 4.0)
        assert ob.net == "SIG"

    def test_smd_pad_keeps_its_copper_layer(self):
        items = [{"type": "pad", "x1": 0, "y1": 0, "x2": 1, "y2": 1, "layer": "F.Cu", "net": "SIG"}]
        (ob,) = obstacles_from_board_items(items)
        assert ob.layer == "F.Cu"
