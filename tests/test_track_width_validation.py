"""P10: track-width bounds validation.

route_trace / route_smart (explicit width) / create_netclass (traceWidth) used
to accept an absurd width (999 mm was seen in the wild, wider than the whole
board) and silently create a giant copper slab.  A single shared helper,
``commands.routing._helpers._track_width_error``, now bounds every width to
``0 < w <= 50 mm`` with a truthful ``VALIDATION`` errorCode and a message that
names the limit and unit.  These tests pin the helper and each call site.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.routing import RoutingCommands  # noqa: E402
from commands.routing._helpers import (  # noqa: E402
    MAX_TRACK_WIDTH_MM,
    _track_width_error,
)


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestTrackWidthErrorHelper:
    def test_none_passes(self):
        # None means "not supplied" — callers only validate an explicit width.
        assert _track_width_error(None) is None

    @pytest.mark.parametrize("good", [0.1, 0.25, 0.5, 1.0, MAX_TRACK_WIDTH_MM])
    def test_in_range_passes(self, good):
        assert _track_width_error(good) is None

    @pytest.mark.parametrize("bad", [0, -0.1, -5])
    def test_non_positive_refused(self, bad):
        err = _track_width_error(bad)
        assert err is not None
        assert err["success"] is False
        assert err["errorCode"] == "VALIDATION"
        assert "0 mm" in err["message"]

    @pytest.mark.parametrize("bad", [50.0001, 51, 999, 1000])
    def test_over_cap_refused(self, bad):
        err = _track_width_error(bad)
        assert err is not None
        assert err["errorCode"] == "VALIDATION"
        # Message must name the concrete limit so the agent can self-correct.
        assert "50" in err["message"]

    def test_non_numeric_refused(self):
        err = _track_width_error("wide")
        assert err is not None
        assert err["errorCode"] == "VALIDATION"

    def test_field_name_is_surfaced(self):
        err = _track_width_error(999, field="traceWidth")
        assert err is not None
        assert "traceWidth" in err["message"]


# ---------------------------------------------------------------------------
# Call sites
# ---------------------------------------------------------------------------
def _mock_board_for_trace():
    board = MagicMock()
    board.GetLayerID.return_value = 0  # a valid layer id (>= 0)
    board.GetNetInfo.return_value.NetsByName.return_value.has_key.return_value = False
    return board


@pytest.mark.unit
class TestRouteTraceWidthBound:
    def test_route_trace_rejects_999(self):
        rc = RoutingCommands(_mock_board_for_trace())
        res = rc.route_trace(
            {
                "start": {"x": 60, "y": 6},
                "end": {"x": 65, "y": 6},
                "layer": "F.Cu",
                "width": 999,
                "net": "/GND",
            }
        )
        assert res["success"] is False
        assert res["errorCode"] == "VALIDATION"
        assert "50" in res["message"]

    def test_route_trace_rejects_non_positive(self):
        rc = RoutingCommands(_mock_board_for_trace())
        res = rc.route_trace(
            {"start": {"x": 0, "y": 0}, "end": {"x": 5, "y": 0}, "layer": "F.Cu", "width": -1}
        )
        assert res["success"] is False
        assert res["errorCode"] == "VALIDATION"

    def test_route_trace_accepts_valid_width(self):
        rc = RoutingCommands(_mock_board_for_trace())
        res = rc.route_trace(
            {"start": {"x": 0, "y": 0}, "end": {"x": 5, "y": 0}, "layer": "F.Cu", "width": 0.5}
        )
        assert res["success"] is True

    def test_route_trace_omitted_width_still_ok(self):
        # No width -> falls back to the design default; no validation error.
        rc = RoutingCommands(_mock_board_for_trace())
        res = rc.route_trace(
            {"start": {"x": 0, "y": 0}, "end": {"x": 5, "y": 0}, "layer": "F.Cu"}
        )
        assert res["success"] is True


@pytest.mark.unit
class TestRouteSmartWidthBound:
    def test_route_smart_rejects_999(self):
        board = MagicMock()
        board.GetLayerID.return_value = 0
        rc = RoutingCommands(board)
        res = rc.route_smart(
            {"start": {"x": 0, "y": 0}, "end": {"x": 10, "y": 0}, "net": "SIG", "width": 999}
        )
        assert res["success"] is False
        assert res["errorCode"] == "VALIDATION"
        assert "50" in res["message"]


@pytest.mark.unit
class TestCreateNetclassWidthBound:
    def test_create_netclass_rejects_999(self):
        rc = RoutingCommands(MagicMock())
        res = rc.create_netclass({"name": "Fat", "traceWidth": 999})
        assert res["success"] is False
        assert res["errorCode"] == "VALIDATION"
        assert "traceWidth" in res["message"]
