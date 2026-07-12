"""Bug 2 regression: query_copper's net filter must resolve net names.

``query_copper(net="GND")`` returned 0 vias while ``net="/GND"`` returned 23 —
the query paths compared the requested name verbatim while copper_pour and
routing resolve it through ``resolve_net_name`` (GD32F103VET6 E2E run).  Pins:

  * the pure ``resolve_query_net_filter`` helper (resolve, annotate, never
    refuse);
  * SWIG ``query_traces`` / ``query_zones`` and the IPC fast-path equivalents
    resolving a bare name to the hierarchical net, keeping exact matches
    exact, and annotating ``netCandidates`` for genuinely-unknown nets
    (empty result, no refusal);
  * the audit fixes that fell out of the same TS↔Python param sweep:
    query_traces honouring the documented ``unit`` param, and ``add_net``
    honouring ``netClass`` (was read as ``class`` and silently dropped).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

import pcbnew  # noqa: E402  (stubbed by tests/conftest.py)
from commands.routing._nets import NetMixin  # noqa: E402
from commands.routing._traces import TraceMixin  # noqa: E402
from commands.routing._zones import ZoneMixin, resolve_query_net_filter  # noqa: E402

BOARD_NETS = ["", "/GND", "/+3V3", "/USART1_TX"]

# ---------------------------------------------------------------------------
# resolve_query_net_filter — pure behaviour
# ---------------------------------------------------------------------------


def test_filter_resolves_bare_to_hierarchical_with_annotations() -> None:
    target, notes = resolve_query_net_filter("GND", BOARD_NETS)
    assert target == "/GND"
    assert notes == {"resolvedNet": "/GND", "requestedNet": "GND"}


def test_filter_exact_match_has_no_annotations() -> None:
    assert resolve_query_net_filter("/GND", BOARD_NETS) == ("/GND", {})


def test_filter_unknown_net_keeps_literal_and_lists_candidates() -> None:
    target, notes = resolve_query_net_filter("VBUS", BOARD_NETS)
    assert target == "VBUS"  # literal → empty query result, never a refusal
    assert "/GND" in notes["netCandidates"]


def test_filter_empty_request_passes_through() -> None:
    assert resolve_query_net_filter(None, BOARD_NETS) == (None, {})
    assert resolve_query_net_filter("", BOARD_NETS) == ("", {})


# ---------------------------------------------------------------------------
# SWIG query_traces
# ---------------------------------------------------------------------------


def _stub_track(net: str, is_via: bool = False, nm: int = 1000000) -> MagicMock:
    track = MagicMock(name=f"track_{net}")
    track.Type.return_value = pcbnew.PCB_VIA_T if is_via else object()
    track.GetNetname.return_value = net
    track.GetNetCode.return_value = 1
    track.m_Uuid.AsString.return_value = f"t-{net}-{'via' if is_via else 'seg'}"
    track.GetLayer.return_value = 0
    track.GetWidth.return_value = nm  # 1 mm
    track.GetLength.return_value = 5 * nm
    track.GetDrillValue.return_value = nm // 2
    pos = MagicMock(x=2 * nm, y=3 * nm)
    track.GetPosition.return_value = pos
    track.GetStart.return_value = MagicMock(x=0, y=0)
    track.GetEnd.return_value = MagicMock(x=3 * nm, y=4 * nm)
    return track


class _TraceHost(NetMixin, ZoneMixin):
    def __init__(self, tracks: List[MagicMock], net_names: List[str]):
        self._net_names = net_names
        self.board = MagicMock(name="board")
        self.board.Tracks.return_value = tracks
        self.board.GetLayerID.side_effect = lambda n: {"F.Cu": 0, "B.Cu": 31}.get(n, -1)
        self.board.GetLayerName.side_effect = lambda lid: {0: "F.Cu", 31: "B.Cu"}.get(lid, "?")

    def _board_net_names(self) -> List[str]:
        return list(self._net_names)


def test_swig_query_traces_bare_net_matches_hierarchical() -> None:
    host = _TraceHost(
        [_stub_track("/GND"), _stub_track("/GND", is_via=True), _stub_track("/+3V3")],
        BOARD_NETS,
    )

    result = host.query_traces({"net": "GND", "includeVias": True})

    assert result["success"] is True
    assert result["traceCount"] == 1
    assert result["viaCount"] == 1
    assert result["resolvedNet"] == "/GND"
    assert result["requestedNet"] == "GND"


def test_swig_query_traces_exact_net_still_works_unannotated() -> None:
    host = _TraceHost([_stub_track("/GND")], BOARD_NETS)

    result = host.query_traces({"net": "/GND"})

    assert result["traceCount"] == 1
    assert "resolvedNet" not in result
    assert "netCandidates" not in result


def test_swig_query_traces_unknown_net_annotates_candidates() -> None:
    host = _TraceHost([_stub_track("/GND")], BOARD_NETS)

    result = host.query_traces({"net": "VBUS"})

    assert result["success"] is True
    assert result["traceCount"] == 0  # empty, not refused
    assert "/GND" in result["netCandidates"]


def test_swig_query_traces_honours_unit_param() -> None:
    """Audit fix: the documented `unit` param was silently ignored (always mm)."""
    host = _TraceHost([_stub_track("/GND"), _stub_track("/GND", is_via=True)], BOARD_NETS)

    result = host.query_traces({"net": "/GND", "unit": "mil", "includeVias": True})

    trace = result["traces"][0]
    assert trace["end"]["unit"] == "mil"
    # 3 mm ≈ 118.11 mil
    assert trace["end"]["x"] == pytest.approx(3000000 / 25400)
    assert trace["width"] == pytest.approx(1000000 / 25400)
    via = result["vias"][0]
    assert via["position"]["unit"] == "mil"
    assert via["diameter"] == pytest.approx(1000000 / 25400)


# ---------------------------------------------------------------------------
# SWIG query_zones
# ---------------------------------------------------------------------------


def _stub_zone(uuid: str, net: str, layer_id: int = 31) -> MagicMock:
    zone = MagicMock(name=f"zone_{uuid}")
    zone.m_Uuid.AsString.return_value = uuid
    zone.GetNetname.return_value = net
    zone.GetNetCode.return_value = 2
    zone.GetLayer.return_value = layer_id
    zone.IsFilled.return_value = True
    zone.GetMinThickness.return_value = 200000
    zone.GetAssignedPriority.return_value = 0
    bb = MagicMock()
    bb.GetLeft.return_value = 0
    bb.GetTop.return_value = 0
    bb.GetRight.return_value = 10000000
    bb.GetBottom.return_value = 10000000
    zone.GetBoundingBox.return_value = bb
    zone.CalculateFilledArea.return_value = 0
    zone.GetFilledArea.return_value = 0
    return zone


class _ZoneHost(ZoneMixin):
    def __init__(self, zones: List[MagicMock], net_names: List[str]):
        self._net_names = net_names
        self.board = MagicMock(name="board")
        self.board.Zones.return_value = zones
        self.board.GetLayerID.side_effect = lambda n: {"F.Cu": 0, "B.Cu": 31}.get(n, -1)
        self.board.GetLayerName.side_effect = lambda lid: {0: "F.Cu", 31: "B.Cu"}.get(lid, "?")

    def _board_net_names(self) -> List[str]:
        return list(self._net_names)


def test_swig_query_zones_bare_net_matches_hierarchical() -> None:
    host = _ZoneHost([_stub_zone("u1", "/GND"), _stub_zone("u2", "/+3V3")], BOARD_NETS)

    result = host.query_zones({"net": "GND"})

    assert result["success"] is True
    assert result["zoneCount"] == 1
    assert result["zones"][0]["uuid"] == "u1"
    assert result["resolvedNet"] == "/GND"
    assert result["requestedNet"] == "GND"


def test_swig_query_zones_exact_net_unannotated() -> None:
    host = _ZoneHost([_stub_zone("u1", "/GND")], BOARD_NETS)

    result = host.query_zones({"net": "/GND"})

    assert result["zoneCount"] == 1
    assert "resolvedNet" not in result


def test_swig_query_zones_unknown_net_annotates_candidates() -> None:
    host = _ZoneHost([_stub_zone("u1", "/GND")], BOARD_NETS)

    result = host.query_zones({"net": "VBUS"})

    assert result["success"] is True
    assert result["zoneCount"] == 0
    assert "/GND" in result["netCandidates"]


# ---------------------------------------------------------------------------
# IPC fast-path query_traces
# ---------------------------------------------------------------------------


class _FakeIPCBoardAPI:
    def __init__(self, nets: List[str]):
        self._nets = nets

    def get_tracks(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "track-1",
                "start": {"x": 0, "y": 0},
                "end": {"x": 3, "y": 4},
                "width": 0.25,
                "layer": "BL_F_Cu",
                "net": "/GND",
                "netCode": 1,
            },
            {
                "id": "track-2",
                "start": {"x": 10, "y": 10},
                "end": {"x": 11, "y": 11},
                "width": 0.2,
                "layer": "BL_B_Cu",
                "net": "/+3V3",
                "netCode": 2,
            },
        ]

    def get_vias(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "via-1",
                "position": {"x": 0.5, "y": 0.5},
                "diameter": 0.8,
                "drill": 0.4,
                "net": "/GND",
                "netCode": 1,
            }
        ]

    def get_nets(self) -> List[Dict[str, Any]]:
        return [{"name": n, "code": i} for i, n in enumerate(self._nets)]


def _ipc_iface(nets: List[str]):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    iface.ipc_board_api = _FakeIPCBoardAPI(nets)
    return iface


def test_ipc_query_traces_bare_net_matches_hierarchical() -> None:
    from handlers.ipc_fastpath._routing import handle_query_traces

    result = handle_query_traces(_ipc_iface(["/GND", "/+3V3"]), {"net": "GND", "includeVias": True})

    assert result["success"] is True, result
    assert result["traceCount"] == 1
    assert result["traces"][0]["uuid"] == "track-1"
    assert result["viaCount"] == 1  # the E2E repro: GND vias were reported as 0
    assert result["resolvedNet"] == "/GND"
    assert result["requestedNet"] == "GND"


def test_ipc_query_traces_exact_net_unannotated() -> None:
    from handlers.ipc_fastpath._routing import handle_query_traces

    result = handle_query_traces(_ipc_iface(["/GND", "/+3V3"]), {"net": "/GND"})

    assert result["traceCount"] == 1
    assert "resolvedNet" not in result


def test_ipc_query_traces_unknown_net_annotates_candidates() -> None:
    from handlers.ipc_fastpath._routing import handle_query_traces

    result = handle_query_traces(_ipc_iface(["/GND", "/+3V3"]), {"net": "VBUS"})

    assert result["success"] is True
    assert result["traceCount"] == 0
    assert "/GND" in result["netCandidates"]


def test_ipc_query_traces_honours_unit_param() -> None:
    from handlers.ipc_fastpath._routing import handle_query_traces

    result = handle_query_traces(
        _ipc_iface(["/GND"]), {"net": "/GND", "unit": "inch", "includeVias": True}
    )

    trace = result["traces"][0]
    assert trace["end"]["unit"] == "inch"
    assert trace["end"]["x"] == pytest.approx(3 / 25.4)
    assert trace["length"] == pytest.approx(5 / 25.4)
    assert result["vias"][0]["diameter"] == pytest.approx(0.8 / 25.4)


# ---------------------------------------------------------------------------
# IPC fast-path query_zones
# ---------------------------------------------------------------------------


def _fake_kipy_zone(uuid: str, net: str) -> MagicMock:
    zone = MagicMock(name=f"kipy_zone_{uuid}")
    zone.id = uuid
    net_obj = MagicMock()
    net_obj.name = net
    zone.net = net_obj
    zone.layers = []
    zone.filled = True
    zone.priority = 0
    return zone


def _ipc_zone_iface(zones: List[MagicMock], nets: List[str]):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    iface.ipc_board_api = MagicMock()
    iface.ipc_board_api.get_nets.return_value = [{"name": n} for n in nets]
    board = MagicMock(name="kipy_board")
    board.get_zones.return_value = zones
    iface.ipc_board_api._get_board.return_value = board
    return iface


@pytest.fixture
def _kipy_stubbed(monkeypatch):
    """handle_query_zones imports kipy.util.units at call time."""
    for mod in ("kipy", "kipy.util", "kipy.util.units"):
        monkeypatch.setitem(sys.modules, mod, MagicMock())
    monkeypatch.setattr(
        "handlers.ipc_fastpath._zones._zone_uuid_str", lambda z: str(getattr(z, "id", ""))
    )


def test_ipc_query_zones_bare_net_matches_hierarchical(_kipy_stubbed) -> None:
    from handlers.ipc_fastpath._zones import handle_query_zones

    iface = _ipc_zone_iface(
        [_fake_kipy_zone("z1", "/GND"), _fake_kipy_zone("z2", "/+3V3")], ["/GND", "/+3V3"]
    )

    result = handle_query_zones(iface, {"net": "GND"})

    assert result["success"] is True, result
    assert result["zoneCount"] == 1
    assert result["zones"][0]["uuid"] == "z1"
    assert result["resolvedNet"] == "/GND"


def test_ipc_query_zones_unknown_net_annotates_candidates(_kipy_stubbed) -> None:
    from handlers.ipc_fastpath._zones import handle_query_zones

    iface = _ipc_zone_iface([_fake_kipy_zone("z1", "/GND")], ["/GND"])

    result = handle_query_zones(iface, {"net": "VBUS"})

    assert result["success"] is True
    assert result["zoneCount"] == 0
    assert "/GND" in result["netCandidates"]


def test_ipc_query_zones_falls_back_to_zone_nets_when_nets_not_enumerable(
    _kipy_stubbed,
) -> None:
    from handlers.ipc_fastpath._zones import handle_query_zones

    iface = _ipc_zone_iface([_fake_kipy_zone("z1", "/GND")], [])
    iface.ipc_board_api.get_nets = MagicMock(side_effect=RuntimeError("no nets"))

    result = handle_query_zones(iface, {"net": "GND"})

    assert result["zoneCount"] == 1
    assert result["resolvedNet"] == "/GND"


# ---------------------------------------------------------------------------
# Audit fix: add_net netClass (TS name) was read as `class` and dropped
# ---------------------------------------------------------------------------


class _AddNetHost(TraceMixin):
    def __init__(self) -> None:
        self.board = MagicMock(name="board")
        nets_map = MagicMock()
        nets_map.has_key.return_value = False
        self.board.GetNetInfo.return_value.NetsByName.return_value = nets_map
        netclass = MagicMock(name="netclass_Power")
        classes = MagicMock()
        classes.Find.return_value = netclass
        self.board.GetNetClasses.return_value = classes
        self.netclass = netclass


def test_add_net_honours_ts_netclass_param() -> None:
    host = _AddNetHost()

    result = host.add_net({"name": "VBUS", "netClass": "Power"})

    assert result["success"] is True
    assert result["net"]["class"] == "Power"
    host.board.GetNetClasses.return_value.Find.assert_called_once_with("Power")


def test_add_net_class_alias_still_works() -> None:
    host = _AddNetHost()

    result = host.add_net({"name": "VBUS", "class": "Power"})

    assert result["success"] is True
    assert result["net"]["class"] == "Power"
