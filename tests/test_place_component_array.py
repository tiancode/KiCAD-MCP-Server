"""place_component_array must work end-to-end from the MCP-advertised param
shape (rows / columns / rowSpacing / columnSpacing / startReference /
startPosition).

Regression (breadth E2E): the TS tool advertised those names while the Python
handler required count / spacingX / spacingY / referencePrefix and enforced
rows*columns==count, so NO documented call succeeded ("componentId and count
are required", miscoded INTERNAL_ERROR).  The handler now speaks the advertised
vocabulary (legacy names still accepted) and refuses missing params with a
truthful VALIDATION code.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.component import ComponentCommands  # noqa: E402


def _cmds_recording() -> "tuple[ComponentCommands, List[Dict[str, Any]]]":
    """ComponentCommands whose place_component is faked to just record calls,
    isolating the array mapping (positions + reference numbering)."""
    cc = ComponentCommands(board=MagicMock(), library_manager=MagicMock())
    calls: List[Dict[str, Any]] = []

    def fake_place(params: Dict[str, Any]) -> Dict[str, Any]:
        calls.append(params)
        return {
            "success": True,
            "component": {"reference": params["reference"], "position": params["position"]},
        }

    cc.place_component = fake_place  # type: ignore[assignment]
    return cc, calls


@pytest.mark.unit
def test_place_component_array_ts_advertised_shape_places_grid():
    cc, calls = _cmds_recording()
    res = cc.place_component_array(
        {
            "componentId": "Capacitor_SMD:C_0402_1005Metric",
            "startPosition": {"x": 5, "y": 25, "unit": "mm"},
            "rows": 2,
            "columns": 2,
            "rowSpacing": 2,
            "columnSpacing": 2,
            "startReference": "C1",
        }
    )
    assert res["success"] is True
    assert len(res["components"]) == 4
    assert len(calls) == 4
    # startReference "C1" -> prefix C, numbering from 1.
    assert [c["reference"] for c in calls] == ["C1", "C2", "C3", "C4"]
    # columnSpacing spaces columns apart (X); rowSpacing spaces rows apart (Y).
    positions = [(c["position"]["x"], c["position"]["y"]) for c in calls]
    assert positions == [(5, 25), (7, 25), (5, 27), (7, 27)]
    assert all(c["position"]["unit"] == "mm" for c in calls)


@pytest.mark.unit
def test_place_component_array_start_reference_offset():
    cc, calls = _cmds_recording()
    res = cc.place_component_array(
        {
            "componentId": "R",
            "startPosition": {"x": 0, "y": 0},
            "rows": 1,
            "columns": 3,
            "rowSpacing": 1,
            "columnSpacing": 1,
            "startReference": "R5",
        }
    )
    assert res["success"] is True
    # "R5" -> prefix R, numbering from 5.
    assert [c["reference"] for c in calls] == ["R5", "R6", "R7"]


@pytest.mark.unit
def test_place_component_array_legacy_params_still_work():
    cc, calls = _cmds_recording()
    res = cc.place_component_array(
        {
            "componentId": "R",
            "startPosition": {"x": 0, "y": 0, "unit": "mm"},
            "rows": 2,
            "columns": 3,
            "spacingX": 1,
            "spacingY": 2,
            "count": 6,
            "referencePrefix": "R",
        }
    )
    assert res["success"] is True
    assert [c["reference"] for c in calls] == ["R1", "R2", "R3", "R4", "R5", "R6"]


@pytest.mark.unit
def test_place_component_array_missing_grid_params_is_validation():
    cc, _ = _cmds_recording()
    res = cc.place_component_array(
        {"componentId": "R", "rows": 2, "columns": 2}  # no startPosition / spacing
    )
    assert res["success"] is False
    assert res["errorCode"] == "VALIDATION"
    assert res["errorCode"] != "INTERNAL_ERROR"


@pytest.mark.unit
def test_place_component_array_missing_component_id_is_validation():
    cc, _ = _cmds_recording()
    res = cc.place_component_array(
        {
            "startPosition": {"x": 0, "y": 0},
            "rows": 2,
            "columns": 2,
            "rowSpacing": 1,
            "columnSpacing": 1,
        }
    )
    assert res["success"] is False
    assert res["errorCode"] == "VALIDATION"


@pytest.mark.unit
def test_place_component_array_count_mismatch_is_validation():
    cc, _ = _cmds_recording()
    res = cc.place_component_array(
        {
            "componentId": "R",
            "startPosition": {"x": 0, "y": 0},
            "rows": 2,
            "columns": 2,
            "rowSpacing": 1,
            "columnSpacing": 1,
            "count": 5,  # 2*2 != 5
        }
    )
    assert res["success"] is False
    assert res["errorCode"] == "VALIDATION"


# ---------------------------------------------------------------------------
# Finding 4: the advertised `footprint` param must reach place_component.
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_place_component_array_forwards_footprint_grid():
    cc, calls = _cmds_recording()
    cc.place_component_array(
        {
            "componentId": "R",
            "startPosition": {"x": 0, "y": 0},
            "rows": 1,
            "columns": 2,
            "rowSpacing": 1,
            "columnSpacing": 1,
            "footprint": "Resistor_SMD:R_0402_1005Metric",
        }
    )
    assert calls  # placements happened
    assert all(c.get("footprint") == "Resistor_SMD:R_0402_1005Metric" for c in calls)


@pytest.mark.unit
def test_place_component_array_forwards_footprint_circular():
    cc, calls = _cmds_recording()
    cc.place_component_array(
        {
            "componentId": "R",
            "pattern": "circular",
            "center": {"x": 0, "y": 0},
            "radius": 5,
            "count": 3,
            "angleStep": 120,
            "footprint": "Resistor_SMD:R_0402_1005Metric",
        }
    )
    assert calls
    assert all(c.get("footprint") == "Resistor_SMD:R_0402_1005Metric" for c in calls)


# ---------------------------------------------------------------------------
# Finding 5: spacing/radius are documented mm regardless of the start unit.
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_place_component_array_mil_start_mm_spacing_grid():
    cc, calls = _cmds_recording()
    cc.place_component_array(
        {
            "componentId": "R",
            # 1000 mil == 25.4 mm.  Spacing stays mm.
            "startPosition": {"x": 1000, "y": 0, "unit": "mil"},
            "rows": 1,
            "columns": 2,
            "rowSpacing": 5,
            "columnSpacing": 5,
        }
    )
    xs = [c["position"]["x"] for c in calls]
    assert all(c["position"]["unit"] == "mm" for c in calls)
    assert xs[0] == pytest.approx(25.4)  # start converted to mm
    assert xs[1] == pytest.approx(30.4)  # + 5 mm spacing, NOT + 5 mil


@pytest.mark.unit
def test_place_component_array_mil_center_mm_radius_circular():
    cc, calls = _cmds_recording()
    cc.place_component_array(
        {
            "componentId": "R",
            "pattern": "circular",
            "center": {"x": 1000, "y": 0, "unit": "mil"},  # 25.4 mm
            "radius": 5,  # mm
            "count": 1,
            "angleStep": 90,
            "angleStart": 0,
        }
    )
    # angle 0 → x = center_x + radius = 25.4 + 5 = 30.4 mm.
    assert calls[0]["position"]["unit"] == "mm"
    assert calls[0]["position"]["x"] == pytest.approx(30.4)


# ---------------------------------------------------------------------------
# Finding 6: non-positive spacing / radius must be refused (would stack copies).
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("col_sp,row_sp", [(0, 1), (1, 0), (-1, 1), (1, -1)])
def test_place_component_array_nonpositive_spacing_refused(col_sp, row_sp):
    cc, calls = _cmds_recording()
    res = cc.place_component_array(
        {
            "componentId": "R",
            "startPosition": {"x": 0, "y": 0},
            "rows": 2,
            "columns": 2,
            "columnSpacing": col_sp,
            "rowSpacing": row_sp,
        }
    )
    assert res["success"] is False
    assert res["errorCode"] == "VALIDATION"
    assert not calls  # refused before any placement


@pytest.mark.unit
def test_place_component_array_positive_spacing_passes():
    cc, _ = _cmds_recording()
    res = cc.place_component_array(
        {
            "componentId": "R",
            "startPosition": {"x": 0, "y": 0},
            "rows": 2,
            "columns": 2,
            "columnSpacing": 1,
            "rowSpacing": 1,
        }
    )
    assert res["success"] is True


@pytest.mark.unit
def test_place_component_array_negative_radius_refused():
    cc, calls = _cmds_recording()
    res = cc.place_component_array(
        {
            "componentId": "R",
            "pattern": "circular",
            "center": {"x": 0, "y": 0},
            "radius": -5,
            "count": 3,
            "angleStep": 120,
        }
    )
    assert res["success"] is False
    assert res["errorCode"] == "VALIDATION"
    assert not calls


# ---------------------------------------------------------------------------
# Finding 7: per-item failures are surfaced, not silently dropped.
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_place_component_array_all_fail_is_refusal():
    cc = ComponentCommands(board=MagicMock(), library_manager=MagicMock())
    cc.place_component = lambda p: {  # type: ignore[assignment]
        "success": False,
        "message": f"could not place {p['reference']}",
        "errorCode": "VALIDATION",
    }
    res = cc.place_component_array(
        {
            "componentId": "R",
            "startPosition": {"x": 0, "y": 0},
            "rows": 1,
            "columns": 2,
            "rowSpacing": 1,
            "columnSpacing": 1,
            "startReference": "R1",
        }
    )
    assert res["success"] is False
    assert res["errorCode"] == "VALIDATION"
    assert len(res["failures"]) == 2
    assert res["failures"][0]["reference"] == "R1"
    assert "could not place" in res["failures"][0]["message"]


@pytest.mark.unit
def test_place_component_array_partial_success_lists_failures():
    cc = ComponentCommands(board=MagicMock(), library_manager=MagicMock())
    seen = {"n": 0}

    def place(p: Dict[str, Any]) -> Dict[str, Any]:
        seen["n"] += 1
        if seen["n"] == 2:  # second placement fails
            return {"success": False, "message": "duplicate", "errorCode": "COMPONENT_EXISTS"}
        return {
            "success": True,
            "component": {"reference": p["reference"], "position": p["position"]},
        }

    cc.place_component = place  # type: ignore[assignment]
    res = cc.place_component_array(
        {
            "componentId": "R",
            "startPosition": {"x": 0, "y": 0},
            "rows": 1,
            "columns": 3,
            "rowSpacing": 1,
            "columnSpacing": 1,
            "startReference": "R1",
        }
    )
    assert res["success"] is True  # partial success
    assert len(res["components"]) == 2
    assert len(res["failures"]) == 1
    assert res["failures"][0]["reference"] == "R2"
    assert res["failures"][0]["errorCode"] == "COMPONENT_EXISTS"
