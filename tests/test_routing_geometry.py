"""Unit tests for the pure / geometry helpers in commands.routing.

These pin down the deterministic, board-independent behaviour of
``RoutingCommands`` (distance math, bbox clipping, unit conversion, and the
obstacle-refusal envelope) so an upcoming internal refactor of the large
``routing.py`` module cannot silently change it.

The heavy pcbnew dependency is stubbed globally by tests/conftest.py; where a
helper constructs ``pcbnew.VECTOR2I`` we monkeypatch in a tiny real stand-in so
the arithmetic actually runs instead of returning MagicMocks.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands import routing  # noqa: E402
from commands.routing import RoutingCommands, _point_to_segment_distance_nm  # noqa: E402


class _Vec:
    """Minimal VECTOR2I stand-in: stores integer x/y like the real KiCAD type."""

    def __init__(self, x, y):
        self.x = int(x)
        self.y = int(y)


@pytest.fixture
def real_vector2i(monkeypatch):
    """Swap the MagicMock pcbnew.VECTOR2I for a real arithmetic-capable stub."""
    monkeypatch.setattr(routing.pcbnew, "VECTOR2I", _Vec)
    return _Vec


def _bbox(left, top, right, bottom):
    """A bounding box exposing the GetLeft/GetTop/GetRight/GetBottom accessors
    that _segment_intersects_bbox reads."""
    return SimpleNamespace(
        GetLeft=lambda: left,
        GetTop=lambda: top,
        GetRight=lambda: right,
        GetBottom=lambda: bottom,
    )


# ---------------------------------------------------------------------------
# _point_to_segment_distance_nm  (module-level, pure math)
# ---------------------------------------------------------------------------


class TestPointToSegmentDistance:
    def test_perpendicular_projection_inside_segment(self):
        # (5,5) projects onto the x-axis segment at (5,0) → distance 5.
        assert _point_to_segment_distance_nm(5, 5, 0, 0, 10, 0) == pytest.approx(5.0)

    def test_projection_clamped_to_start_endpoint(self):
        # (-5,0) projects before the start → clamps to (0,0) → distance 5.
        assert _point_to_segment_distance_nm(-5, 0, 0, 0, 10, 0) == pytest.approx(5.0)

    def test_projection_clamped_to_end_endpoint(self):
        # (15,0) projects past the end → clamps to (10,0) → distance 5.
        assert _point_to_segment_distance_nm(15, 0, 0, 0, 10, 0) == pytest.approx(5.0)

    def test_point_on_segment_is_zero(self):
        assert _point_to_segment_distance_nm(5, 0, 0, 0, 10, 0) == pytest.approx(0.0)

    def test_degenerate_zero_length_segment(self):
        # Both endpoints coincide → plain point-to-point distance (3,4)->5.
        assert _point_to_segment_distance_nm(3, 4, 0, 0, 0, 0) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# RoutingCommands._segment_intersects_bbox  (staticmethod, pure)
# ---------------------------------------------------------------------------


class TestSegmentIntersectsBbox:
    box = staticmethod(lambda: _bbox(0, 0, 10, 10))

    def test_segment_fully_inside(self):
        assert RoutingCommands._segment_intersects_bbox(2, 2, 8, 8, self.box()) is True

    def test_segment_crossing_through(self):
        assert RoutingCommands._segment_intersects_bbox(-5, 5, 15, 5, self.box()) is True

    def test_segment_entirely_outside(self):
        assert RoutingCommands._segment_intersects_bbox(-5, -5, -1, -1, self.box()) is False

    def test_vertical_line_through_box(self):
        assert RoutingCommands._segment_intersects_bbox(5, -5, 5, 15, self.box()) is True

    def test_unreadable_bbox_returns_false(self):
        broken = SimpleNamespace()  # no GetLeft/... → AttributeError → False
        assert RoutingCommands._segment_intersects_bbox(0, 0, 1, 1, broken) is False


# ---------------------------------------------------------------------------
# RoutingCommands._point_distance  (pure once given .x/.y points)
# ---------------------------------------------------------------------------


class TestPointDistance:
    def test_basic_3_4_5(self):
        rc = RoutingCommands()
        p1 = SimpleNamespace(x=0, y=0)
        p2 = SimpleNamespace(x=3, y=4)
        assert rc._point_distance(p1, p2) == pytest.approx(5.0)

    def test_identical_points_zero(self):
        rc = RoutingCommands()
        p = SimpleNamespace(x=7, y=-2)
        assert rc._point_distance(p, p) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# RoutingCommands._point_to_track_distance  (needs real VECTOR2I)
# ---------------------------------------------------------------------------


class TestPointToTrackDistance:
    @staticmethod
    def _track(sx, sy, ex, ey):
        return SimpleNamespace(
            GetStart=lambda: SimpleNamespace(x=sx, y=sy),
            GetEnd=lambda: SimpleNamespace(x=ex, y=ey),
        )

    def test_projection_inside(self, real_vector2i):
        rc = RoutingCommands()
        d = rc._point_to_track_distance(SimpleNamespace(x=5, y=5), self._track(0, 0, 10, 0))
        assert d == pytest.approx(5.0)

    def test_beyond_end_uses_endpoint(self, real_vector2i):
        rc = RoutingCommands()
        d = rc._point_to_track_distance(SimpleNamespace(x=15, y=0), self._track(0, 0, 10, 0))
        assert d == pytest.approx(5.0)

    def test_zero_length_track_uses_start(self, real_vector2i):
        rc = RoutingCommands()
        d = rc._point_to_track_distance(SimpleNamespace(x=3, y=4), self._track(0, 0, 0, 0))
        assert d == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# RoutingCommands._get_point  (unit conversion → VECTOR2I)
# ---------------------------------------------------------------------------


class TestGetPoint:
    def test_mm_scale_default(self, real_vector2i):
        rc = RoutingCommands()
        pt = rc._get_point({"x": 2, "y": 3})  # unit defaults to mm
        assert (pt.x, pt.y) == (2_000_000, 3_000_000)

    def test_mm_scale_explicit(self, real_vector2i):
        rc = RoutingCommands()
        pt = rc._get_point({"x": 1, "y": 2, "unit": "mm"})
        assert (pt.x, pt.y) == (1_000_000, 2_000_000)

    def test_mil_scale(self, real_vector2i):
        rc = RoutingCommands()
        pt = rc._get_point({"x": 1, "y": 1, "unit": "mil"})
        assert (pt.x, pt.y) == (25_400, 25_400)

    def test_other_unit_scale(self, real_vector2i):
        rc = RoutingCommands()
        pt = rc._get_point({"x": 1, "y": 1, "unit": "in"})
        assert (pt.x, pt.y) == (25_400_000, 25_400_000)

    def test_invalid_spec_raises(self, real_vector2i):
        rc = RoutingCommands()
        with pytest.raises(ValueError):
            rc._get_point({"foo": "bar"})


# ---------------------------------------------------------------------------
# _refuse_with_obstacles  (module-level, pure envelope)
# ---------------------------------------------------------------------------


class TestRefuseWithObstacles:
    def test_envelope_shape(self):
        result = routing._refuse_with_obstacles("R1", "1", "U2", "3", ["C1.2", "C3.1"])
        assert result["success"] is False
        assert result["hasObstacles"] is True
        assert result["obstacleCount"] == 2
        assert result["obstaclesCrossed"] == ["C1.2", "C3.1"]

    def test_message_names_both_pads_and_count(self):
        result = routing._refuse_with_obstacles("R1", "1", "U2", "3", ["C1.2", "C3.1"])
        assert "R1.1" in result["message"]
        assert "U2.3" in result["message"]
        assert "2 other pad(s)" in result["message"]

    def test_hint_points_at_force_optout(self):
        result = routing._refuse_with_obstacles("R1", "1", "U2", "3", [])
        assert result["obstacleCount"] == 0
        assert "force=true" in result["hint"]


# ---------------------------------------------------------------------------
# Public API surface — a guard for the upcoming internal refactor.
#
# If routing.py is later split into mixins / collaborating modules, this
# pins the set of public command methods so the split can't silently drop or
# rename one. Update this set deliberately when adding/removing a command.
# ---------------------------------------------------------------------------

EXPECTED_ROUTING_COMMANDS = {
    "add_copper_pour",
    "add_gnd_stitching_vias",
    "add_net",
    "add_via",
    "assign_net_to_class",
    "assign_netclass_pattern",
    "copy_routing_pattern",
    "create_netclass",
    "delete_trace",
    "get_nets_list",
    "modify_trace",
    "query_traces",
    "query_zones",
    "route_arc_trace",
    "route_differential_pair",
    "route_pad_to_pad",
    "route_trace",
}


class TestPublicApiSurface:
    def test_public_command_methods_unchanged(self):
        actual = {
            name
            for name in dir(RoutingCommands)
            if not name.startswith("_") and callable(getattr(RoutingCommands, name))
        }
        assert actual == EXPECTED_ROUTING_COMMANDS

    def test_constructor_accepts_no_board(self):
        # Handlers instantiate RoutingCommands() then assign a board; the
        # split must keep this zero-arg construction working.
        assert RoutingCommands().board is None
