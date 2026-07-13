"""Page-awareness regression tests (S2 + S9).

S2: placeAllUnits must lay a multi-unit symbol out in a grid that stays inside
    the sheet — tall units wrap into a new column instead of stacking off the
    bottom of the A4 page. Units that are unavoidably off-page are flagged.
S9: add/move must reject absurd coordinates (>10× a page dimension) and flag a
    merely off-page (but valid) target with a warning that names the page size.
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from handlers.schematic_component._placement import (  # noqa: E402
    handle_add_schematic_component,
    handle_move_schematic_component,
)

_EMPTY_SCH = Path(__file__).resolve().parent.parent / "python" / "templates" / "empty.kicad_sch"


# A 2-unit symbol whose units are ~100 mm tall — two stacked vertically would
# run off an A4 sheet (210 mm), forcing the page-aware grid to wrap to columns.
def _pin(num, px, py, name):
    return (
        f"      (pin passive line (at {px} {py} 0) (length 2.54)\n"
        f'        (name "{name}" (effects (font (size 1.27 1.27))))\n'
        f'        (number "{num}" (effects (font (size 1.27 1.27)))))'
    )


def _unit_body(unit, base_num):
    # Pins span y from -50 to +50 (100 mm tall), x from -10 to +10 (20 mm wide).
    pins = "\n".join(
        [
            _pin(base_num + 0, -10, 50, f"P{base_num}A"),
            _pin(base_num + 1, 10, 25, f"P{base_num}B"),
            _pin(base_num + 2, -10, -50, f"P{base_num}C"),
        ]
    )
    return f'    (symbol "TallDual_{unit}_1"\n{pins}\n    )'


_LIB_KICAD_SYM = f"""\
(kicad_symbol_lib (version 20211014) (generator test)
  (symbol "TallDual" (pin_names (offset 0.127)) (in_bom yes) (on_board yes)
    (property "Reference" "U" (at 0 5.08 0) (effects (font (size 1.27 1.27))))
    (property "Value" "TallDual" (at 0 -5.08 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
{_unit_body(1, 1)}
{_unit_body(2, 4)}
  )
  (symbol "Simple" (pin_names (offset 0.127)) (in_bom yes) (on_board yes)
    (property "Reference" "R" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
    (property "Value" "Simple" (at 0 -2.54 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (symbol "Simple_1_1"
{_pin(1, 0, 2.54, "A")}
{_pin(2, 0, -2.54, "B")}
    )
  )
)
"""


def _project(tmp_path):
    (tmp_path / "TestLib.kicad_sym").write_text(_LIB_KICAD_SYM, encoding="utf-8")
    (tmp_path / "sym-lib-table").write_text(
        '(sym_lib_table\n  (lib (name "TestLib")(type "KiCad")'
        f'(uri "{tmp_path / "TestLib.kicad_sym"}")(options "")(descr ""))\n)\n',
        encoding="utf-8",
    )
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(_EMPTY_SCH.read_text(encoding="utf-8"), encoding="utf-8")
    return sch


@pytest.mark.unit
class TestPlaceAllUnitsPageAware:
    def test_tall_units_wrap_into_columns_not_off_page(self, tmp_path):
        sch = _project(tmp_path)
        res = handle_add_schematic_component(
            None,
            {
                "schematicPath": str(sch),
                "snapToGrid": False,  # clean coordinates for assertions
                "component": {
                    "library": "TestLib",
                    "type": "TallDual",
                    "reference": "U1",
                    "x": 150,
                    "y": 100,
                    "placeAllUnits": True,
                },
            },
        )
        assert res["success"] is True
        assert res["units"]["placed"] == [1, 2]
        pos = res["unitPositions"]
        assert set(pos) == {"1", "2"}
        # The regression: unit 2 must NOT be stacked far below the page bottom.
        # It wraps into a new column (x steps right), staying on the A4 sheet.
        assert pos["2"]["x"] > pos["1"]["x"], "unit 2 should wrap to a new column"
        assert pos["2"]["y"] + 50 <= res["pageSize"]["height"] + 1
        assert res["pageSize"]["name"] == "A4"
        # Both units fit → no off-page warning.
        assert "offPageWarning" not in res
        assert "offPageUnits" not in res

    def test_unavoidably_off_page_unit_is_flagged(self, tmp_path):
        sch = _project(tmp_path)
        # Placed near the bottom: unit 1's downward pins (to y≈220) can't fit.
        res = handle_add_schematic_component(
            None,
            {
                "schematicPath": str(sch),
                "snapToGrid": False,
                "component": {
                    "library": "TestLib",
                    "type": "TallDual",
                    "reference": "U2",
                    "x": 150,
                    "y": 170,
                    "placeAllUnits": True,
                },
            },
        )
        assert res["success"] is True
        assert "offPageWarning" in res
        assert 1 in res["offPageUnits"]
        assert "A4" in res["offPageWarning"]


@pytest.mark.unit
class TestAddPositionGuard:
    def test_absurd_coordinate_rejected(self, tmp_path):
        sch = _project(tmp_path)
        res = handle_add_schematic_component(
            None,
            {
                "schematicPath": str(sch),
                "component": {
                    "library": "TestLib",
                    "type": "Simple",
                    "reference": "R1",
                    "x": 99999,
                    "y": 100,
                },
            },
        )
        assert res["success"] is False
        assert res["errorCode"] == "POSITION_OFF_SHEET"
        assert "99999" in res["message"]

    def test_off_page_but_valid_is_placed_with_warning(self, tmp_path):
        sch = _project(tmp_path)
        res = handle_add_schematic_component(
            None,
            {
                "schematicPath": str(sch),
                "snapToGrid": False,
                "component": {
                    "library": "TestLib",
                    "type": "Simple",
                    "reference": "R2",
                    "x": 150,
                    "y": 300,  # below the A4 bottom (210) but not absurd
                },
            },
        )
        assert res["success"] is True
        assert "offPageWarning" in res
        assert "A4" in res["offPageWarning"]
        assert res["pageSize"]["height"] == 210.0


# ---------------------------------------------------------------------------
# S9: move_schematic_component page guard
# ---------------------------------------------------------------------------

_PLACED = """\
  (symbol (lib_id "Device:R") (at 50 50 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    (property "Reference" "R1" (at 51.27 47.46 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 51.27 52.54 0) (effects (font (size 1.27 1.27))))
  )
"""


def _sch_with_symbol(tmp_path):
    content = f"""(kicad_sch (version 20250114) (generator "test")
  (uuid aaaaaaaa-0000-0000-0000-000000000001)
  (paper "A4")
  (lib_symbols)
{_PLACED}
  (sheet_instances (path "/" (page "1")))
)
"""
    p = tmp_path / "move.kicad_sch"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.mark.unit
class TestMovePositionGuard:
    def test_absurd_move_rejected(self, tmp_path):
        sch = _sch_with_symbol(tmp_path)
        res = handle_move_schematic_component(
            None,
            {"schematicPath": str(sch), "reference": "R1", "position": {"x": 99999, "y": 100}},
        )
        assert res["success"] is False
        assert res["errorCode"] == "POSITION_OFF_SHEET"
        assert "99999" in res["message"]
        # The symbol must not have moved.
        assert "50 50" in sch.read_text(encoding="utf-8")

    def test_off_page_move_succeeds_with_warning(self, tmp_path):
        sch = _sch_with_symbol(tmp_path)
        res = handle_move_schematic_component(
            None,
            {
                "schematicPath": str(sch),
                "reference": "R1",
                "position": {"x": 150, "y": 300},
                "snapToGrid": False,
            },
        )
        assert res["success"] is True
        assert "offPageWarning" in res
        assert "A4" in res["offPageWarning"]
        assert res["newPosition"] == {"x": 150, "y": 300}
