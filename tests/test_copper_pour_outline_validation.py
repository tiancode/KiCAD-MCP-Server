"""B9 regression: add_copper_pour must refuse a degenerate outline.

A supplied 1-2 point (or ≥3 colinear, zero-area) outline used to fall silently
into the board-outline fallback — creating a full-board plane the caller never
asked for, or persisting a useless zero-area zone.  Only an OMITTED outline may
default to the board rectangle; a supplied-but-degenerate one is refused with a
truthful VALIDATION errorCode (mirrors edit_copper_pour's ">=3 points" gate).

Pins:
  * the pure ``_outline_is_degenerate`` helper (unit-aware, colinear detection);
  * add_copper_pour refusing 2-point and colinear outlines without mutating;
  * an omitted outline still falling back to the board rectangle (regression);
  * a valid polygon still succeeding.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

import pcbnew  # noqa: F401, E402  (stubbed by tests/conftest.py)
from commands.routing._zones import ZoneMixin, _outline_is_degenerate  # noqa: E402

_NM = 1_000_000


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOutlineIsDegenerate:
    def test_two_points(self):
        assert _outline_is_degenerate([{"x": 0, "y": 0}, {"x": 10, "y": 0}]) is True

    def test_one_point(self):
        assert _outline_is_degenerate([{"x": 0, "y": 0}]) is True

    def test_three_colinear_points(self):
        pts = [{"x": 0, "y": 0}, {"x": 5, "y": 0}, {"x": 10, "y": 0}]
        assert _outline_is_degenerate(pts) is True

    def test_valid_triangle(self):
        pts = [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}]
        assert _outline_is_degenerate(pts) is False

    def test_valid_square(self):
        pts = [
            {"x": 0, "y": 0},
            {"x": 20, "y": 0},
            {"x": 20, "y": 20},
            {"x": 0, "y": 20},
        ]
        assert _outline_is_degenerate(pts) is False

    def test_colinear_in_mil_still_degenerate(self):
        pts = [
            {"x": 0, "y": 0, "unit": "mil"},
            {"x": 100, "y": 0, "unit": "mil"},
            {"x": 200, "y": 0, "unit": "mil"},
        ]
        assert _outline_is_degenerate(pts) is True

    def test_malformed_point_is_not_degenerate(self):
        # Can't judge missing coordinates — leave it to downstream handling.
        pts = [{"x": 0, "y": 0}, {"y": 5}, {"x": 10, "y": 10}]
        assert _outline_is_degenerate(pts) is False


# ---------------------------------------------------------------------------
# add_copper_pour integration
# ---------------------------------------------------------------------------


class _AddHost(ZoneMixin):
    """Minimal ZoneMixin host with a fixed net list and a stub board."""

    def __init__(self, net_names: List[str]) -> None:
        self._net_names = net_names
        self.board = MagicMock(name="board")
        self.board.GetLayerID.side_effect = lambda n: {
            "F.Cu": 0,
            "B.Cu": 31,
            "Edge.Cuts": 44,
        }.get(n, -1)
        nm = MagicMock(name="NetsByName")
        nm.has_key.side_effect = lambda n: n in net_names
        self.board.GetNetInfo.return_value.NetsByName.return_value = nm
        # Board-outline fallback support.
        box = MagicMock()
        box.GetWidth.return_value = int(80 * _NM)
        box.GetHeight.return_value = int(60 * _NM)
        box.GetX.return_value = 0
        box.GetY.return_value = 0
        self.board.GetBoardEdgesBoundingBox.return_value = box
        self.board.GetDrawings.return_value = []

    def _board_net_names(self) -> List[str]:
        return list(self._net_names)


@pytest.mark.unit
class TestAddCopperPourOutlineValidation:
    def test_two_point_outline_refused_not_board_fallback(self):
        host = _AddHost(["", "/GND"])
        out = host.add_copper_pour(
            {"layer": "F.Cu", "net": "GND", "outline": [{"x": 10, "y": 10}, {"x": 20, "y": 10}]}
        )
        assert out["success"] is False
        assert out["errorCode"] == "VALIDATION"
        assert "3 points" in out["message"]
        host.board.Add.assert_not_called()  # never silently made a board plane

    def test_colinear_outline_refused(self):
        host = _AddHost(["", "/GND"])
        out = host.add_copper_pour(
            {
                "layer": "F.Cu",
                "net": "GND",
                "outline": [{"x": 0, "y": 0}, {"x": 5, "y": 0}, {"x": 10, "y": 0}],
            }
        )
        assert out["success"] is False
        assert out["errorCode"] == "VALIDATION"
        assert "degenerate" in out["message"] or "zero area" in out["message"]
        host.board.Add.assert_not_called()

    def test_omitted_outline_still_falls_back_to_board_rect(self):
        host = _AddHost(["", "/GND"])
        out = host.add_copper_pour({"layer": "F.Cu", "net": "GND"})
        assert out["success"] is True
        assert out["pour"]["pointCount"] == 4  # board rectangle
        host.board.Add.assert_called_once()

    def test_valid_triangle_succeeds(self):
        host = _AddHost(["", "/GND"])
        out = host.add_copper_pour(
            {
                "layer": "F.Cu",
                "net": "GND",
                "outline": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}],
            }
        )
        assert out["success"] is True
        assert out["pour"]["pointCount"] == 3
        host.board.Add.assert_called_once()

    def test_unknown_net_still_refused_before_outline_check(self):
        # Net resolution precedes the outline check — a bad net is NOT_FOUND
        # regardless of outline shape.
        host = _AddHost(["", "/GND"])
        out = host.add_copper_pour(
            {"layer": "F.Cu", "net": "VBUS", "outline": [{"x": 0, "y": 0}, {"x": 1, "y": 0}]}
        )
        assert out["success"] is False
        assert out["errorCode"] == "NOT_FOUND"
