"""Regression: get_component_pads must work over IPC.

The SWIG handler reads ``iface.board`` and returns "No board is loaded" when
the user has the board open in KiCad but never ran open_project through the
MCP (a recurring user complaint — same class as get_pad_position/run_drc).
get_component_pads now has an IPC fast-path that reads pads live from KiCad,
so it succeeds whenever KiCad has a .kicad_pcb open even with no SWIG board.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


class _Vec:
    def __init__(self, x, y):
        self.x = x
        self.y = y


def _fake_pad(number, netname, x_nm, y_nm, shape, pad_type, size_nm, drill_nm):
    return SimpleNamespace(
        number=number,
        net=SimpleNamespace(name=netname),
        position=_Vec(x_nm, y_nm),
        pad_type=pad_type,
        padstack=SimpleNamespace(
            copper_layers=[SimpleNamespace(size=_Vec(size_nm, size_nm), shape=shape)],
            drill=SimpleNamespace(diameter=_Vec(drill_nm, drill_nm)),
        ),
    )


def _fake_board_with(reference, pads):
    fp = SimpleNamespace(
        reference_field=SimpleNamespace(text=SimpleNamespace(value=reference)),
        position=_Vec(10_000_000, 20_000_000),
        definition=SimpleNamespace(pads=pads),
    )
    board = MagicMock()
    board.get_footprints = MagicMock(return_value=[fp])
    return board


# ---------------------------------------------------------------------------
# IPCBoardAPI.get_component_pads — live extraction from kipy
# ---------------------------------------------------------------------------
def test_ipc_board_api_get_component_pads_extracts_geometry_and_net(real_kipy):
    from kicad_api.ipc_backend import IPCBoardAPI
    from kipy.proto.board.board_types_pb2 import PadStackShape, PadType
    from kipy.util.units import to_mm

    pad_smd = _fake_pad(
        "1", "VCC", 11_000_000, 20_000_000, PadStackShape.PSS_CIRCLE, PadType.PT_SMD, 800_000, 0
    )
    pad_pth = _fake_pad(
        "2",
        "GND",
        9_000_000,
        20_000_000,
        PadStackShape.PSS_RECTANGLE,
        PadType.PT_PTH,
        900_000,
        400_000,
    )
    api = IPCBoardAPI(None, lambda *_a: None)
    api._board = _fake_board_with("R1", [pad_smd, pad_pth])

    out = api.get_component_pads("R1")

    assert out["reference"] == "R1"
    assert out["padCount"] == 2
    assert out["componentPosition"] == {
        "x": to_mm(10_000_000),
        "y": to_mm(20_000_000),
        "unit": "mm",
    }

    p1, p2 = out["pads"]
    assert p1["number"] == "1" and p1["name"] == "1"
    assert p1["net"] == "VCC"
    assert p1["type"] == "smd"
    assert p1["shape"] == "circle"
    assert p1["position"] == {"x": to_mm(11_000_000), "y": to_mm(20_000_000), "unit": "mm"}
    assert p1["size"] == {"x": to_mm(800_000), "y": to_mm(800_000), "unit": "mm"}
    assert p1["drillSize"] is None  # SMD → no drill
    # Shape parity with the SWIG handler (which emits netCode); None over IPC.
    assert p1["netCode"] is None

    assert p2["type"] == "through_hole"
    assert p2["shape"] == "rect"
    assert p2["drillSize"] == to_mm(400_000)


def test_ipc_board_api_get_component_pads_returns_none_when_missing(real_kipy):
    from kicad_api.ipc_backend import IPCBoardAPI

    api = IPCBoardAPI(None, lambda *_a: None)
    api._board = _fake_board_with("R1", [])

    assert api.get_component_pads("R99") is None


def test_ipc_board_api_get_component_pads_survives_padstack_gaps(real_kipy):
    """Padstack geometry isn't always available over IPC; missing pieces must
    degrade to None/unknown rather than raise."""
    from kicad_api.ipc_backend import IPCBoardAPI
    from kipy.proto.board.board_types_pb2 import PadType

    pad = SimpleNamespace(
        number="1",
        net=SimpleNamespace(name="N1"),
        position=_Vec(0, 0),
        pad_type=PadType.PT_SMD,
        padstack=SimpleNamespace(copper_layers=[], drill=SimpleNamespace(diameter=_Vec(0, 0))),
    )
    api = IPCBoardAPI(None, lambda *_a: None)
    api._board = _fake_board_with("R1", [pad])

    out = api.get_component_pads("R1")
    assert out["pads"][0]["size"] is None
    assert out["pads"][0]["shape"] == "unknown"
    assert out["pads"][0]["drillSize"] is None


# ---------------------------------------------------------------------------
# Dispatch: get_component_pads routes through IPC with no SWIG board loaded
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _pcb_editor_open(monkeypatch):
    from kicad_interface import KiCADInterface
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr(KiCADProcessManager, "is_pcb_editor_running", lambda: True)
    monkeypatch.setattr(KiCADInterface, "_ipc_has_open_board_document", lambda self: True)


def _make_ipc_iface():
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    board_api = MagicMock()
    iface.ipc_board_api = board_api
    iface.ipc_backend = MagicMock()
    # handle_command -> _try_enable_ipc_backend -> _refresh_ipc_board_api
    # reassigns ipc_board_api = ipc_backend.get_board(); return our configured
    # mock so the real fast-path handler runs against it.
    iface.ipc_backend.get_board = MagicMock(return_value=board_api)
    iface.board = None  # no SWIG board — the failing condition
    iface.command_routes = {}
    iface._board_disk_signature = None
    iface._current_project_path = None
    iface._last_auto_save_status = None
    iface._ipc_writes_pending = False
    iface._swig_writes_landed = False
    iface._ipc_change_callback_registered = False
    return iface


def test_get_component_pads_routes_through_ipc_without_swig_board():
    iface = _make_ipc_iface()
    iface.ipc_board_api.get_component_pads = MagicMock(
        return_value={
            "reference": "R1",
            "componentPosition": {"x": 10.0, "y": 20.0, "unit": "mm"},
            "padCount": 1,
            "pads": [{"number": "1", "net": "VCC"}],
        }
    )

    out = iface.handle_command("get_component_pads", {"reference": "R1"})

    assert out["success"] is True
    assert out["reference"] == "R1"
    assert out["padCount"] == 1
    iface.ipc_board_api.get_component_pads.assert_called_once_with("R1")


def test_get_component_pads_ipc_read_flagged_stale_when_swig_wrote_disk():
    """As a read-only IPC command it passes the cross-backend gate but, when
    SWIG has landed disk writes, must carry the staleVsDisk hint."""
    iface = _make_ipc_iface()
    iface._swig_writes_landed = True
    iface.ipc_board_api.get_component_pads = MagicMock(
        return_value={"reference": "R1", "padCount": 0, "pads": []}
    )

    out = iface.handle_command("get_component_pads", {"reference": "R1"})

    assert out["success"] is True
    assert out["staleVsDisk"] is True
    assert "needs_reconcile" not in out
