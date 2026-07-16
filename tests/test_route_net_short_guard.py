"""B4 regression: routing tools must refuse a cross-net short.

route_pad_to_pad adopted the START pad's net without comparing it to the END
pad; route_smart's endpoint resolver did the same; route_trace stamped a net
onto a track whose endpoint could land on a foreign-net pad; and the IPC
route_trace fast path had the identical gap.  A router must never SILENTLY
connect two different nets (a hard short).  These tests pin:

  * the pure ``_nets_equivalent`` / ``_endpoint_conflict_messages`` core and
    the SWIG ``endpoint_net_conflicts`` scanner;
  * route_pad_to_pad / route_smart / route_trace refusing with the distinct
    ``CROSS_NET_SHORT`` errorCode, and ``force=true`` overriding;
  * the IPC handle_route_trace guard (best-effort over live pads) + force.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

import pcbnew  # noqa: F401, E402  (stubbed by tests/conftest.py)
from commands.routing import RoutingCommands  # noqa: E402
from commands.routing._helpers import (  # noqa: E402
    _endpoint_conflict_messages,
    _nets_equivalent,
    _refuse_cross_net_short,
    endpoint_net_conflicts,
)

_NM = 1_000_000


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNetsEquivalent:
    def test_identical(self):
        assert _nets_equivalent("/GND", "/GND") is True

    def test_slash_prefix_tolerated(self):
        assert _nets_equivalent("GND", "/GND") is True
        assert _nets_equivalent("/GND", "GND") is True

    def test_different_nets(self):
        assert _nets_equivalent("/GND", "/+3V3") is False

    def test_empty_never_matches(self):
        assert _nets_equivalent("", "/GND") is False
        assert _nets_equivalent(None, "/GND") is False


@pytest.mark.unit
class TestEndpointConflictMessages:
    PAD = ("U4", "2", "/+3V3", (44.5, 24.5, 45.5, 25.5))

    def test_foreign_pad_at_endpoint_conflicts(self):
        out = _endpoint_conflict_messages([(45.0, 25.0)], "/GND", [self.PAD])
        assert len(out) == 1
        assert "U4.2" in out[0]
        assert "/+3V3" in out[0]
        assert "/GND" in out[0]

    def test_same_net_pad_is_clean(self):
        pad = ("U4", "2", "/GND", (44.5, 24.5, 45.5, 25.5))
        assert _endpoint_conflict_messages([(45.0, 25.0)], "/GND", [pad]) == []

    def test_slash_variant_pad_is_clean(self):
        pad = ("U4", "2", "/GND", (44.5, 24.5, 45.5, 25.5))
        # trace net "GND" (bare) vs pad "/GND" — same net, no false short.
        assert _endpoint_conflict_messages([(45.0, 25.0)], "GND", [pad]) == []

    def test_endpoint_outside_pad_is_clean(self):
        assert _endpoint_conflict_messages([(10.0, 10.0)], "/GND", [self.PAD]) == []

    def test_no_net_returns_empty(self):
        assert _endpoint_conflict_messages([(45.0, 25.0)], "", [self.PAD]) == []

    def test_non_numeric_endpoint_is_ignored(self):
        # A MagicMock coordinate (dehydrated SWIG proxy) must not falsely fire.
        assert _endpoint_conflict_messages([(MagicMock(), MagicMock())], "/GND", [self.PAD]) == []

    def test_each_pad_reported_once(self):
        out = _endpoint_conflict_messages([(45.0, 25.0), (45.0, 25.0)], "/GND", [self.PAD])
        assert len(out) == 1


@pytest.mark.unit
class TestRefuseCrossNetShort:
    def test_shape(self):
        out = _refuse_cross_net_short("/GND", ["c1", "c2"])
        assert out["success"] is False
        assert out["errorCode"] == "CROSS_NET_SHORT"
        assert out["conflictCount"] == 2
        assert out["crossNetConflicts"] == ["c1", "c2"]
        assert "force=true" in out["hint"]


# ---------------------------------------------------------------------------
# Mock board / pad builders (mirror test_route_smart_bridge)
# ---------------------------------------------------------------------------


def _mock_pad(number, x_mm, y_mm, net, size_mm=1.0):
    pad = MagicMock()
    pad.GetNumber.return_value = number
    pos = MagicMock()
    pos.x, pos.y = int(x_mm * _NM), int(y_mm * _NM)
    pad.GetPosition.return_value = pos
    pad.GetNetname.return_value = net
    pad.HasHole.return_value = False
    pad.IsOnLayer.side_effect = lambda lid: lid == 0  # F.Cu only
    bb = MagicMock()
    half = int(size_mm * _NM / 2)
    bb.GetLeft.return_value = pos.x - half
    bb.GetRight.return_value = pos.x + half
    bb.GetTop.return_value = pos.y - half
    bb.GetBottom.return_value = pos.y + half
    pad.GetBoundingBox.return_value = bb
    return pad


def _mock_board(pads_by_ref, fp_layers=None, size_mm=(50, 50)):
    board = MagicMock()
    fp_layers = fp_layers or {}
    footprints = []
    for ref, pads in pads_by_ref.items():
        fp = MagicMock()
        fp.GetReference.return_value = ref
        fp.Pads.return_value = pads
        fp.GetLayer.return_value = fp_layers.get(ref, 0)
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
    nm = MagicMock()
    nm.has_key.return_value = False
    board.GetNetInfo.return_value.NetsByName.return_value = nm
    return board


# ---------------------------------------------------------------------------
# endpoint_net_conflicts over a SWIG-style mock board
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEndpointNetConflictsScanner:
    def test_foreign_pad_endpoint_flagged(self):
        board = _mock_board(
            {"U4": [_mock_pad("1", 5.0, 25.0, "/GND"), _mock_pad("2", 45.0, 25.0, "/+3V3")]}
        )
        out = endpoint_net_conflicts(board, [(5 * _NM, 25 * _NM), (45 * _NM, 25 * _NM)], "/GND")
        assert len(out) == 1
        assert "U4.2" in out[0]

    def test_same_net_clean(self):
        board = _mock_board(
            {"U4": [_mock_pad("1", 5.0, 25.0, "/GND"), _mock_pad("2", 45.0, 25.0, "/GND")]}
        )
        assert endpoint_net_conflicts(board, [(45 * _NM, 25 * _NM)], "/GND") == []

    def test_non_iterable_footprints_degrade_to_empty(self):
        board = MagicMock()  # GetFootprints() returns a non-iterable MagicMock
        assert endpoint_net_conflicts(board, [(0, 0)], "/GND") == []

    def test_no_net_short_circuits(self):
        board = _mock_board({"U4": [_mock_pad("2", 45.0, 25.0, "/+3V3")]})
        assert endpoint_net_conflicts(board, [(45 * _NM, 25 * _NM)], None) == []


# ---------------------------------------------------------------------------
# route_pad_to_pad
# ---------------------------------------------------------------------------


def _cmds(board):
    cmds = RoutingCommands.__new__(RoutingCommands)
    cmds.board = board
    cmds._project_netclass_props = lambda net: {}  # type: ignore[method-assign]
    cmds._netclass_track_width_mm = lambda pad: 0.25  # type: ignore[method-assign]
    return cmds


@pytest.mark.unit
class TestRoutePadToPadCrossNet:
    def _board(self):
        return _mock_board(
            {"U4": [_mock_pad("1", 5.0, 25.0, "/GND"), _mock_pad("2", 45.0, 25.0, "/+3V3")]}
        )

    def test_cross_net_refused(self):
        cmds = _cmds(self._board())
        res = cmds.route_pad_to_pad({"fromRef": "U4", "fromPad": "1", "toRef": "U4", "toPad": "2"})
        assert res["success"] is False
        assert res["errorCode"] == "CROSS_NET_SHORT"
        assert res["conflictCount"] >= 1

    def test_force_overrides(self):
        cmds = _cmds(self._board())
        cmds.route_trace = lambda params: {"success": True}  # type: ignore[method-assign]
        cmds.add_via = lambda params: {"success": True}  # type: ignore[method-assign]
        res = cmds.route_pad_to_pad(
            {"fromRef": "U4", "fromPad": "1", "toRef": "U4", "toPad": "2", "force": True}
        )
        assert res.get("errorCode") != "CROSS_NET_SHORT"
        assert res["success"] is True

    def test_same_net_not_refused(self):
        board = _mock_board(
            {"U4": [_mock_pad("1", 5.0, 25.0, "/GND"), _mock_pad("2", 45.0, 25.0, "/GND")]}
        )
        cmds = _cmds(board)
        cmds.route_trace = lambda params: {"success": True}  # type: ignore[method-assign]
        cmds.add_via = lambda params: {"success": True}  # type: ignore[method-assign]
        res = cmds.route_pad_to_pad({"fromRef": "U4", "fromPad": "1", "toRef": "U4", "toPad": "2"})
        assert res.get("errorCode") != "CROSS_NET_SHORT"
        assert res["success"] is True


# ---------------------------------------------------------------------------
# route_smart
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRouteSmartCrossNet:
    def _board(self, net_a="/GND", net_b="/+3V3"):
        return _mock_board(
            {"U4": [_mock_pad("1", 5.0, 25.0, net_a), _mock_pad("2", 45.0, 25.0, net_b)]}
        )

    def test_cross_net_refused_before_astar(self):
        cmds = RoutingCommands(self._board())
        res = cmds.route_smart({"fromRef": "U4", "fromPad": "1", "toRef": "U4", "toPad": "2"})
        assert res["success"] is False
        assert res["errorCode"] == "CROSS_NET_SHORT"

    def test_force_bypasses_cross_net_gate(self):
        cmds = RoutingCommands(self._board())
        res = cmds.route_smart(
            {"fromRef": "U4", "fromPad": "1", "toRef": "U4", "toPad": "2", "force": True}
        )
        # force takes us PAST the cross-net gate (the A* may then fail with
        # NO_PATH because the destination pad is a foreign-net obstacle — that
        # is a different, honest outcome, not CROSS_NET_SHORT).
        assert res.get("errorCode") != "CROSS_NET_SHORT"

    def test_same_net_routes(self):
        cmds = RoutingCommands(self._board(net_a="/GND", net_b="/GND"))
        res = cmds.route_smart({"fromRef": "U4", "fromPad": "1", "toRef": "U4", "toPad": "2"})
        assert res["success"] is True
        assert res.get("errorCode") != "CROSS_NET_SHORT"


# ---------------------------------------------------------------------------
# route_trace
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRouteTraceCrossNet:
    def _board(self):
        # /+3V3 pad sits at the trace END (45, 25); /GND pad at the START.
        return _mock_board(
            {"U4": [_mock_pad("1", 5.0, 25.0, "/GND"), _mock_pad("2", 45.0, 25.0, "/+3V3")]}
        )

    def test_endpoint_on_foreign_pad_refused(self):
        cmds = RoutingCommands(self._board())
        res = cmds.route_trace(
            {
                "start": {"x": 5, "y": 25, "unit": "mm"},
                "end": {"x": 45, "y": 25, "unit": "mm"},
                "layer": "F.Cu",
                "width": 0.25,
                "net": "/GND",
            }
        )
        assert res["success"] is False
        assert res["errorCode"] == "CROSS_NET_SHORT"

    def test_force_overrides(self):
        cmds = RoutingCommands(self._board())
        res = cmds.route_trace(
            {
                "start": {"x": 5, "y": 25, "unit": "mm"},
                "end": {"x": 45, "y": 25, "unit": "mm"},
                "layer": "F.Cu",
                "width": 0.25,
                "net": "/GND",
                "force": True,
            }
        )
        assert res.get("errorCode") != "CROSS_NET_SHORT"
        assert res["success"] is True

    def test_no_net_skips_check(self):
        # Arc/no-net segments have nothing to compare — must not be refused.
        cmds = RoutingCommands(self._board())
        res = cmds.route_trace(
            {
                "start": {"x": 5, "y": 25, "unit": "mm"},
                "end": {"x": 45, "y": 25, "unit": "mm"},
                "layer": "F.Cu",
                "width": 0.25,
            }
        )
        assert res.get("errorCode") != "CROSS_NET_SHORT"
        assert res["success"] is True


# ---------------------------------------------------------------------------
# IPC handle_route_trace
# ---------------------------------------------------------------------------


def _ipc_pad(number, x_mm, y_mm, net, size_mm=1.0):
    return SimpleNamespace(
        number=number,
        net=SimpleNamespace(name=net),
        position=SimpleNamespace(x=int(x_mm * _NM), y=int(y_mm * _NM)),
        padstack=SimpleNamespace(
            copper_layers=[
                SimpleNamespace(size=SimpleNamespace(x=int(size_mm * _NM), y=int(size_mm * _NM)))
            ]
        ),
    )


def _ipc_footprint(ref, pads):
    return SimpleNamespace(
        reference_field=SimpleNamespace(text=SimpleNamespace(value=ref)),
        definition=SimpleNamespace(pads=pads),
    )


class _FakeIPCBoardAPI:
    def __init__(self, footprints, add_ok=True, raise_on_footprints=False):
        self._footprints = footprints
        self._raise = raise_on_footprints
        self.add_ok = add_ok
        self.add_track_calls = []

    def _get_board(self):
        api = self

        class _Board:
            def get_footprints(self_inner):
                if api._raise:
                    raise RuntimeError("no footprints over IPC")
                return api._footprints

        return _Board()

    def add_track(self, **kwargs):
        self.add_track_calls.append(kwargs)
        return self.add_ok


def _ipc_iface(footprints, add_ok=True, raise_on_footprints=False):
    return SimpleNamespace(ipc_board_api=_FakeIPCBoardAPI(footprints, add_ok, raise_on_footprints))


@pytest.mark.unit
class TestIpcRouteTraceCrossNet:
    def _footprints(self):
        return [_ipc_footprint("U4", [_ipc_pad("2", 45.0, 25.0, "/+3V3")])]

    def test_cross_net_refused(self):
        from handlers.ipc_fastpath._routing import handle_route_trace

        iface = _ipc_iface(self._footprints())
        res = handle_route_trace(
            iface,
            {
                "start": {"x": 5, "y": 25},
                "end": {"x": 45, "y": 25},
                "layer": "F.Cu",
                "net": "/GND",
            },
        )
        assert res["success"] is False
        assert res["errorCode"] == "CROSS_NET_SHORT"
        assert iface.ipc_board_api.add_track_calls == []  # refused before mutating

    def test_force_overrides(self):
        from handlers.ipc_fastpath._routing import handle_route_trace

        iface = _ipc_iface(self._footprints())
        res = handle_route_trace(
            iface,
            {
                "start": {"x": 5, "y": 25},
                "end": {"x": 45, "y": 25},
                "layer": "F.Cu",
                "net": "/GND",
                "force": True,
            },
        )
        assert res["success"] is True
        assert len(iface.ipc_board_api.add_track_calls) == 1

    def test_same_net_routes(self):
        from handlers.ipc_fastpath._routing import handle_route_trace

        iface = _ipc_iface([_ipc_footprint("U4", [_ipc_pad("2", 45.0, 25.0, "/GND")])])
        res = handle_route_trace(
            iface,
            {
                "start": {"x": 5, "y": 25},
                "end": {"x": 45, "y": 25},
                "layer": "F.Cu",
                "net": "/GND",
            },
        )
        assert res["success"] is True
        assert len(iface.ipc_board_api.add_track_calls) == 1

    def test_unreadable_pads_degrade_to_allow(self):
        from handlers.ipc_fastpath._routing import handle_route_trace

        iface = _ipc_iface(self._footprints(), raise_on_footprints=True)
        res = handle_route_trace(
            iface,
            {
                "start": {"x": 5, "y": 25},
                "end": {"x": 45, "y": 25},
                "layer": "F.Cu",
                "net": "/GND",
            },
        )
        # Can't read pads → best-effort guard degrades to allowing the route.
        assert res["success"] is True
        assert len(iface.ipc_board_api.add_track_calls) == 1
