"""Tests for the generic drawing-primitive handlers (shapes.py).

We don't exercise kipy itself — these tests verify that:
  - the handler accepts both nested-dict and flat top-level coordinate forms
  - the IPC board API gets called with the right kwargs
  - missing-IPC and bad-input cases fail cleanly without calling the API
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


@pytest.fixture(autouse=True)
def _pcb_editor_open(monkeypatch):
    """Pretend the PCB editor frame is open so the IPC board-op gate passes."""
    from kicad_interface import KiCADInterface
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr(KiCADProcessManager, "is_pcb_editor_running", lambda: True)
    monkeypatch.setattr(
        KiCADInterface, "_ipc_has_open_board_document", lambda self: True
    )


def _make_iface(api, use_ipc=True):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = use_ipc
    iface.ipc_backend = MagicMock() if use_ipc else None
    iface.ipc_board_api = api
    iface.board = None
    iface.command_routes = {}
    iface._board_disk_signature = None
    iface._current_project_path = None
    iface._last_auto_save_status = None
    return iface


class _RecordingAPI:
    """Records every shape call so tests can inspect the forwarded kwargs."""

    def __init__(self):
        self.last = None

    def _record(self, cmd, kwargs):
        self.last = (cmd, kwargs)
        return {"success": True, "id": "fake-id", "layer": kwargs.get("layer", "?")}

    def add_segment(self, **kwargs):
        return self._record("add_segment", kwargs)

    def add_arc(self, **kwargs):
        return self._record("add_arc", kwargs)

    def add_circle(self, **kwargs):
        return self._record("add_circle", kwargs)

    def add_rectangle(self, **kwargs):
        return self._record("add_rectangle", kwargs)

    def add_polygon(self, **kwargs):
        return self._record("add_polygon", kwargs)


def test_add_segment_nested_form():
    api = _RecordingAPI()
    iface = _make_iface(api)
    out = iface._handle_add_segment(
        {"start": {"x": 1, "y": 2}, "end": {"x": 3, "y": 4}, "width": 0.2, "layer": "F.Fab"}
    )
    assert out["success"] is True
    cmd, kwargs = api.last
    assert cmd == "add_segment"
    assert kwargs == {
        "start_x": 1.0,
        "start_y": 2.0,
        "end_x": 3.0,
        "end_y": 4.0,
        "width": 0.2,
        "layer": "F.Fab",
    }


def test_add_segment_flat_form_and_defaults():
    api = _RecordingAPI()
    iface = _make_iface(api)
    iface._handle_add_segment({"startX": 0, "startY": 0, "endX": 10, "endY": 0})
    _, kwargs = api.last
    # Defaults: width 0.15, layer F.SilkS
    assert kwargs["width"] == 0.15
    assert kwargs["layer"] == "F.SilkS"
    assert kwargs["start_x"] == 0.0 and kwargs["end_x"] == 10.0


def test_add_arc_forwards_three_points():
    api = _RecordingAPI()
    iface = _make_iface(api)
    iface._handle_add_arc(
        {
            "start": {"x": 0, "y": 0},
            "mid": {"x": 5, "y": 5},
            "end": {"x": 10, "y": 0},
            "layer": "Edge.Cuts",
        }
    )
    _, kwargs = api.last
    assert kwargs["start_x"] == 0 and kwargs["mid_x"] == 5 and kwargs["end_x"] == 10
    assert kwargs["layer"] == "Edge.Cuts"


def test_add_circle_filled_default_false():
    api = _RecordingAPI()
    iface = _make_iface(api)
    iface._handle_add_circle({"center": {"x": 5, "y": 5}, "radius": 2})
    _, kwargs = api.last
    assert kwargs["center_x"] == 5 and kwargs["center_y"] == 5
    assert kwargs["radius"] == 2
    assert kwargs["filled"] is False
    assert kwargs["layer"] == "F.SilkS"


def test_add_rectangle_filled_true():
    api = _RecordingAPI()
    iface = _make_iface(api)
    iface._handle_add_rectangle(
        {
            "topLeft": {"x": 0, "y": 0},
            "bottomRight": {"x": 10, "y": 5},
            "filled": True,
            "layer": "F.Cu",
        }
    )
    _, kwargs = api.last
    assert kwargs["top_left_x"] == 0 and kwargs["bottom_right_x"] == 10
    assert kwargs["filled"] is True
    assert kwargs["layer"] == "F.Cu"


def test_add_polygon_passes_points_list():
    api = _RecordingAPI()
    iface = _make_iface(api)
    pts = [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 5, "y": 10}]
    iface._handle_add_polygon({"points": pts, "filled": True})
    _, kwargs = api.last
    assert kwargs["points"] == pts
    assert kwargs["filled"] is True


def test_add_polygon_rejects_non_list():
    api = _RecordingAPI()
    iface = _make_iface(api)
    out = iface._handle_add_polygon({"points": "not a list"})
    assert out["success"] is False
    assert "list" in out["message"].lower()
    assert api.last is None  # API was not called


def test_handlers_fail_without_ipc():
    iface = _make_iface(api=None, use_ipc=False)
    iface.ensure_ipc = lambda **kw: (False, "ipc disabled in test")
    for cmd in ("add_segment", "add_arc", "add_circle", "add_rectangle", "add_polygon"):
        out = getattr(iface, f"_handle_{cmd}")({})
        assert out["success"] is False
        assert "IPC" in out["message"]
