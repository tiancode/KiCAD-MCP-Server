"""Unit tests for the rebuilt duplicate_component (F2).

Covers the KiCAD-10 fix and the TS/Python contract alignment:

* deep-copy via the FOOTPRINT copy constructor (no more PAD.Copy(), which was
  removed in KiCAD 10 and raised "'PAD' object has no attribute 'Copy'");
* `offset` (relative, unit-aware) placement, `count` N>1 with sequential
  references, and auto-annotation of the new reference when omitted;
* pad nets cleared on every duplicate (a copy must land unconnected).

pcbnew is stubbed globally by tests/conftest.py, so these assert on the pcbnew
API calls the implementation makes. End-to-end behaviour against the real
pcbnew 10.0.4 bindings is exercised separately (see the work-package report).
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

import pcbnew  # noqa: E402  — stubbed by conftest
from commands.component._placement import (  # noqa: E402
    _allocate_duplicate_refs,
    _parse_ref,
)

MM = 1_000_000


@pytest.fixture
def fresh_pcbnew_mock():
    pcbnew.reset_mock()
    pcbnew.DEGREES_T = "deg"
    return pcbnew


def _make_cmds(source_ref="R2", source_pos=(158 * MM, 60 * MM), existing=("R1", "R2"), pads=2):
    """Build a ComponentCommands with a stubbed board + source footprint."""
    from commands.component import ComponentCommands

    source = MagicMock(name="source")
    source.GetReference.return_value = source_ref
    source.GetPosition.return_value = SimpleNamespace(x=source_pos[0], y=source_pos[1])

    board = MagicMock(name="board")

    def _find(ref):
        return source if ref == source_ref else None

    board.FindFootprintByReference.side_effect = _find

    footprints = []
    for r in existing:
        fp = MagicMock()
        fp.GetReference.return_value = r
        footprints.append(fp)
    board.GetFootprints.return_value = footprints

    # The duplicate produced by pcbnew.FOOTPRINT(source) — configure its pads so
    # the net-clearing loop can iterate.
    new_module = pcbnew.FOOTPRINT.return_value
    new_module.Pads.return_value = [MagicMock(name=f"pad{i}") for i in range(pads)]

    cmds = ComponentCommands(board=board, library_manager=MagicMock())
    return cmds, board, source, new_module


# ---------------------------------------------------------------------------
# reference allocation helper (pure)
# ---------------------------------------------------------------------------


class TestReferenceHelpers:
    def test_parse_ref(self):
        assert _parse_ref("R2") == ("R", 2)
        assert _parse_ref("R98") == ("R", 98)
        assert _parse_ref("U10") == ("U", 10)
        assert _parse_ref("REF") == ("REF", None)

    def test_auto_next_free_from_source(self):
        # R2 with R2,R3 taken → next free is R4.
        assert _allocate_duplicate_refs("R2", None, 1, {"R2", "R3"}) == ["R4"]

    def test_auto_simple_increment(self):
        assert _allocate_duplicate_refs("R98", None, 1, {"R98"}) == ["R99"]

    def test_count_sequential_auto(self):
        assert _allocate_duplicate_refs("R2", None, 3, {"R2"}) == ["R3", "R4", "R5"]

    def test_explicit_then_sequential(self):
        assert _allocate_duplicate_refs("R2", "R98", 3, {"R1", "R2"}) == ["R98", "R99", "R100"]

    def test_explicit_collision_raises(self):
        with pytest.raises(ValueError):
            _allocate_duplicate_refs("R2", "R99", 1, {"R2", "R99"})

    def test_count_skips_used(self):
        assert _allocate_duplicate_refs("R2", None, 3, {"R2", "R4"}) == ["R3", "R5", "R6"]


# ---------------------------------------------------------------------------
# duplicate_component behaviour
# ---------------------------------------------------------------------------


class TestDuplicateComponent:
    def test_offset_only_auto_reference(self, fresh_pcbnew_mock):
        cmds, board, source, new_module = _make_cmds()

        result = cmds.duplicate_component(
            {"reference": "R2", "offset": {"x": 20, "y": 20, "unit": "mm"}}
        )

        assert result["success"] is True
        assert result["count"] == 1
        assert len(result["components"]) == 1
        # Auto reference: R2 → next free R3.
        assert result["components"][0]["reference"] == "R3"
        new_module.SetReference.assert_called_once_with("R3")
        # Copy constructor used with the SOURCE footprint (deep copy).
        fresh_pcbnew_mock.FOOTPRINT.assert_called_once_with(source)
        # Position = source + offset (178,80 mm).
        assert (178 * MM, 80 * MM) in [c.args for c in fresh_pcbnew_mock.VECTOR2I.call_args_list]
        board.Add.assert_called_once()

    def test_pads_nets_cleared(self, fresh_pcbnew_mock):
        cmds, board, source, new_module = _make_cmds(pads=2)
        cmds.duplicate_component({"reference": "R2", "offset": {"x": 5, "y": 0}})
        for pad in new_module.Pads.return_value:
            pad.SetNetCode.assert_called_once_with(0)

    def test_offset_unit_defaults_to_mm(self, fresh_pcbnew_mock):
        cmds, board, source, new_module = _make_cmds()
        cmds.duplicate_component({"reference": "R2", "offset": {"x": 10, "y": 0}})
        assert (168 * MM, 60 * MM) in [c.args for c in fresh_pcbnew_mock.VECTOR2I.call_args_list]

    def test_count_creates_sequential_offsets(self, fresh_pcbnew_mock):
        cmds, board, source, new_module = _make_cmds(existing=("R1", "R2"))

        result = cmds.duplicate_component(
            {
                "reference": "R2",
                "newReference": "R98",
                "offset": {"x": 20, "y": 20, "unit": "mm"},
                "count": 3,
            }
        )

        assert result["success"] is True
        assert result["count"] == 3
        assert [c["reference"] for c in result["components"]] == ["R98", "R99", "R100"]
        new_module.SetReference.assert_has_calls(
            [call("R98"), call("R99"), call("R100")], any_order=False
        )
        vecs = [c.args for c in fresh_pcbnew_mock.VECTOR2I.call_args_list]
        assert (178 * MM, 80 * MM) in vecs
        assert (198 * MM, 100 * MM) in vecs
        assert (218 * MM, 120 * MM) in vecs
        assert board.Add.call_count == 3

    def test_absolute_position(self, fresh_pcbnew_mock):
        cmds, board, source, new_module = _make_cmds()
        cmds.duplicate_component(
            {"reference": "R2", "newReference": "R5", "position": {"x": 100, "y": 50, "unit": "mm"}}
        )
        assert (100 * MM, 50 * MM) in [c.args for c in fresh_pcbnew_mock.VECTOR2I.call_args_list]

    def test_explicit_new_reference_collision(self, fresh_pcbnew_mock):
        cmds, board, source, new_module = _make_cmds(existing=("R1", "R2", "R99"))
        result = cmds.duplicate_component(
            {"reference": "R2", "newReference": "R99", "offset": {"x": 5, "y": 5}}
        )
        assert result["success"] is False
        assert "already exists" in result["errorDetails"]

    def test_missing_reference(self, fresh_pcbnew_mock):
        cmds, board, source, new_module = _make_cmds()
        result = cmds.duplicate_component({"offset": {"x": 5, "y": 5}})
        assert result["success"] is False
        assert "reference is required" in result["errorDetails"]

    def test_source_not_found(self, fresh_pcbnew_mock):
        cmds, board, source, new_module = _make_cmds()
        result = cmds.duplicate_component({"reference": "R999", "offset": {"x": 5, "y": 5}})
        assert result["success"] is False
        assert "Could not find" in result["errorDetails"]

    def test_no_pad_copy_attribute_used(self, fresh_pcbnew_mock):
        """The removed PAD.Copy() path must not be exercised (KiCAD 10 crash)."""
        cmds, board, source, new_module = _make_cmds()
        cmds.duplicate_component({"reference": "R2", "offset": {"x": 5, "y": 5}})
        # PAD() is never constructed and .Copy() is never called on any pad.
        fresh_pcbnew_mock.PAD.assert_not_called()
