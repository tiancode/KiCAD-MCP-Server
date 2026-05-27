"""Regression tests for the IPC ``add_copper_pour`` handler.

Bug history: the IPC fast-path read the outline from ``params["points"]``
while the TS schema (and the SWIG path) named it ``outline``.  Every call
through the IPC backend therefore saw zero points and refused with
"At least 3 points are required for copper pour outline".  These tests
lock in:

  - ``outline`` is the canonical key.
  - ``points`` still works as a legacy alias.
  - Omitting both falls back to the board's Edge.Cuts bounding box, matching
    the SWIG path and the public docstring.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


@pytest.fixture
def iface():
    """Bare KiCADInterface with a mock IPC board API for the handler to call."""
    from kicad_interface import KiCADInterface

    obj = KiCADInterface.__new__(KiCADInterface)
    obj.use_ipc = True
    obj.ipc_board_api = MagicMock()
    obj.ipc_board_api.add_zone = MagicMock(return_value=True)
    obj.ipc_backend = MagicMock()
    obj.board = None
    obj.command_routes = {}
    obj._ipc_writes_pending = False
    obj._swig_writes_landed = False
    obj._ipc_change_callback_registered = False
    return obj


def _square(side: float) -> list:
    return [
        {"x": 0, "y": 0},
        {"x": side, "y": 0},
        {"x": side, "y": side},
        {"x": 0, "y": side},
    ]


def test_outline_kwarg_is_accepted(iface):
    """The TS schema names the parameter ``outline`` — the IPC handler must
    read it (regression: it used to only read ``points`` and reject every
    real-world call)."""
    from handlers.ipc_fastpath import handle_add_copper_pour

    out = handle_add_copper_pour(
        iface,
        {"layer": "B.Cu", "net": "GND", "outline": _square(50)},
    )

    assert out["success"] is True
    iface.ipc_board_api.add_zone.assert_called_once()
    call = iface.ipc_board_api.add_zone.call_args
    assert call.kwargs["points"] == _square(50)
    assert call.kwargs["layer"] == "B.Cu"
    assert call.kwargs["net_name"] == "GND"
    assert out["pour"]["pointCount"] == 4


def test_points_kwarg_still_works_as_alias(iface):
    """Legacy callers that pass ``points`` instead of ``outline`` keep working."""
    from handlers.ipc_fastpath import handle_add_copper_pour

    out = handle_add_copper_pour(
        iface,
        {"layer": "F.Cu", "net": "VCC", "points": _square(40)},
    )

    assert out["success"] is True
    call = iface.ipc_board_api.add_zone.call_args
    assert call.kwargs["points"] == _square(40)


def test_outline_takes_precedence_over_points_when_both_given(iface):
    """If a caller passes both, the canonical name wins."""
    from handlers.ipc_fastpath import handle_add_copper_pour

    handle_add_copper_pour(
        iface,
        {
            "layer": "F.Cu",
            "net": "GND",
            "outline": _square(80),
            "points": _square(10),  # should be ignored
        },
    )

    call = iface.ipc_board_api.add_zone.call_args
    assert call.kwargs["points"] == _square(80)


def test_omitted_outline_falls_back_to_board_edge_rect(iface, monkeypatch):
    """Match the SWIG behaviour: when the caller omits the outline, derive
    a rectangle from Edge.Cuts shapes."""
    from handlers import ipc_fastpath

    monkeypatch.setattr(
        ipc_fastpath,
        "_ipc_board_edge_rect",
        lambda api: [
            {"x": 0.0, "y": 0.0},
            {"x": 100.0, "y": 0.0},
            {"x": 100.0, "y": 80.0},
            {"x": 0.0, "y": 80.0},
        ],
    )

    out = ipc_fastpath.handle_add_copper_pour(
        iface,
        {"layer": "B.Cu", "net": "GND"},
    )

    assert out["success"] is True
    call = iface.ipc_board_api.add_zone.call_args
    assert call.kwargs["points"][0] == {"x": 0.0, "y": 0.0}
    assert call.kwargs["points"][2] == {"x": 100.0, "y": 80.0}
    assert out["pour"]["pointCount"] == 4


def test_omitted_outline_with_no_edge_cuts_returns_actionable_error(iface, monkeypatch):
    """When neither the caller's outline nor an Edge.Cuts rect is
    available, refuse with a message that names *both* recovery paths
    (pass an outline, or add a board outline first)."""
    from handlers import ipc_fastpath

    monkeypatch.setattr(ipc_fastpath, "_ipc_board_edge_rect", lambda api: None)

    out = ipc_fastpath.handle_add_copper_pour(iface, {"layer": "B.Cu", "net": "GND"})

    assert out["success"] is False
    msg = out["message"]
    assert "outline" in msg.lower()
    assert "edge.cuts" in msg.lower() or "board outline" in msg.lower()
    iface.ipc_board_api.add_zone.assert_not_called()


def test_short_outline_falls_back_to_board_when_possible(iface, monkeypatch):
    """A 2-point ``outline`` is also too short — same fall-back applies."""
    from handlers import ipc_fastpath

    monkeypatch.setattr(
        ipc_fastpath,
        "_ipc_board_edge_rect",
        lambda api: _square(60),
    )

    out = ipc_fastpath.handle_add_copper_pour(
        iface,
        {"layer": "B.Cu", "net": "GND", "outline": [{"x": 0, "y": 0}, {"x": 10, "y": 10}]},
    )

    assert out["success"] is True
    call = iface.ipc_board_api.add_zone.call_args
    assert len(call.kwargs["points"]) == 4
