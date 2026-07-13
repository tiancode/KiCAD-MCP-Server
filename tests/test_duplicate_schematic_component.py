"""S13: duplicate_schematic_component clones a placed symbol.

The clone carries the same library symbol, value, footprint, and custom
sourcing properties (MPN/LCSC), lands at an offset (or explicit position), and
auto-assigns the next free reference of the same prefix.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from handlers.schematic_component._duplicate import (  # noqa: E402
    handle_duplicate_schematic_component,
)
from handlers.schematic_component._placement import (  # noqa: E402
    handle_add_schematic_component,
)
from handlers.schematic_component._properties import (  # noqa: E402
    handle_get_schematic_component,
)
from kicad_interface import KiCADInterface  # noqa: E402

_EMPTY_SCH = Path(__file__).resolve().parent.parent / "python" / "templates" / "empty.kicad_sch"

_LIB_KICAD_SYM = """\
(kicad_symbol_lib (version 20211014) (generator test)
  (symbol "Simple" (pin_names (offset 0.127)) (in_bom yes) (on_board yes)
    (property "Reference" "R" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
    (property "Value" "Simple" (at 0 -2.54 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (symbol "Simple_1_1"
      (pin passive line (at 0 2.54 270) (length 1.27)
        (name "A" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 0 -2.54 90) (length 1.27)
        (name "B" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27)))))
    )
  )
)
"""


def _iface():
    return KiCADInterface.__new__(KiCADInterface)


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


def _place_source(sch, iface):
    """Place R1 with a value, footprint, and a custom LCSC property."""
    handle_add_schematic_component(
        iface,
        {
            "schematicPath": str(sch),
            "snapToGrid": False,
            "component": {
                "library": "TestLib",
                "type": "Simple",
                "reference": "R1",
                "value": "10k",
                "footprint": "Resistor_SMD:R_0603_1608Metric",
                "x": 100,
                "y": 100,
            },
        },
    )
    from handlers.schematic_component._properties import handle_edit_schematic_component

    handle_edit_schematic_component(
        iface,
        {
            "schematicPath": str(sch),
            "reference": "R1",
            "properties": {"LCSC": "C25804", "MPN": "RC0603FR-0710KL"},
        },
    )


@pytest.mark.unit
class TestDuplicateSchematicComponent:
    def test_default_offset_and_auto_reference(self, tmp_path):
        iface = _iface()
        sch = _project(tmp_path)
        _place_source(sch, iface)

        res = handle_duplicate_schematic_component(
            iface, {"schematicPath": str(sch), "reference": "R1"}
        )
        assert res["success"] is True
        # Auto-assigned next free ref of the same prefix.
        assert res["reference"] == "R2"
        # Default offset {x:10, y:0} from the source at (100, 100).
        assert res["position"]["x"] == 110.0
        assert res["position"]["y"] == 100.0
        # Custom sourcing props were copied.
        assert set(res["copiedProperties"]) >= {"LCSC", "MPN"}

        # The clone actually exists with the same value/footprint + custom props.
        got = handle_get_schematic_component(iface, {"schematicPath": str(sch), "reference": "R2"})
        assert got["success"] is True
        assert got["fields"]["Value"]["value"] == "10k"
        assert got["fields"]["Footprint"]["value"] == "Resistor_SMD:R_0603_1608Metric"
        assert got["fields"]["LCSC"]["value"] == "C25804"
        assert got["fields"]["MPN"]["value"] == "RC0603FR-0710KL"

    def test_explicit_new_reference_and_position(self, tmp_path):
        iface = _iface()
        sch = _project(tmp_path)
        _place_source(sch, iface)

        res = handle_duplicate_schematic_component(
            iface,
            {
                "schematicPath": str(sch),
                "reference": "R1",
                "newReference": "R7",
                "position": {"x": 150, "y": 80},
            },
        )
        assert res["success"] is True
        assert res["reference"] == "R7"
        assert res["position"] == {"x": 150.0, "y": 80.0}

    def test_custom_offset(self, tmp_path):
        iface = _iface()
        sch = _project(tmp_path)
        _place_source(sch, iface)

        res = handle_duplicate_schematic_component(
            iface,
            {"schematicPath": str(sch), "reference": "R1", "offset": {"x": 0, "y": 20}},
        )
        assert res["success"] is True
        assert res["position"]["x"] == 100.0
        assert res["position"]["y"] == 120.0

    def test_duplicate_reference_collision_rejected(self, tmp_path):
        iface = _iface()
        sch = _project(tmp_path)
        _place_source(sch, iface)
        # Place a second symbol R2 so an explicit newReference=R2 collides.
        handle_add_schematic_component(
            iface,
            {
                "schematicPath": str(sch),
                "snapToGrid": False,
                "component": {
                    "library": "TestLib",
                    "type": "Simple",
                    "reference": "R2",
                    "x": 130,
                    "y": 100,
                },
            },
        )
        res = handle_duplicate_schematic_component(
            iface,
            {"schematicPath": str(sch), "reference": "R1", "newReference": "R2"},
        )
        assert res["success"] is False
        assert res["errorCode"] == "REFERENCE_EXISTS"

    def test_missing_source_returns_failure(self, tmp_path):
        iface = _iface()
        sch = _project(tmp_path)
        res = handle_duplicate_schematic_component(
            iface, {"schematicPath": str(sch), "reference": "U99"}
        )
        assert res["success"] is False
