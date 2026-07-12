"""N2 regression: zone add/edit/delete/list route coherently per session.

With KiCad up, ``add_copper_pour`` / ``query_zones`` routed through IPC while
``delete_copper_pour`` / ``edit_copper_pour`` existed only as SWIG methods —
an IPC-added zone (KiCad memory) was invisible to the very next delete, which
read the SWIG board and refused with "No zone matched the given net/layer
filters".  All four zone commands are now IPC-capable, so a session uses ONE
backend for the whole zone lifecycle: IPC when active, SWIG when KiCad is
closed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from kicad_interface import KiCADInterface  # noqa: E402


def _make_iface(*, use_ipc):
    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = use_ipc
    iface.ipc_backend = MagicMock() if use_ipc else None
    iface.ipc_board_api = MagicMock() if use_ipc else None
    iface.board = None
    iface.command_routes = {}
    iface._board_disk_signature = None
    iface._swig_board_backed_commands = set()
    iface._last_auto_save_status = None
    iface._ipc_writes_pending = False
    iface._swig_writes_landed = False
    iface._ipc_change_callback_registered = False
    return iface


@pytest.fixture(autouse=True)
def _editor_open(monkeypatch):
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr(KiCADProcessManager, "is_pcb_editor_running", lambda: True)
    monkeypatch.setattr(KiCADInterface, "_ipc_has_open_board_document", lambda self: True)
    monkeypatch.setattr(KiCADInterface, "_try_enable_ipc_backend", lambda self, force=False: False)


# ---------------------------------------------------------------------------
# Registration coherence: all four zone ops are IPC-capable together.
# ---------------------------------------------------------------------------
def test_all_zone_commands_are_ipc_capable():
    for cmd in ("add_copper_pour", "query_zones", "delete_copper_pour", "edit_copper_pour"):
        assert cmd in KiCADInterface.IPC_CAPABLE_COMMANDS, f"{cmd} must route IPC when active"


def test_zone_fastpath_handlers_resolve():
    import handlers.ipc_fastpath as fp

    for name in (
        "handle_add_copper_pour",
        "handle_query_zones",
        "handle_delete_copper_pour",
        "handle_edit_copper_pour",
    ):
        assert callable(getattr(fp, name, None)), f"{name} missing from ipc_fastpath"


def test_zone_mutations_still_swig_gated_in_board_mutating_set():
    for cmd in ("add_copper_pour", "delete_copper_pour", "edit_copper_pour"):
        assert cmd in KiCADInterface._BOARD_MUTATING_COMMANDS


# ---------------------------------------------------------------------------
# IPC-active session: add → delete sees the SAME zone set (KiCad memory).
# ---------------------------------------------------------------------------
def _fake_kipy_zone(uuid="zone-1", net="GND", filled=False):
    zone = MagicMock(name=f"kipy_zone_{uuid}")
    zone.id = uuid
    net_obj = MagicMock()
    net_obj.name = net
    zone.net = net_obj
    zone.layers = []  # layer normalization best-effort → []
    zone.filled = filled
    zone.priority = 0
    return zone


def _wire_ipc_zone_board(iface, zones):
    """ipc_board_api._get_board().get_zones() returns ``zones`` (live list)."""
    board = MagicMock(name="kipy_board")
    board.get_zones.return_value = zones
    iface.ipc_board_api._get_board.return_value = board

    def remove_zones(matches):
        for z in list(matches):
            zones.remove(z)
        return True

    iface.ipc_board_api.remove_zones.side_effect = remove_zones
    iface.ipc_board_api.update_zone.return_value = True
    return board


def test_ipc_session_add_then_delete_sees_same_zone_set(monkeypatch):
    """The N2 repro, healed: delete right after an IPC add finds the zone."""
    monkeypatch.setattr(
        "handlers.ipc_fastpath._zones._zone_uuid_str", lambda z: str(getattr(z, "id", ""))
    )
    iface = _make_iface(use_ipc=True)
    live_zones = [_fake_kipy_zone(uuid="z-added", net="GND")]
    _wire_ipc_zone_board(iface, live_zones)

    result = iface.handle_command("delete_copper_pour", {"net": "GND"})

    assert result["success"] is True, result
    assert result["deleted"][0]["uuid"] == "z-added"
    assert live_zones == []  # actually removed from the live (KiCad) set
    # SWIG board was never touched (there is none).
    assert iface.board is None


def test_ipc_session_delete_no_match_lists_live_zones(monkeypatch):
    monkeypatch.setattr(
        "handlers.ipc_fastpath._zones._zone_uuid_str", lambda z: str(getattr(z, "id", ""))
    )
    iface = _make_iface(use_ipc=True)
    _wire_ipc_zone_board(iface, [_fake_kipy_zone(uuid="z1", net="+3V3")])

    result = iface.handle_command("delete_copper_pour", {"net": "NO_SUCH_NET"})

    assert result["success"] is False
    assert "No zone matched" in result["message"]
    # The candidates come from the LIVE (IPC) zone set, proving routing.
    assert result["zones"][0]["uuid"] == "z1"


def test_ipc_session_delete_multi_match_refuses_without_all(monkeypatch):
    monkeypatch.setattr(
        "handlers.ipc_fastpath._zones._zone_uuid_str", lambda z: str(getattr(z, "id", ""))
    )
    iface = _make_iface(use_ipc=True)
    zones = [_fake_kipy_zone(uuid="z1", net="GND"), _fake_kipy_zone(uuid="z2", net="GND")]
    _wire_ipc_zone_board(iface, zones)

    result = iface.handle_command("delete_copper_pour", {"net": "GND"})

    assert result["success"] is False
    assert "all=true" in result["message"]
    assert len(zones) == 2  # nothing deleted

    result = iface.handle_command("delete_copper_pour", {"net": "GND", "all": True})
    assert result["success"] is True
    assert zones == []


def test_ipc_session_delete_net_filter_resolves_names(monkeypatch):
    """F3 parity on the IPC path: filter net 'GND' matches a '/GND' zone."""
    monkeypatch.setattr(
        "handlers.ipc_fastpath._zones._zone_uuid_str", lambda z: str(getattr(z, "id", ""))
    )
    iface = _make_iface(use_ipc=True)
    zones = [_fake_kipy_zone(uuid="z1", net="/GND")]
    _wire_ipc_zone_board(iface, zones)

    result = iface.handle_command("delete_copper_pour", {"net": "GND"})

    assert result["success"] is True
    assert result["deleted"][0]["net"] == "/GND"


def test_ipc_session_edit_updates_live_zone(monkeypatch, real_kipy):
    # real_kipy: the edit fast path imports kipy.geometry / kipy.proto inside
    # the handler; earlier tests leave a non-package kipy stub in sys.modules.
    monkeypatch.setattr(
        "handlers.ipc_fastpath._zones._zone_uuid_str", lambda z: str(getattr(z, "id", ""))
    )
    iface = _make_iface(use_ipc=True)
    zone = _fake_kipy_zone(uuid="z1", net="GND")
    _wire_ipc_zone_board(iface, [zone])

    result = iface.handle_command("edit_copper_pour", {"uuid": "z1", "priority": 3})

    assert result["success"] is True, result
    assert "priority" in result["changed"]
    assert zone.priority == 3
    iface.ipc_board_api.update_zone.assert_called_once_with(zone)


def test_ipc_session_edit_requires_editable_property(monkeypatch, real_kipy):
    monkeypatch.setattr(
        "handlers.ipc_fastpath._zones._zone_uuid_str", lambda z: str(getattr(z, "id", ""))
    )
    iface = _make_iface(use_ipc=True)
    _wire_ipc_zone_board(iface, [_fake_kipy_zone(uuid="z1")])

    result = iface.handle_command("edit_copper_pour", {"uuid": "z1"})

    assert result["success"] is False
    assert "No editable property" in result["message"]
    iface.ipc_board_api.update_zone.assert_not_called()


# ---------------------------------------------------------------------------
# SWIG-only session: all four still fall back to the SWIG handlers.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command, route_attr",
    [
        ("delete_copper_pour", "delete_copper_pour"),
        ("edit_copper_pour", "edit_copper_pour"),
        ("query_zones", "query_zones"),
    ],
)
def test_swig_session_zone_commands_fall_back(command, route_attr):
    iface = _make_iface(use_ipc=False)
    called = {}

    def swig_handler(params):
        called["params"] = params
        return {"success": True, "via": "swig"}

    iface.command_routes = {command: swig_handler}

    result = iface.handle_command(command, {"net": "GND"})

    assert result["success"] is True
    assert result["via"] == "swig"
    assert called["params"] == {"net": "GND"}


def test_swig_session_add_falls_back_to_wrapper():
    iface = _make_iface(use_ipc=False)
    called = {}

    def wrapper(params):
        called["n"] = called.get("n", 0) + 1
        return {"success": True, "via": "swig-wrapper"}

    iface.command_routes = {"add_copper_pour": wrapper}

    result = iface.handle_command("add_copper_pour", {"net": "GND", "layer": "B.Cu"})

    assert result["success"] is True
    assert called["n"] == 1


# ---------------------------------------------------------------------------
# IPC add: autoRefill parity with the SWIG wrapper.
# ---------------------------------------------------------------------------
def _wire_add_zone(iface):
    iface.ipc_board_api.add_zone.return_value = True
    iface.ipc_board_api.refill_zones.return_value = True
    net = MagicMock()
    net.name = "GND"
    iface.ipc_board_api.get_nets.return_value = [{"name": "GND"}]


def test_ipc_add_auto_refills_by_default():
    from handlers.ipc_fastpath._zones import handle_add_copper_pour

    iface = _make_iface(use_ipc=True)
    _wire_add_zone(iface)
    params = {
        "net": "GND",
        "layer": "B.Cu",
        "outline": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}],
    }

    result = handle_add_copper_pour(iface, params)

    assert result["success"] is True
    assert result["refillStatus"] == "filled"
    iface.ipc_board_api.refill_zones.assert_called_once()


def test_ipc_add_defers_refill_when_disabled():
    from handlers.ipc_fastpath._zones import handle_add_copper_pour

    iface = _make_iface(use_ipc=True)
    _wire_add_zone(iface)
    params = {
        "net": "GND",
        "layer": "B.Cu",
        "autoRefill": False,
        "outline": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}],
    }

    result = handle_add_copper_pour(iface, params)

    assert result["success"] is True
    assert "deferred" in result["refillStatus"]
    iface.ipc_board_api.refill_zones.assert_not_called()
