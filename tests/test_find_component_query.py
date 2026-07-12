"""Unit tests for find_component's free-text `query` param (F13).

Previously `{"query":"crystal"}` returned "Missing search criteria" because the
handler only understood reference/value/footprint. `query` is now a
case-insensitive substring matched across reference, value AND footprint-id, and
combines with the targeted filters via AND.

pcbnew is stubbed globally by tests/conftest.py.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _fp(ref, value, fpid, pos=(0, 0)):
    m = MagicMock()
    m.GetReference.return_value = ref
    m.GetValue.return_value = value
    m.GetFPIDAsString.return_value = fpid
    m.GetPosition.return_value = SimpleNamespace(x=pos[0], y=pos[1])
    m.GetOrientation.return_value = SimpleNamespace(AsDegrees=lambda: 0.0)
    return m


@pytest.fixture
def cmds():
    from commands.component import ComponentCommands

    board = MagicMock(name="board")
    board.GetFootprints.return_value = [
        _fp("Y1", "16MHz", "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm"),
        _fp("R1", "10k", "Resistor_SMD:R_0805_2012Metric"),
        _fp("R2", "4.7k", "Resistor_SMD:R_0805_2012Metric"),
        _fp("U1", "GD32F103VET6", "Package_QFP:LQFP-100_14x14mm_P0.5mm"),
    ]
    board.GetLayerName.return_value = "F.Cu"
    return ComponentCommands(board=board, library_manager=MagicMock())


class TestFindComponentQuery:
    def test_query_matches_footprint_id(self, cmds):
        # "crystal" appears only in the footprint-id of Y1.
        result = cmds.find_component({"query": "crystal"})
        assert result["success"] is True
        assert result["matchCount"] == 1
        assert result["components"][0]["reference"] == "Y1"

    def test_query_matches_value(self, cmds):
        result = cmds.find_component({"query": "gd32"})
        assert result["matchCount"] == 1
        assert result["components"][0]["reference"] == "U1"

    def test_query_matches_reference(self, cmds):
        # "r" matches references R1/R2 and also "Metric"/"Resistor" in fpids and
        # values — an OR across the three fields, so at least R1,R2,U1 (GD32...).
        result = cmds.find_component({"query": "r2"})
        refs = {c["reference"] for c in result["components"]}
        assert "R2" in refs

    def test_query_case_insensitive(self, cmds):
        assert cmds.find_component({"query": "CRYSTAL"})["matchCount"] == 1

    def test_query_no_match(self, cmds):
        result = cmds.find_component({"query": "nonexistent-xyz"})
        assert result["success"] is True
        assert result["matchCount"] == 0

    def test_query_combines_with_value_filter_AND(self, cmds):
        # query "resistor" matches R1,R2 (fpid); value "10k" narrows to R1.
        result = cmds.find_component({"query": "resistor", "value": "10k"})
        assert result["matchCount"] == 1
        assert result["components"][0]["reference"] == "R1"

    def test_targeted_filters_still_work(self, cmds):
        result = cmds.find_component({"reference": "R"})
        refs = {c["reference"] for c in result["components"]}
        assert refs == {"R1", "R2"}

    def test_footprint_filter_now_exposed(self, cmds):
        result = cmds.find_component({"footprint": "lqfp"})
        assert result["matchCount"] == 1
        assert result["components"][0]["reference"] == "U1"

    def test_missing_all_criteria_errors(self, cmds):
        result = cmds.find_component({})
        assert result["success"] is False
        assert "Missing search criteria" in result["message"]

    def test_explicit_null_query_is_not_criteria(self, cmds):
        # A None query (JSON null) must not count as a criterion nor crash.
        result = cmds.find_component({"query": None})
        assert result["success"] is False
        assert "Missing search criteria" in result["message"]
