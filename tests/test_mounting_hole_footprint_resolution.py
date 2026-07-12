"""Unit tests for the MountingHole footprint-name resolver (F8).

add_mounting_hole used to emit `MountingHole_<d>mm` for every size, but the
stock KiCAD 10 lib has NO bare footprint for several common sizes (3.2 / 4.3 /
5.3 / 6.4 ship only as `_M3` / `_M4` / …), so DRC flagged the parts with
lib_footprint_issues. The resolver now maps a diameter to a name that actually
exists, preferring the plain (non-_Pad) variant, and falls back to the legacy
synthetic name only when the stock lib can't be located.

The resolver reads the real footprint dir, so these tests monkeypatch the
directory-listing function with a controlled set for determinism.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

import commands.board.outline as outline  # noqa: E402
import pcbnew  # noqa: E402  — stubbed by conftest
from commands.board.outline import (  # noqa: E402
    BoardOutlineCommands,
    _resolve_mountinghole_footprint,
)

# A representative subset of the real /usr/share/kicad/footprints/MountingHole.pretty
_STOCK = {
    "MountingHole_2mm",
    "MountingHole_2.2mm_M2",
    "MountingHole_2.2mm_M2_Pad",
    "MountingHole_2.2mm_M2_ISO7380",
    "MountingHole_2.5mm",
    "MountingHole_2.5mm_Pad",
    "MountingHole_2.7mm",
    "MountingHole_2.7mm_M2.5",
    "MountingHole_3mm",
    "MountingHole_3.2mm_M3",
    "MountingHole_3.2mm_M3_Pad",
    "MountingHole_3.2mm_M3_DIN965",
    "MountingHole_3.5mm",
    "MountingHole_4mm",
    "MountingHole_4.3mm_M4",
    "MountingHole_4.3mm_M4_Pad",
    "MountingHole_5mm",
    "MountingHole_5.3mm_M5",
    "MountingHole_6mm",
    "MountingHole_6.4mm_M6",
    "MountingHole_8.4mm_M8",
}


@pytest.fixture
def stock(monkeypatch):
    monkeypatch.setattr(outline, "_list_mountinghole_footprints", lambda: set(_STOCK))
    return _STOCK


class TestResolver:
    @pytest.mark.parametrize(
        "diameter,expected",
        [
            (2, "MountingHole_2mm"),  # bare plain exists
            (2.5, "MountingHole_2.5mm"),  # bare plain exists (over _Pad)
            (2.7, "MountingHole_2.7mm"),  # bare plain preferred over _M2.5
            (3, "MountingHole_3mm"),
            (3.2, "MountingHole_3.2mm_M3"),  # no bare 3.2mm → screw variant
            (3.5, "MountingHole_3.5mm"),
            (4, "MountingHole_4mm"),
            (4.3, "MountingHole_4.3mm_M4"),
            (5, "MountingHole_5mm"),
            (5.3, "MountingHole_5.3mm_M5"),
            (6, "MountingHole_6mm"),
            (6.4, "MountingHole_6.4mm_M6"),
            (8.4, "MountingHole_8.4mm_M8"),
        ],
    )
    def test_common_sizes_resolve_to_existing(self, stock, diameter, expected):
        resolved = _resolve_mountinghole_footprint(diameter)
        assert resolved == expected
        assert resolved in stock, "resolved name must exist in the stock lib"

    def test_never_returns_pad_variant(self, stock):
        assert "_Pad" not in _resolve_mountinghole_footprint(3.2)
        assert "_Pad" not in _resolve_mountinghole_footprint(2.2)

    def test_unknown_size_falls_to_closest(self, stock):
        # 3.3 mm has no entry → closest plain diameter is 3.2mm_M3 (delta 0.1).
        assert _resolve_mountinghole_footprint(3.3) == "MountingHole_3.2mm_M3"

    def test_lib_not_found_returns_none(self, monkeypatch):
        monkeypatch.setattr(outline, "_list_mountinghole_footprints", lambda: set())
        assert _resolve_mountinghole_footprint(3.2) is None


class TestAddMountingHoleIntegration:
    @pytest.fixture
    def fresh_pcbnew_mock(self):
        pcbnew.reset_mock()
        pcbnew.PAD_ATTRIB_NPTH = "NPTH"
        pcbnew.PAD_ATTRIB_PTH = "PTH"
        pcbnew.PAD_SHAPE_CIRCLE = "circle"
        pcbnew.F_Mask = "F.Mask"
        pcbnew.B_Mask = "B.Mask"
        pcbnew.SHAPE_T_CIRCLE = "circle_shape"
        pcbnew.F_CrtYd = "F.CrtYd"
        pcbnew.F_Fab = "F.Fab"
        return pcbnew

    @pytest.fixture
    def cmds(self, fresh_pcbnew_mock):
        board = MagicMock(name="board")
        board.GetFootprints.return_value = []
        return BoardOutlineCommands(board=board)

    def test_default_32mm_uses_real_m3_name(self, cmds, fresh_pcbnew_mock, stock):
        result = cmds.add_mounting_hole(
            {"position": {"x": 0, "y": 0, "unit": "mm"}, "diameter": 3.2}
        )
        assert result["success"] is True
        fresh_pcbnew_mock.LIB_ID.assert_called_once_with("MountingHole", "MountingHole_3.2mm_M3")
        assert result["mountingHole"]["footprintLibId"] == "MountingHole:MountingHole_3.2mm_M3"

    def test_default_2mm_uses_bare_name(self, cmds, fresh_pcbnew_mock, stock):
        cmds.add_mounting_hole({"position": {"x": 0, "y": 0, "unit": "mm"}, "diameter": 2})
        fresh_pcbnew_mock.LIB_ID.assert_called_once_with("MountingHole", "MountingHole_2mm")

    def test_explicit_lib_id_bypasses_resolver(self, cmds, fresh_pcbnew_mock, stock):
        cmds.add_mounting_hole(
            {
                "position": {"x": 0, "y": 0, "unit": "mm"},
                "diameter": 3.2,
                "footprintLibId": "MountingHole:MountingHole_3.2mm_M3_Pad",
            }
        )
        fresh_pcbnew_mock.LIB_ID.assert_called_once_with(
            "MountingHole", "MountingHole_3.2mm_M3_Pad"
        )

    def test_lib_missing_keeps_synthetic_name(self, cmds, fresh_pcbnew_mock, monkeypatch):
        monkeypatch.setattr(outline, "_list_mountinghole_footprints", lambda: set())
        cmds.add_mounting_hole({"position": {"x": 0, "y": 0, "unit": "mm"}, "diameter": 3.2})
        fresh_pcbnew_mock.LIB_ID.assert_called_once_with("MountingHole", "MountingHole_3.2mm")
