"""P12: truthful errorCodes on routing/zone refusals (not INTERNAL_ERROR).

Round-5 established that a deliberate refusal must carry a truthful, branchable
errorCode instead of the generic INTERNAL_ERROR.  The E2E round-6 PCB phase
found several routing/zone refusals still leaking INTERNAL_ERROR:

  * route_smart "no path"          -> NO_PATH
  * route_pad_to_pad short refusal -> SHORT_REFUSED
  * delete_trace bogus uuid        -> NOT_FOUND
  * refill_zones SWIG refusal      -> REQUIRES_IPC

plus the surrounding zone-command surface (net/zone not-found, ambiguous match,
bad params).  These tests pin each code at the command boundary.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.routing import RoutingCommands, _refuse_with_obstacles  # noqa: E402

_NM = 1_000_000


# ---------------------------------------------------------------------------
# route_smart no-path -> NO_PATH
# ---------------------------------------------------------------------------
def _empty_route_board():
    board = MagicMock()
    board.GetFootprints.return_value = []
    board.Tracks.return_value = []
    board.GetFileName.return_value = "/nonexistent/proj.kicad_pcb"
    board.GetLayerID.side_effect = lambda name: {"F.Cu": 0, "B.Cu": 31}.get(name, -1)
    board.GetLayerName.side_effect = lambda lid: {0: "F.Cu", 31: "B.Cu"}.get(lid, "?")
    bbox = MagicMock()
    bbox.GetLeft.return_value = 0
    bbox.GetTop.return_value = 0
    bbox.GetRight.return_value = int(50 * _NM)
    bbox.GetBottom.return_value = int(50 * _NM)
    board.GetBoardEdgesBoundingBox.return_value = bbox
    design = MagicMock()
    design.GetCurrentTrackWidth.return_value = int(0.25 * _NM)
    board.GetDesignSettings.return_value = design
    board.GetNetInfo.return_value.GetNetItem.return_value = None
    return board


@pytest.mark.unit
def test_route_smart_no_path_gets_no_path_code():
    rc = RoutingCommands(_empty_route_board())
    # End point outside the routable bounds -> the A* core fails -> route_smart
    # reports a no-path outcome, which is NOT an internal error.
    res = rc.route_smart({"start": {"x": 1, "y": 1}, "end": {"x": 100, "y": 100}, "net": "SIG"})
    assert res["success"] is False
    assert res["errorCode"] == "NO_PATH"
    assert res["errorCode"] != "INTERNAL_ERROR"


@pytest.mark.unit
def test_route_smart_unknown_layer_gets_validation_code():
    rc = RoutingCommands(_empty_route_board())
    res = rc.route_smart(
        {"start": {"x": 1, "y": 1}, "end": {"x": 2, "y": 2}, "layers": ["X.Cu"]}
    )
    assert res["success"] is False
    assert res["errorCode"] == "VALIDATION"


@pytest.mark.unit
def test_route_smart_component_not_found_gets_not_found_code():
    rc = RoutingCommands(_empty_route_board())
    res = rc.route_smart({"fromRef": "NOPE", "fromPad": "1", "toRef": "ALSO", "toPad": "1"})
    assert res["success"] is False
    assert res["errorCode"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# route_pad_to_pad short refusal -> SHORT_REFUSED
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_refuse_with_obstacles_carries_short_refused():
    out = _refuse_with_obstacles("C1", "1", "C1", "2", ["through U3.12", "through U3.13"])
    assert out["success"] is False
    assert out["errorCode"] == "SHORT_REFUSED"
    assert out["hasObstacles"] is True


# ---------------------------------------------------------------------------
# delete_trace / modify_trace not-found -> NOT_FOUND
# ---------------------------------------------------------------------------
def _board_one_track(uuid="real-uuid"):
    board = MagicMock()
    track = MagicMock()
    track.m_Uuid.AsString.return_value = uuid
    board.Tracks.return_value = [track]
    return board


@pytest.mark.unit
def test_delete_trace_bogus_uuid_gets_not_found():
    rc = RoutingCommands(_board_one_track())
    res = rc.delete_trace({"traceUuid": "00000000-dead-beef-0000-000000000000"})
    assert res["success"] is False
    assert res["message"] == "Track not found"
    assert res["errorCode"] == "NOT_FOUND"
    assert res["errorCode"] != "INTERNAL_ERROR"


@pytest.mark.unit
def test_modify_trace_bogus_uuid_gets_not_found():
    rc = RoutingCommands(_board_one_track())
    res = rc.modify_trace({"uuid": "bogus", "width": 0.3})
    assert res["success"] is False
    assert res["errorCode"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# refill_zones SWIG refusal -> REQUIRES_IPC
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_refill_zones_refusal_gets_requires_ipc():
    from handlers.routing import handle_refill_zones

    iface = MagicMock()
    iface.board.GetAreaCount.return_value = 2
    res = handle_refill_zones(iface, {})  # no force -> refuse
    assert res["success"] is False
    assert res["requires_ipc"] is True
    assert res["errorCode"] == "REQUIRES_IPC"
    assert res["errorCode"] != "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Zone command surface: net/zone not-found + ambiguous match
# ---------------------------------------------------------------------------
def _board_with_nets(names):
    board = MagicMock()
    info = MagicMock()
    info.GetNetCount.return_value = len(names)
    items = []
    for n in names:
        it = MagicMock()
        it.GetNetname.return_value = n
        items.append(it)
    info.GetNetItem.side_effect = lambda code: items[code]
    board.GetNetInfo.return_value = info
    return board


@pytest.mark.unit
def test_add_copper_pour_unknown_net_gets_not_found():
    board = _board_with_nets(["/GND"])
    rc = RoutingCommands(board)
    res = rc.add_copper_pour(
        {
            "net": "VCC",
            "layer": "F.Cu",
            "outline": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}],
        }
    )
    assert res["success"] is False
    assert res["errorCode"] == "NOT_FOUND"


@pytest.mark.unit
def test_add_copper_pour_missing_net_gets_validation():
    board = _board_with_nets(["/GND"])
    rc = RoutingCommands(board)
    res = rc.add_copper_pour(
        {"layer": "F.Cu", "outline": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}]}
    )
    assert res["success"] is False
    assert res["errorCode"] == "VALIDATION"


@pytest.mark.unit
def test_delete_copper_pour_unknown_uuid_gets_not_found():
    board = MagicMock()
    board.Zones.return_value = []
    rc = RoutingCommands(board)
    res = rc.delete_copper_pour({"zoneUuid": "nope"})
    assert res["success"] is False
    assert res["errorCode"] == "NOT_FOUND"


@pytest.mark.unit
def test_edit_copper_pour_ambiguous_match_gets_validation():
    board = MagicMock()
    z1, z2 = MagicMock(), MagicMock()
    for z, u in ((z1, "a"), (z2, "b")):
        z.m_Uuid.AsString.return_value = u
        z.GetNetname.return_value = "/GND"
        z.GetLayer.return_value = 0
        z.IsFilled.return_value = False
    board.Zones.return_value = [z1, z2]
    board.GetLayerID.return_value = 0
    board.GetLayerName.return_value = "F.Cu"
    rc = RoutingCommands(board)
    res = rc.edit_copper_pour({"net": "/GND", "clearance": 0.3})
    assert res["success"] is False
    assert res["errorCode"] == "VALIDATION"
