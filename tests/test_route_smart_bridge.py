"""Smoke tests for the route_smart board bridge (mocked SWIG board)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.routing import RoutingCommands

_NM = 1_000_000


def _mock_pad(number, x_mm, y_mm, net="NET1", through=False, size_mm=1.0):
    pad = MagicMock()
    pad.GetNumber.return_value = number
    pos = MagicMock()
    pos.x, pos.y = int(x_mm * _NM), int(y_mm * _NM)
    pad.GetPosition.return_value = pos
    pad.GetNetname.return_value = net
    pad.HasHole.return_value = through
    pad.IsOnLayer.return_value = True
    bb = MagicMock()
    half = int(size_mm * _NM / 2)
    bb.GetLeft.return_value = pos.x - half
    bb.GetRight.return_value = pos.x + half
    bb.GetTop.return_value = pos.y - half
    bb.GetBottom.return_value = pos.y + half
    pad.GetBoundingBox.return_value = bb
    return pad


def _mock_board(pads_by_ref, size_mm=(50, 50)):
    board = MagicMock()
    footprints = []
    for ref, pads in pads_by_ref.items():
        fp = MagicMock()
        fp.GetReference.return_value = ref
        fp.Pads.return_value = pads
        footprints.append(fp)
    board.GetFootprints.return_value = footprints
    board.Tracks.return_value = []
    board.GetLayerID.side_effect = lambda name: {"F.Cu": 0, "B.Cu": 31}.get(name, -1)
    board.GetLayerName.side_effect = lambda lid: {0: "F.Cu", 31: "B.Cu"}.get(lid, "?")
    bbox = MagicMock()
    bbox.GetLeft.return_value = 0
    bbox.GetTop.return_value = 0
    bbox.GetRight.return_value = int(size_mm[0] * _NM)
    bbox.GetBottom.return_value = int(size_mm[1] * _NM)
    board.GetBoardEdgesBoundingBox.return_value = bbox
    design = MagicMock()
    design.GetCurrentTrackWidth.return_value = int(0.25 * _NM)
    design.GetCurrentViaSize.return_value = int(0.6 * _NM)
    design.GetCurrentViaDrill.return_value = int(0.3 * _NM)
    board.GetDesignSettings.return_value = design
    net_item = MagicMock()
    net_item.GetNetCode.return_value = 7
    net_item.GetNetClass.return_value = None
    board.GetNetInfo.return_value.GetNetItem.return_value = net_item
    return board


@pytest.mark.unit
class TestRouteSmartBridge:
    def test_routes_between_two_pads_and_adds_tracks(self):
        board = _mock_board(
            {
                "R1": [_mock_pad("1", 5.0, 25.0)],
                "R2": [_mock_pad("1", 45.0, 25.0)],
            }
        )
        rc = RoutingCommands(board)
        result = rc.route_smart(
            {"fromRef": "R1", "fromPad": "1", "toRef": "R2", "toPad": "1", "width": 0.25}
        )
        assert result["success"], result
        assert result["tracksCreated"] >= 1
        assert result["net"] == "NET1"
        assert board.Add.call_count == result["tracksCreated"] + result["viasCreated"]
        assert result["lengthMm"] >= 40.0

    def test_no_board_refused(self):
        rc = RoutingCommands(None)
        result = rc.route_smart({"fromRef": "R1", "fromPad": "1", "toRef": "R2", "toPad": "1"})
        assert result["success"] is False
        assert "No board" in result["message"]

    def test_missing_component_refused(self):
        board = _mock_board({"R1": [_mock_pad("1", 5.0, 25.0)]})
        rc = RoutingCommands(board)
        result = rc.route_smart({"fromRef": "R1", "fromPad": "1", "toRef": "NOPE", "toPad": "1"})
        assert result["success"] is False
        assert "NOPE" in result["message"]

    def test_unknown_layer_refused(self):
        board = _mock_board({"R1": [_mock_pad("1", 5.0, 25.0)]})
        rc = RoutingCommands(board)
        result = rc.route_smart(
            {
                "fromRef": "R1",
                "fromPad": "1",
                "toRef": "R1",
                "toPad": "1",
                "layers": ["X.Cu"],
            }
        )
        assert result["success"] is False
        assert "Unknown layer" in result["message"]

    def test_point_to_point_route(self):
        board = _mock_board({})
        rc = RoutingCommands(board)
        result = rc.route_smart(
            {
                "start": {"x": 5.0, "y": 5.0},
                "end": {"x": 20.0, "y": 5.0},
                "net": "SIG",
                "width": 0.3,
            }
        )
        assert result["success"], result
        assert result["lengthMm"] == pytest.approx(15.0, abs=1.0)
