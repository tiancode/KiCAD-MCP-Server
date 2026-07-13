"""P2: route_smart must honour a net's netclass trace/via width.

In KiCad 9/10 net-class *membership* lives in the ``.kicad_pro`` project JSON
(``netclass_assignments`` + wildcard ``netclass_patterns``), NOT in the SWIG
board — so ``NETINFO_ITEM.GetNetClass()`` returns Default for a net the user
assigned via ``assign_net_to_class``.  route_smart therefore routed a Power-class
net at the global default (0.25) instead of the class's 0.5 mm.

The fix resolves the net's class straight from the project JSON.  These tests
pin the pure resolvers, the project-props reader, the default-width fallback
chain, and the end-to-end route_smart width on a stubbed board whose sibling
.kicad_pro assigns the net to a Power class.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.routing import RoutingCommands  # noqa: E402
from commands.routing._nets import (  # noqa: E402
    netclass_property,
    resolve_netclass_name,
)

_NM = 1_000_000


# ---------------------------------------------------------------------------
# Synthesized net_settings with a Power class (track 0.5, via 0.8/0.4)
# ---------------------------------------------------------------------------
def _net_settings():
    return {
        "classes": [
            {"name": "Default", "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "Power", "track_width": 0.5, "via_diameter": 0.8, "via_drill": 0.4},
        ],
        "netclass_assignments": {"/+5V": "Power", "/+3V3": "Power"},
        "netclass_patterns": [{"netclass": "Power", "pattern": "*VBUS"}],
    }


# ---------------------------------------------------------------------------
# Pure resolvers
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestPureResolvers:
    def test_exact_assignment_wins(self):
        assert resolve_netclass_name(_net_settings(), "/+5V") == "Power"

    def test_pattern_match(self):
        assert resolve_netclass_name(_net_settings(), "/USB_VBUS") == "Power"

    def test_unassigned_net_is_none(self):
        assert resolve_netclass_name(_net_settings(), "/SDA") is None

    def test_empty_inputs(self):
        assert resolve_netclass_name(_net_settings(), "") is None
        assert resolve_netclass_name(None, "/+5V") is None

    def test_netclass_property_reads_mm(self):
        ns = _net_settings()
        assert netclass_property(ns, "Power", "track_width") == 0.5
        assert netclass_property(ns, "Power", "via_diameter") == 0.8
        assert netclass_property(ns, "Power", "via_drill") == 0.4

    def test_netclass_property_missing_class_or_key(self):
        ns = _net_settings()
        assert netclass_property(ns, "Nope", "track_width") is None
        assert netclass_property(ns, "Power", "nonexistent") is None
        assert netclass_property(ns, None, "track_width") is None


# ---------------------------------------------------------------------------
# Project-props reader + default-width fallback chain (stubbed board + real
# .kicad_pro on disk)
# ---------------------------------------------------------------------------
def _board_with_project(tmp_path, ns):
    """A MagicMock board whose sibling .kicad_pro carries ``ns`` net_settings."""
    pcb = tmp_path / "proj.kicad_pcb"
    pro = tmp_path / "proj.kicad_pro"
    pro.write_text(json.dumps({"net_settings": ns}, indent="  ") + "\n", encoding="utf-8")
    board = MagicMock()
    board.GetFileName.return_value = str(pcb)
    return board


@pytest.mark.unit
class TestProjectNetclassProps:
    def test_resolves_power_props(self, tmp_path):
        rc = RoutingCommands(_board_with_project(tmp_path, _net_settings()))
        props = rc._project_netclass_props("/+5V")
        assert props["className"] == "Power"
        assert props["track_width"] == 0.5
        assert props["via_diameter"] == 0.8
        assert props["via_drill"] == 0.4

    def test_unassigned_net_empty(self, tmp_path):
        rc = RoutingCommands(_board_with_project(tmp_path, _net_settings()))
        assert rc._project_netclass_props("/SDA") == {}

    def test_no_net_empty(self, tmp_path):
        rc = RoutingCommands(_board_with_project(tmp_path, _net_settings()))
        assert rc._project_netclass_props(None) == {}

    def test_missing_project_file_empty(self):
        # GetFileName points nowhere -> no project -> empty (never raises).
        board = MagicMock()
        board.GetFileName.return_value = "/nonexistent/proj.kicad_pcb"
        rc = RoutingCommands(board)
        assert rc._project_netclass_props("/+5V") == {}


@pytest.mark.unit
class TestSmartDefaultWidth:
    def test_prefers_project_netclass_width(self, tmp_path):
        rc = RoutingCommands(_board_with_project(tmp_path, _net_settings()))
        # Even though the SWIG board would report Default (0.25), the .kicad_pro
        # Power assignment must win.
        assert rc._smart_default_width_mm("/+5V") == 0.5

    def test_falls_back_to_board_default_when_unassigned(self, tmp_path):
        board = _board_with_project(tmp_path, _net_settings())
        board.GetNetInfo.return_value.GetNetItem.return_value.GetNetClass.return_value = None
        board.GetDesignSettings.return_value.GetCurrentTrackWidth.return_value = int(0.3 * _NM)
        rc = RoutingCommands(board)
        assert rc._smart_default_width_mm("/SDA") == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# End-to-end route_smart width (the reported P2 symptom)
# ---------------------------------------------------------------------------
def _mock_pad(number, x_mm, y_mm, net):
    pad = MagicMock()
    pad.GetNumber.return_value = number
    pos = MagicMock()
    pos.x, pos.y = int(x_mm * _NM), int(y_mm * _NM)
    pad.GetPosition.return_value = pos
    pad.GetNetname.return_value = net
    pad.HasHole.return_value = True  # through-hole -> any layer, no via needed
    pad.IsOnLayer.return_value = True
    bb = MagicMock()
    half = int(0.5 * _NM)
    bb.GetLeft.return_value = pos.x - half
    bb.GetRight.return_value = pos.x + half
    bb.GetTop.return_value = pos.y - half
    bb.GetBottom.return_value = pos.y + half
    pad.GetBoundingBox.return_value = bb
    return pad


def _route_board(tmp_path, ns):
    board = _board_with_project(tmp_path, ns)
    fps = []
    for ref, pad in (
        ("J1", _mock_pad("A9", 5.0, 25.0, "/+5V")),
        ("U4", _mock_pad("3", 45.0, 25.0, "/+5V")),
    ):
        fp = MagicMock()
        fp.GetReference.return_value = ref
        fp.Pads.return_value = [pad]
        fps.append(fp)
    board.GetFootprints.return_value = fps
    board.Tracks.return_value = []
    board.GetLayerID.side_effect = lambda name: {"F.Cu": 0, "B.Cu": 31}.get(name, -1)
    board.GetLayerName.side_effect = lambda lid: {0: "F.Cu", 31: "B.Cu"}.get(lid, "?")
    bbox = MagicMock()
    bbox.GetLeft.return_value = 0
    bbox.GetTop.return_value = 0
    bbox.GetRight.return_value = int(50 * _NM)
    bbox.GetBottom.return_value = int(50 * _NM)
    board.GetBoardEdgesBoundingBox.return_value = bbox
    design = MagicMock()
    design.GetCurrentTrackWidth.return_value = int(0.25 * _NM)  # global default
    design.GetCurrentViaSize.return_value = int(0.6 * _NM)
    design.GetCurrentViaDrill.return_value = int(0.3 * _NM)
    board.GetDesignSettings.return_value = design
    net_item = MagicMock()
    net_item.GetNetCode.return_value = 7
    net_item.GetNetClass.return_value = None  # SWIG board doesn't know the class
    board.GetNetInfo.return_value.GetNetItem.return_value = net_item
    return board


@pytest.mark.unit
class TestRouteSmartHonoursNetclassWidth:
    def test_power_net_routes_at_netclass_width(self, tmp_path):
        board = _route_board(tmp_path, _net_settings())
        rc = RoutingCommands(board)
        res = rc.route_smart({"fromRef": "J1", "fromPad": "A9", "toRef": "U4", "toPad": "3"})
        assert res["success"], res
        # The reported bug: widthMm was 0.25 (global default) despite the Power
        # class. It must now be 0.5 (the /+5V netclass width) with no explicit width.
        assert res["widthMm"] == pytest.approx(0.5)

    def test_explicit_width_still_overrides(self, tmp_path):
        board = _route_board(tmp_path, _net_settings())
        rc = RoutingCommands(board)
        res = rc.route_smart(
            {"fromRef": "J1", "fromPad": "A9", "toRef": "U4", "toPad": "3", "width": 0.8}
        )
        assert res["success"], res
        assert res["widthMm"] == pytest.approx(0.8)

    def test_unassigned_net_uses_global_default(self, tmp_path):
        # A net with no class assignment keeps the global 0.25 default.
        ns = _net_settings()
        ns["netclass_assignments"] = {}
        ns["netclass_patterns"] = []
        board = _route_board(tmp_path, ns)
        rc = RoutingCommands(board)
        res = rc.route_smart({"fromRef": "J1", "fromPad": "A9", "toRef": "U4", "toPad": "3"})
        assert res["success"], res
        assert res["widthMm"] == pytest.approx(0.25)


@pytest.mark.unit
class TestRoutePadToPadHonoursNetclassWidth:
    """route_pad_to_pad shares P2's fix: project netclass width wins when
    the caller omits width (the SWIG netclass reports Default there too)."""

    def _rc_with_spy(self, tmp_path, ns, monkeypatch):
        board = _route_board(tmp_path, ns)
        for fp in board.GetFootprints.return_value:
            fp.GetLayer.return_value = 0  # both on F.Cu -> same-layer branch
        rc = RoutingCommands(board)
        captured = {}

        def fake_route_trace(params):
            captured.update(params)
            return {"success": True, "message": "ok"}

        monkeypatch.setattr(rc, "route_trace", fake_route_trace)
        return rc, captured

    def test_power_net_defaults_to_netclass_width(self, tmp_path, monkeypatch):
        rc, captured = self._rc_with_spy(tmp_path, _net_settings(), monkeypatch)
        res = rc.route_pad_to_pad(
            {"fromRef": "J1", "fromPad": "A9", "toRef": "U4", "toPad": "3"}
        )
        assert res["success"], res
        assert captured["width"] == pytest.approx(0.5)

    def test_explicit_width_still_overrides(self, tmp_path, monkeypatch):
        rc, captured = self._rc_with_spy(tmp_path, _net_settings(), monkeypatch)
        res = rc.route_pad_to_pad(
            {"fromRef": "J1", "fromPad": "A9", "toRef": "U4", "toPad": "3", "width": 0.8}
        )
        assert res["success"], res
        assert captured["width"] == pytest.approx(0.8)

    def test_unassigned_net_keeps_none_for_route_trace_default(self, tmp_path, monkeypatch):
        ns = _net_settings()
        ns["netclass_assignments"] = {}
        ns["netclass_patterns"] = []
        rc, captured = self._rc_with_spy(tmp_path, ns, monkeypatch)
        for fp in rc.board.GetFootprints.return_value:
            fp.Pads.return_value[0].GetNet.return_value = None  # no SWIG netclass either
        res = rc.route_pad_to_pad(
            {"fromRef": "J1", "fromPad": "A9", "toRef": "U4", "toPad": "3"}
        )
        assert res["success"], res
        # No class anywhere -> width stays None so route_trace applies its
        # own GetCurrentTrackWidth() fallback, exactly as before the fix.
        assert captured["width"] is None
