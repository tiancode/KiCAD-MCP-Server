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


@pytest.fixture(autouse=True)
def _board_document_open(monkeypatch):
    """IPC gate now asks kipy.get_open_documents() — stub it as 'yes' so
    these handler-level tests don't trip the editor-frame gate."""
    from kicad_interface import KiCADInterface

    monkeypatch.setattr(KiCADInterface, "_ipc_has_open_board_document", lambda self: True)


@pytest.fixture
def iface():
    """Bare KiCADInterface with a mock IPC board API for the handler to call.

    Wire ipc_backend.get_board() to return the same ipc_board_api mock so
    that ``_refresh_ipc_board_api`` (which dispatch calls before the IPC
    fast-path) doesn't replace it with a fresh auto-magic mock and lose our
    pre-configured ``add_zone`` return value.
    """
    from kicad_interface import KiCADInterface

    obj = KiCADInterface.__new__(KiCADInterface)
    obj.use_ipc = True
    board_api = MagicMock()
    board_api.add_zone = MagicMock(return_value=True)
    obj.ipc_board_api = board_api
    obj.ipc_backend = MagicMock()
    obj.ipc_backend.get_board = MagicMock(return_value=board_api)
    obj.ipc_backend.is_connected = MagicMock(return_value=True)
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
        ipc_fastpath._zones,
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

    monkeypatch.setattr(ipc_fastpath._zones, "_ipc_board_edge_rect", lambda api: None)

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
        ipc_fastpath._zones,
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


# ---------------------------------------------------------------------------
# Unit conversion (add_zone schema makes `unit` required and accepts mil/inch)
# ---------------------------------------------------------------------------
def test_top_level_unit_inch_is_converted_to_mm(iface):
    """add_zone's schema requires a `unit` parameter; mil/inch must be
    converted to mm before reaching kipy (which expects mm)."""
    from handlers.ipc_fastpath import handle_add_copper_pour

    handle_add_copper_pour(
        iface,
        {
            "layer": "B.Cu",
            "net": "GND",
            "unit": "inch",
            "points": [
                {"x": 0, "y": 0},
                {"x": 1, "y": 0},
                {"x": 1, "y": 1},
                {"x": 0, "y": 1},
            ],
        },
    )

    call = iface.ipc_board_api.add_zone.call_args
    pts = call.kwargs["points"]
    # 1 inch = 25.4 mm
    assert pts[1]["x"] == pytest.approx(25.4)
    assert pts[2]["y"] == pytest.approx(25.4)


def test_top_level_unit_mil_is_converted_to_mm(iface):
    from handlers.ipc_fastpath import handle_add_copper_pour

    handle_add_copper_pour(
        iface,
        {
            "layer": "B.Cu",
            "net": "GND",
            "unit": "mil",
            "points": [
                {"x": 0, "y": 0},
                {"x": 1000, "y": 0},
                {"x": 1000, "y": 1000},
                {"x": 0, "y": 1000},
            ],
        },
    )

    call = iface.ipc_board_api.add_zone.call_args
    pts = call.kwargs["points"]
    # 1000 mil = 25.4 mm
    assert pts[1]["x"] == pytest.approx(25.4)


# ---------------------------------------------------------------------------
# add_zone alias (removed 2026-07)
# ---------------------------------------------------------------------------


def test_add_zone_alias_stays_removed():
    """Command-redundancy cleanup (2026-07): the ``add_zone`` alias for
    ``add_copper_pour`` was unreachable from the MCP surface (the TS layer
    stopped registering it in the 2026-06 tool cleanup) and has been removed
    from every python registry. It must not silently come back."""
    from kicad_interface import KiCADInterface

    assert "add_zone" not in KiCADInterface.IPC_CAPABLE_COMMANDS
    assert "add_zone" not in KiCADInterface._BOARD_MUTATING_COMMANDS
    # Same cleanup removed the add_text alias (canonical name: add_board_text).
    assert "add_text" not in KiCADInterface.IPC_CAPABLE_COMMANDS
    assert "add_text" not in KiCADInterface._BOARD_MUTATING_COMMANDS
    assert "add_board_text" in KiCADInterface.IPC_CAPABLE_COMMANDS
