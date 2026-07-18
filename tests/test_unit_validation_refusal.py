"""Unknown ``unit`` strings must be REFUSED with a structured VALIDATION error,
and a missing unit must default to millimetres.

Regression (breadth E2E): add_via / route_trace declared ``position.unit`` as a
free string, and ``unit_to_nm_scale`` treated anything that wasn't mm/mil as
inch — so ``{"unit": "banana"}`` silently scaled coordinates ×25.4 and landed
copper hundreds of mm off-board with ``success: true``.  The parse path now
raises InvalidUnitError, which each handler turns into a truthful VALIDATION
refusal via ``utils.responses.failed``; a missing unit means mm.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

import pcbnew  # noqa: E402  (conftest stub)
from commands.routing import RoutingCommands  # noqa: E402

_NM = 1_000_000


def _board() -> MagicMock:
    board = MagicMock()
    board.GetLayerID.side_effect = lambda name: {"F.Cu": 0, "B.Cu": 31}.get(name, -1)
    return board


# ---- add_via ---------------------------------------------------------------


@pytest.mark.unit
def test_add_via_unknown_unit_refused_validation():
    rc = RoutingCommands(_board())
    res = rc.add_via({"position": {"x": 10, "y": 10, "unit": "banana"}, "net": "SIG1"})
    assert res["success"] is False
    assert res["errorCode"] == "VALIDATION"
    assert res["errorCode"] != "INTERNAL_ERROR"
    assert "banana" in res["message"]


@pytest.mark.unit
def test_add_via_missing_unit_defaults_to_mm():
    pcbnew.VECTOR2I.reset_mock()
    rc = RoutingCommands(_board())
    res = rc.add_via({"position": {"x": 10, "y": 10}, "net": "SIG1"})
    assert res["success"] is True
    assert res["via"]["position"]["unit"] == "mm"
    # 10 mm -> 10_000_000 nm (mm scale), NOT 254_000_000 (the old inch bug).
    assert pcbnew.VECTOR2I.call_args.args == (10 * _NM, 10 * _NM)


# ---- route_trace -----------------------------------------------------------


@pytest.mark.unit
def test_route_trace_unknown_unit_refused_validation():
    rc = RoutingCommands(_board())
    res = rc.route_trace(
        {
            "start": {"x": 5, "y": 5, "unit": "furlong"},
            "end": {"x": 10, "y": 10},
            "layer": "F.Cu",
            "width": 0.25,
            "net": "SIG1",
        }
    )
    assert res["success"] is False
    assert res["errorCode"] == "VALIDATION"
    assert res["errorCode"] != "INTERNAL_ERROR"
    assert "furlong" in res["message"]


@pytest.mark.unit
def test_route_trace_missing_unit_defaults_to_mm():
    pcbnew.VECTOR2I.reset_mock()
    rc = RoutingCommands(_board())
    res = rc.route_trace(
        {
            "start": {"x": 5, "y": 5},
            "end": {"x": 10, "y": 10},
            "layer": "F.Cu",
            "width": 0.25,
            "net": "SIG1",
        }
    )
    # A missing unit must NOT refuse — it defaults to mm and routes.
    assert res["success"] is True
    assert res.get("errorCode") is None
    # The end point was converted with the mm scale (10 mm -> 10_000_000 nm),
    # not the old inch fall-through (254_000_000 nm).
    seen = {tuple(c.args) for c in pcbnew.VECTOR2I.call_args_list}
    assert (10 * _NM, 10 * _NM) in seen
