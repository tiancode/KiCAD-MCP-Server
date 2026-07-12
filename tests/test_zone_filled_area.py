"""Bug 3 regression: query_copper(zones) reported filledArea: 0 for filled zones.

``ZONE.GetFilledArea()`` returns the *cached* ``m_area``, which is 0 for a
zone freshly loaded from disk (the cache is only populated by a fill pass or
``CalculateFilledArea()``).  A GD32 board with 6 ``filled_polygon`` blocks in
the .kicad_pcb therefore reported ``isFilled: true`` but ``filledArea: 0``.
These tests pin the SWIG API-call order + IU²→mm² conversion, and that a
backend that can't compute the area reports ``null`` — never a fake 0.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.routing._zones import ZoneMixin, _zone_filled_area_mm2  # noqa: E402

IU_PER_MM2 = 1000000 * 1000000  # nm² per mm²

# ---------------------------------------------------------------------------
# _zone_filled_area_mm2 — API-call order and conversion
# ---------------------------------------------------------------------------


def test_calculate_filled_area_wins_over_stale_cache() -> None:
    """The disk-load repro: cached GetFilledArea is 0, recompute is real."""
    zone = MagicMock()
    zone.CalculateFilledArea.return_value = int(12.5 * IU_PER_MM2)
    zone.GetFilledArea.return_value = 0

    assert _zone_filled_area_mm2(zone) == pytest.approx(12.5)
    zone.CalculateFilledArea.assert_called_once()


def test_iu2_to_mm2_conversion() -> None:
    zone = MagicMock()
    zone.CalculateFilledArea.return_value = 3 * IU_PER_MM2
    assert _zone_filled_area_mm2(zone) == pytest.approx(3.0)


def test_falls_back_to_cached_getter_when_no_calculate() -> None:
    zone = MagicMock(spec=["GetFilledArea"])
    zone.GetFilledArea.return_value = 2 * IU_PER_MM2
    assert _zone_filled_area_mm2(zone) == pytest.approx(2.0)


def test_falls_back_when_calculate_raises() -> None:
    zone = MagicMock(spec=["CalculateFilledArea", "GetFilledArea"])
    zone.CalculateFilledArea.side_effect = RuntimeError("SWIG drift")
    zone.GetFilledArea.return_value = 7 * IU_PER_MM2
    assert _zone_filled_area_mm2(zone) == pytest.approx(7.0)


def test_unfilled_zone_reports_real_zero() -> None:
    zone = MagicMock(spec=["CalculateFilledArea", "GetFilledArea"])
    zone.CalculateFilledArea.return_value = 0
    zone.GetFilledArea.return_value = 0
    assert _zone_filled_area_mm2(zone) == 0.0


def test_none_when_no_area_api_at_all() -> None:
    zone = MagicMock(spec=[])  # neither method exists
    assert _zone_filled_area_mm2(zone) is None


def test_none_when_api_returns_non_numeric() -> None:
    zone = MagicMock()  # bare mock: both methods return MagicMocks
    assert _zone_filled_area_mm2(zone) is None


# ---------------------------------------------------------------------------
# SWIG query_zones surfaces the recomputed area
# ---------------------------------------------------------------------------


def _stub_zone(uuid: str, area_iu2: int) -> MagicMock:
    zone = MagicMock(name=f"zone_{uuid}")
    zone.m_Uuid.AsString.return_value = uuid
    zone.GetNetname.return_value = "/GND"
    zone.GetNetCode.return_value = 2
    zone.GetLayer.return_value = 31
    zone.IsFilled.return_value = True
    zone.GetMinThickness.return_value = 200000
    zone.GetAssignedPriority.return_value = 0
    bb = MagicMock()
    bb.GetLeft.return_value = 0
    bb.GetTop.return_value = 0
    bb.GetRight.return_value = 10000000
    bb.GetBottom.return_value = 10000000
    zone.GetBoundingBox.return_value = bb
    zone.CalculateFilledArea.return_value = area_iu2
    zone.GetFilledArea.return_value = 0  # the stale disk-load cache
    return zone


class _ZoneHost(ZoneMixin):
    def __init__(self, zones: List[MagicMock]):
        self.board = MagicMock(name="board")
        self.board.Zones.return_value = zones
        self.board.GetLayerID.side_effect = lambda n: {"F.Cu": 0, "B.Cu": 31}.get(n, -1)
        self.board.GetLayerName.side_effect = lambda lid: {0: "F.Cu", 31: "B.Cu"}.get(lid, "?")

    def _board_net_names(self) -> List[str]:
        return ["", "/GND"]


def test_swig_query_zones_reports_real_area_for_disk_loaded_fill() -> None:
    host = _ZoneHost([_stub_zone("u1", int(42.5 * IU_PER_MM2))])

    result = host.query_zones({})

    assert result["success"] is True
    assert result["zones"][0]["isFilled"] is True
    assert result["zones"][0]["filledArea"] == pytest.approx(42.5)


def test_swig_query_zones_reports_null_when_area_unobtainable() -> None:
    zone = _stub_zone("u1", 0)
    del zone.CalculateFilledArea  # emulate an API without either method
    del zone.GetFilledArea
    host = _ZoneHost([zone])

    result = host.query_zones({})

    assert result["zones"][0]["filledArea"] is None  # null, never a fake 0


# ---------------------------------------------------------------------------
# IPC query_zones: no area API over IPC — always null, never 0
# ---------------------------------------------------------------------------


def test_ipc_query_zones_filled_area_is_null(monkeypatch) -> None:
    for mod in ("kipy", "kipy.util", "kipy.util.units"):
        monkeypatch.setitem(sys.modules, mod, MagicMock())
    monkeypatch.setattr(
        "handlers.ipc_fastpath._zones._zone_uuid_str", lambda z: str(getattr(z, "id", ""))
    )
    from handlers.ipc_fastpath._zones import handle_query_zones
    from kicad_interface import KiCADInterface

    zone = MagicMock(name="kipy_zone")
    zone.id = "z1"
    net_obj = MagicMock()
    net_obj.name = "/GND"
    zone.net = net_obj
    zone.layers = []
    zone.filled = True
    zone.priority = 0

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    iface.ipc_board_api = MagicMock()
    iface.ipc_board_api.get_nets.return_value = [{"name": "/GND"}]
    board = MagicMock(name="kipy_board")
    board.get_zones.return_value = [zone]
    iface.ipc_board_api._get_board.return_value = board

    result = handle_query_zones(iface, {})

    assert result["success"] is True, result
    assert result["zones"][0]["isFilled"] is True
    assert result["zones"][0]["filledArea"] is None
