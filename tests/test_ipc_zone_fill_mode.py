"""Regression test for IPC copper-pour (add_zone) on kipy 10.

kipy 10 made ``Zone.fill_mode`` a getter-only property, so the old
``zone.fill_mode = ZoneFillMode.ZFM_SOLID`` raised
"property 'fill_mode' of 'Zone' object has no setter" — caught and
turned into a generic ``add_zone -> False``, so every copper pour
silently failed.  The fix assigns the underlying proto enum
(``zone._proto.copper_settings.fill_mode``) instead.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

# ``real_kipy`` fixture is provided by tests/conftest.py.


def _api_with_fakes(captured):
    """An IPCBoardAPI whose board/commit/notify hooks are stubbed so we can
    drive add_zone without a live KiCad."""
    from kicad_api.ipc_backend import IPCBoardAPI

    api = IPCBoardAPI.__new__(IPCBoardAPI)  # bypass __init__ (needs a kipy board)
    fake_board = MagicMock()
    fake_board.get_nets.return_value = []
    api._get_board = lambda: fake_board
    api._apply_create = lambda board, item, msg: captured.__setitem__("zone", item)
    api._notify = lambda *a, **k: None
    return api


def test_add_zone_solid_fill_mode_no_setter_error(real_kipy):
    from kipy.proto.board.board_types_pb2 import ZoneFillMode

    captured = {}
    api = _api_with_fakes(captured)
    pts = [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}]

    # Before the fix this returned False (the fill_mode setter raised).
    assert api.add_zone(points=pts, layer="B.Cu", net_name="GND", fill_mode="solid") is True
    assert captured["zone"]._proto.copper_settings.fill_mode == ZoneFillMode.ZFM_SOLID


def test_add_zone_hatched_fill_mode(real_kipy):
    from kipy.proto.board.board_types_pb2 import ZoneFillMode

    captured = {}
    api = _api_with_fakes(captured)
    pts = [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}]

    assert api.add_zone(points=pts, layer="F.Cu", fill_mode="hatched") is True
    assert captured["zone"]._proto.copper_settings.fill_mode == ZoneFillMode.ZFM_HATCHED


def test_add_zone_rejects_too_few_points(real_kipy):
    api = _api_with_fakes({})
    assert api.add_zone(points=[{"x": 0, "y": 0}, {"x": 1, "y": 1}], layer="F.Cu") is False
