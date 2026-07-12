"""Bug 1 regression: copper_pour(edit|delete) must honour the ``zoneUuid`` selector.

The TS schema documents ``zoneUuid`` as the preferred edit/delete selector,
but every Python site read only ``params.get("uuid")`` — the uuid was silently
dropped, the selection fell back to net+layer, and two zones sharing net+layer
produced a multi-match refusal in exactly the situation the uuid exists for
(GD32F103VET6 E2E run).  These tests pin, for BOTH the SWIG methods and the
IPC fast paths:

  * ``zoneUuid`` selects exactly the right zone when two zones share net+layer;
  * ``uuid`` keeps working as an alias;
  * ``zoneUuid`` wins when both are given.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.routing._zones import ZoneMixin  # noqa: E402

# ---------------------------------------------------------------------------
# SWIG stubs (mirrors tests/test_zone_edit_delete.py)
# ---------------------------------------------------------------------------


def _stub_zone(uuid: str, net: str, layer_id: int) -> MagicMock:
    zone = MagicMock(name=f"zone_{uuid}")
    zone.m_Uuid.AsString.return_value = uuid
    zone.GetNetname.return_value = net
    zone.GetLayer.return_value = layer_id
    zone.IsFilled.return_value = True
    return zone


class _Host(ZoneMixin):
    def __init__(self, zones: List[MagicMock]):
        self.board = MagicMock(name="board")
        self.board.Zones.return_value = zones
        self.board.GetLayerID.side_effect = lambda name: {"F.Cu": 0, "B.Cu": 31}.get(name, -1)
        self.board.GetLayerName.side_effect = lambda lid: {0: "F.Cu", 31: "B.Cu"}.get(lid, "?")


def _twin_zone_host() -> tuple:
    """Two zones sharing net AND layer — only a uuid can disambiguate."""
    z1 = _stub_zone("uuid-1", "/GND", 31)
    z2 = _stub_zone("uuid-2", "/GND", 31)
    return _Host([z1, z2]), z1, z2


# ---------------------------------------------------------------------------
# SWIG delete_copper_pour
# ---------------------------------------------------------------------------


def test_swig_delete_zone_uuid_selects_exact_zone() -> None:
    host, z1, z2 = _twin_zone_host()

    result = host.delete_copper_pour({"zoneUuid": "uuid-2"})

    assert result["success"] is True, result
    host.board.Remove.assert_called_once_with(z2)
    assert result["deleted"] == [
        {"uuid": "uuid-2", "net": "/GND", "layer": "B.Cu", "isFilled": True}
    ]


def test_swig_delete_uuid_alias_still_works() -> None:
    host, z1, _ = _twin_zone_host()

    result = host.delete_copper_pour({"uuid": "uuid-1"})

    assert result["success"] is True
    host.board.Remove.assert_called_once_with(z1)


def test_swig_delete_zone_uuid_wins_over_alias() -> None:
    host, _, z2 = _twin_zone_host()

    result = host.delete_copper_pour({"zoneUuid": "uuid-2", "uuid": "uuid-1"})

    assert result["success"] is True
    host.board.Remove.assert_called_once_with(z2)


def test_swig_delete_without_uuid_still_refuses_twin_match() -> None:
    """The multi-match refusal (the failure mode uuid exists to avoid) stays."""
    host, _, _ = _twin_zone_host()

    result = host.delete_copper_pour({"net": "GND", "layer": "B.Cu"})

    assert result["success"] is False
    assert host.board.Remove.call_count == 0
    assert "zoneUuid" in result["message"]


# ---------------------------------------------------------------------------
# SWIG edit_copper_pour
# ---------------------------------------------------------------------------


def test_swig_edit_zone_uuid_selects_exact_zone() -> None:
    host, z1, z2 = _twin_zone_host()

    result = host.edit_copper_pour({"zoneUuid": "uuid-2", "clearance": 0.4})

    assert result["success"] is True, result
    z2.SetLocalClearance.assert_called_once_with(400000)
    z1.SetLocalClearance.assert_not_called()
    assert result["zone"]["uuid"] == "uuid-2"


def test_swig_edit_uuid_alias_still_works() -> None:
    host, z1, z2 = _twin_zone_host()

    result = host.edit_copper_pour({"uuid": "uuid-1", "clearance": 0.4})

    assert result["success"] is True
    z1.SetLocalClearance.assert_called_once_with(400000)
    z2.SetLocalClearance.assert_not_called()


def test_swig_edit_without_uuid_still_refuses_twin_match() -> None:
    host, z1, z2 = _twin_zone_host()

    result = host.edit_copper_pour({"net": "GND", "layer": "B.Cu", "clearance": 0.4})

    assert result["success"] is False
    z1.SetLocalClearance.assert_not_called()
    z2.SetLocalClearance.assert_not_called()
    assert "zoneUuid" in result["message"]


# ---------------------------------------------------------------------------
# IPC fast paths (mock pattern from tests/test_zone_routing_coherence_n2.py)
# ---------------------------------------------------------------------------


def _fake_kipy_zone(uuid: str, net: str = "/GND") -> MagicMock:
    zone = MagicMock(name=f"kipy_zone_{uuid}")
    zone.id = uuid
    net_obj = MagicMock()
    net_obj.name = net
    zone.net = net_obj
    zone.layers = []
    zone.filled = False
    zone.priority = 0
    return zone


def _ipc_iface(zones: List[MagicMock]):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    iface.ipc_board_api = MagicMock()
    board = MagicMock(name="kipy_board")
    board.get_zones.return_value = zones
    iface.ipc_board_api._get_board.return_value = board
    iface.ipc_board_api.remove_zones.return_value = True
    iface.ipc_board_api.update_zone.return_value = True
    return iface


@pytest.fixture(autouse=True)
def _bare_uuid(monkeypatch):
    monkeypatch.setattr(
        "handlers.ipc_fastpath._zones._zone_uuid_str", lambda z: str(getattr(z, "id", ""))
    )


def test_ipc_delete_zone_uuid_selects_exact_zone() -> None:
    from handlers.ipc_fastpath._zones import handle_delete_copper_pour

    z1, z2 = _fake_kipy_zone("z-1"), _fake_kipy_zone("z-2")
    iface = _ipc_iface([z1, z2])

    result = handle_delete_copper_pour(iface, {"zoneUuid": "z-2"})

    assert result["success"] is True, result
    iface.ipc_board_api.remove_zones.assert_called_once_with([z2])
    assert result["deleted"][0]["uuid"] == "z-2"


def test_ipc_delete_uuid_alias_still_works() -> None:
    from handlers.ipc_fastpath._zones import handle_delete_copper_pour

    z1, z2 = _fake_kipy_zone("z-1"), _fake_kipy_zone("z-2")
    iface = _ipc_iface([z1, z2])

    result = handle_delete_copper_pour(iface, {"uuid": "z-1"})

    assert result["success"] is True
    iface.ipc_board_api.remove_zones.assert_called_once_with([z1])


def test_ipc_delete_without_uuid_still_refuses_twin_match() -> None:
    from handlers.ipc_fastpath._zones import handle_delete_copper_pour

    iface = _ipc_iface([_fake_kipy_zone("z-1"), _fake_kipy_zone("z-2")])

    result = handle_delete_copper_pour(iface, {"net": "GND"})

    assert result["success"] is False
    assert "zoneUuid" in result["message"]
    iface.ipc_board_api.remove_zones.assert_not_called()


def test_ipc_edit_zone_uuid_selects_exact_zone(monkeypatch) -> None:
    """priority-only edit avoids the kipy geometry/proto imports' attributes,
    so MagicMock kipy modules suffice (pattern from test_ipc_zone_query_format)."""
    for mod in (
        "kipy",
        "kipy.geometry",
        "kipy.proto",
        "kipy.proto.board",
        "kipy.proto.board.board_types_pb2",
        "kipy.util",
        "kipy.util.units",
    ):
        monkeypatch.setitem(sys.modules, mod, MagicMock())
    from handlers.ipc_fastpath._zones import handle_edit_copper_pour

    z1, z2 = _fake_kipy_zone("z-1"), _fake_kipy_zone("z-2")
    iface = _ipc_iface([z1, z2])

    result = handle_edit_copper_pour(iface, {"zoneUuid": "z-2", "priority": 3})

    assert result["success"] is True, result
    assert z2.priority == 3
    assert z1.priority == 0
    iface.ipc_board_api.update_zone.assert_called_once_with(z2)


def test_ipc_edit_uuid_alias_still_works(monkeypatch) -> None:
    for mod in (
        "kipy",
        "kipy.geometry",
        "kipy.proto",
        "kipy.proto.board",
        "kipy.proto.board.board_types_pb2",
        "kipy.util",
        "kipy.util.units",
    ):
        monkeypatch.setitem(sys.modules, mod, MagicMock())
    from handlers.ipc_fastpath._zones import handle_edit_copper_pour

    z1, z2 = _fake_kipy_zone("z-1"), _fake_kipy_zone("z-2")
    iface = _ipc_iface([z1, z2])

    result = handle_edit_copper_pour(iface, {"uuid": "z-1", "priority": 7})

    assert result["success"] is True
    assert z1.priority == 7
    iface.ipc_board_api.update_zone.assert_called_once_with(z1)
