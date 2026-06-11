"""Tests for edit_copper_pour / delete_copper_pour (ZoneMixin).

These tools close the gap where a zone created by add_copper_pour could
only have its connect_pads / outline changed by hand-editing the
.kicad_pcb file — which the MCP's own auto-save then clobbered (see
handlers/routing.py refill_zones external-change guard).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.routing._zones import ZoneMixin  # noqa: E402

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _stub_zone(uuid: str, net: str, layer_id: int, filled: bool = True) -> MagicMock:
    zone = MagicMock(name=f"zone_{uuid}")
    zone.m_Uuid.AsString.return_value = uuid
    zone.GetNetname.return_value = net
    zone.GetLayer.return_value = layer_id
    zone.IsFilled.return_value = filled
    return zone


class _Host(ZoneMixin):
    """Minimal host exposing the attributes ZoneMixin reads off self."""

    def __init__(self, zones: List[MagicMock]):
        self.board = MagicMock(name="board")
        self.board.Zones.return_value = zones
        # Layer ids: F.Cu -> 0, B.Cu -> 31, anything else -> -1
        self.board.GetLayerID.side_effect = lambda name: {"F.Cu": 0, "B.Cu": 31}.get(name, -1)
        self.board.GetLayerName.side_effect = lambda lid: {0: "F.Cu", 31: "B.Cu"}.get(lid, "?")


# ---------------------------------------------------------------------------
# delete_copper_pour
# ---------------------------------------------------------------------------


def test_delete_by_uuid_removes_single_zone() -> None:
    z1 = _stub_zone("uuid-1", "GND", 0)
    z2 = _stub_zone("uuid-2", "GND", 31)
    host = _Host([z1, z2])

    result = host.delete_copper_pour({"uuid": "uuid-1"})

    assert result["success"] is True
    host.board.Remove.assert_called_once_with(z1)
    assert result["deleted"][0]["uuid"] == "uuid-1"


def test_delete_refuses_multi_match_without_all() -> None:
    z1 = _stub_zone("uuid-1", "GND", 0)
    z2 = _stub_zone("uuid-2", "GND", 31)
    host = _Host([z1, z2])

    result = host.delete_copper_pour({"net": "GND"})

    assert result["success"] is False
    assert host.board.Remove.call_count == 0
    # Candidate list surfaced for disambiguation
    assert {z["uuid"] for z in result["zones"]} == {"uuid-1", "uuid-2"}


def test_delete_all_matches_with_flag() -> None:
    z1 = _stub_zone("uuid-1", "GND", 0)
    z2 = _stub_zone("uuid-2", "GND", 31)
    host = _Host([z1, z2])

    result = host.delete_copper_pour({"net": "GND", "all": True})

    assert result["success"] is True
    assert host.board.Remove.call_count == 2
    assert len(result["deleted"]) == 2


def test_delete_unknown_uuid_lists_zones() -> None:
    host = _Host([_stub_zone("uuid-1", "GND", 0)])

    result = host.delete_copper_pour({"uuid": "nope"})

    assert result["success"] is False
    assert result["zones"][0]["uuid"] == "uuid-1"


# ---------------------------------------------------------------------------
# edit_copper_pour
# ---------------------------------------------------------------------------


def test_edit_pad_connection_and_clearance(monkeypatch: Any) -> None:
    z1 = _stub_zone("uuid-1", "GND", 0)
    host = _Host([z1])

    import commands.routing._zones as zones_mod

    monkeypatch.setattr(zones_mod.pcbnew, "ZONE_CONNECTION_FULL", 2, raising=False)

    result = host.edit_copper_pour({"uuid": "uuid-1", "padConnection": "solid", "clearance": 0.4})

    assert result["success"] is True
    assert set(result["changed"]) == {"padConnection", "clearance"}
    z1.SetPadConnection.assert_called_once_with(2)
    z1.SetLocalClearance.assert_called_once_with(400000)
    # Fill must be marked stale after an edit
    assert z1.UnFill.called or z1.SetIsFilled.called


def test_edit_outline_replaces_contour() -> None:
    z1 = _stub_zone("uuid-1", "GND", 0)
    host = _Host([z1])

    pts = [{"x": 21, "y": 21}, {"x": 69, "y": 21}, {"x": 69, "y": 49}, {"x": 21, "y": 49}]
    result = host.edit_copper_pour({"uuid": "uuid-1", "outline": pts})

    assert result["success"] is True
    outline = z1.Outline.return_value
    outline.RemoveAllContours.assert_called_once()
    outline.NewOutline.assert_called_once()
    assert outline.Append.call_count == 4


def test_edit_refuses_when_no_property_given() -> None:
    z1 = _stub_zone("uuid-1", "GND", 0)
    host = _Host([z1])

    result = host.edit_copper_pour({"uuid": "uuid-1"})

    assert result["success"] is False
    assert "No editable property" in result["message"]


def test_edit_refuses_ambiguous_selector() -> None:
    z1 = _stub_zone("uuid-1", "GND", 0)
    z2 = _stub_zone("uuid-2", "GND", 31)
    host = _Host([z1, z2])

    result = host.edit_copper_pour({"net": "GND", "clearance": 0.4})

    assert result["success"] is False
    assert z1.SetLocalClearance.call_count == 0
    assert z2.SetLocalClearance.call_count == 0


def test_edit_rejects_unknown_pad_connection() -> None:
    z1 = _stub_zone("uuid-1", "GND", 0)
    host = _Host([z1])

    result = host.edit_copper_pour({"uuid": "uuid-1", "padConnection": "bogus"})

    assert result["success"] is False
    assert "padConnection" in result["message"]
