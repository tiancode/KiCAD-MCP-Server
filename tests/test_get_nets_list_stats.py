"""Regression (C7): get_nets_list must honor includeStats + unit.

Phase C E2E found the response identical with or without includeStats — the
handler never read params["includeStats"] or params["unit"].  It now attaches
{trackCount, viaCount, totalLength} per net (reusing compute_net_lengths, the
same engine behind report_net_lengths) and reports totalLength in the requested
unit.  Without the flag the lean {name, code, class} shape is unchanged.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

import pcbnew  # noqa: E402  (stubbed by tests/conftest.py)
from commands.routing._nets import NetMixin  # noqa: E402


def _net(name, code, cls="Default"):
    n = MagicMock(name=f"net_{name}")
    n.GetNetname.return_value = name
    n.GetNetCode.return_value = code
    n.GetNetClassName.return_value = cls
    return n


def _seg(net, sx, sy, ex, ey, layer_id=0):
    t = MagicMock(name=f"seg_{net}")
    t.Type.return_value = pcbnew.PCB_TRACE_T  # not a via, not an arc
    t.GetNetname.return_value = net
    t.GetStart.return_value = MagicMock(x=sx, y=sy)
    t.GetEnd.return_value = MagicMock(x=ex, y=ey)
    t.GetLayer.return_value = layer_id
    return t


def _via(net):
    v = MagicMock(name=f"via_{net}")
    v.Type.return_value = pcbnew.PCB_VIA_T
    v.GetNetname.return_value = net
    return v


class _NetHost(NetMixin):
    def __init__(self, nets, tracks):
        self._nets = nets
        self.board = MagicMock(name="board")
        netinfo = MagicMock(name="netinfo")
        netinfo.GetNetCount.return_value = len(nets)
        netinfo.GetNetItem.side_effect = lambda code: nets[code]
        self.board.GetNetInfo.return_value = netinfo
        self.board.Tracks.return_value = tracks
        self.board.GetLayerName.side_effect = lambda lid: {0: "F.Cu", 31: "B.Cu"}.get(lid, "?")


MM = 1_000_000  # nm per mm


@pytest.mark.unit
def test_stats_attached_per_net_when_requested():
    nets = [_net("/GND", 1), _net("/SIG", 2), _net("", 0)]
    tracks = [
        _seg("/GND", 0, 0, 3 * MM, 4 * MM),  # 5 mm
        _seg("/GND", 3 * MM, 4 * MM, 3 * MM, 14 * MM),  # 10 mm
        _via("/GND"),
        _seg("/SIG", 0, 0, 1 * MM, 0),  # 1 mm
    ]
    host = _NetHost(nets, tracks)

    result = host.get_nets_list({"includeStats": True, "unit": "mm"})

    assert result["success"] is True
    assert result["unit"] == "mm"

    gnd = next(n for n in result["nets"] if n["name"] == "/GND")
    assert gnd["trackCount"] == 2
    assert gnd["viaCount"] == 1
    assert gnd["totalLength"] == pytest.approx(15.0)

    sig = next(n for n in result["nets"] if n["name"] == "/SIG")
    assert sig["trackCount"] == 1
    assert sig["viaCount"] == 0
    assert sig["totalLength"] == pytest.approx(1.0)

    # A net with no copper is zeroed, not dropped.
    empty = next(n for n in result["nets"] if n["name"] == "")
    assert empty["trackCount"] == 0
    assert empty["viaCount"] == 0
    assert empty["totalLength"] == 0.0


@pytest.mark.unit
def test_stats_honor_unit_mil():
    nets = [_net("/GND", 1)]
    tracks = [_seg("/GND", 0, 0, 25_400_000, 0)]  # 25.4 mm == 1000 mil
    host = _NetHost(nets, tracks)

    result = host.get_nets_list({"includeStats": True, "unit": "mil"})

    assert result["unit"] == "mil"
    assert result["nets"][0]["totalLength"] == pytest.approx(1000.0)


@pytest.mark.unit
def test_stats_default_unit_is_mm():
    nets = [_net("/GND", 1)]
    tracks = [_seg("/GND", 0, 0, 1 * MM, 0)]
    host = _NetHost(nets, tracks)

    result = host.get_nets_list({"includeStats": True})

    assert result["unit"] == "mm"
    assert result["nets"][0]["totalLength"] == pytest.approx(1.0)


@pytest.mark.unit
def test_lean_shape_unchanged_without_flag():
    nets = [_net("/GND", 1)]
    tracks = [_seg("/GND", 0, 0, 3 * MM, 4 * MM)]
    host = _NetHost(nets, tracks)

    result = host.get_nets_list({})

    assert result["success"] is True
    assert "unit" not in result
    assert set(result["nets"][0].keys()) == {"name", "code", "class"}
