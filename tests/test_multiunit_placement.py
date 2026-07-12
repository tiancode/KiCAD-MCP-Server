"""F1 regression tests — multi-unit symbols with unplaced units.

Background
----------
A multi-unit part (e.g. an MCU split into a GPIO unit and a power-pin unit) is
placed as one ``(symbol …)`` instance per unit, all sharing the reference. Every
unit's pins are drawn relative to the SAME library origin, so the only thing
distinguishing unit B's pins from unit A's is the placed instance position.

The bug (F1)
------------
When only ONE unit was on the sheet, ``PinLocator._get_symbol_transform`` took a
``len(instances) == 1`` shortcut and returned that lone instance for ANY
requested unit — so a pin on the unplaced unit was located on the placed unit's
origin. ``get_all_symbol_pins`` then reported phantom coordinates, and
``add_schematic_net_label`` happily "connected" a label to a pin that isn't on
the sheet, silently leaving the part with no power connectivity.

The fix
-------
* ``get_pin_location`` / ``get_all_symbol_pins`` return None / drop a pin whose
  numbered unit is not placed (only falling back for a genuinely single-unit
  part), so phantom coordinates are never produced.
* ``get_schematic_pin_locations`` marks such pins ``placed: false`` with no
  coordinates and a summary warning.
* ``add_schematic_net_label`` and ``connect_to_net`` refuse a pin on an unplaced
  unit with a message that names the unit and the exact placement fix.
* ``add_schematic_component`` reports the unit situation and supports
  ``placeAllUnits``.
"""

import sys
import tempfile
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from commands.connection_schematic import ConnectionManager  # noqa: E402
from commands.pin_locator import PinLocator  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture: a 2-unit part (GPIO unit + power unit) built directly as .kicad_sch
# ---------------------------------------------------------------------------

# Library pin layout (Y-up), grouped by unit. Unit 2's pins carry power names.
_UNIT_PINS = {
    1: [("1", -7.62, 2.54, 0, "PA0"), ("2", -7.62, 0.0, 0, "PA1")],
    2: [("3", -7.62, 2.54, 0, "VDD"), ("4", -7.62, -2.54, 0, "VSS")],
}


def _pin_text(number, px, py, angle, name):
    return (
        f"        (pin passive line (at {px} {py} {angle}) (length 2.54)\n"
        f'          (name "{name}" (effects (font (size 1.27 1.27))))\n'
        f'          (number "{number}" (effects (font (size 1.27 1.27))))\n'
        f"        )"
    )


def _unit_subsymbol(unit):
    pins = "\n".join(_pin_text(n, px, py, a, nm) for n, px, py, a, nm in _UNIT_PINS[unit])
    return f'      (symbol "DualBank_{unit}_1"\n{pins}\n      )'


def _lib_symbol_def():
    subs = "\n".join(_unit_subsymbol(u) for u in (1, 2))
    return f"""    (symbol "Sim:DualBank" (pin_names (offset 0.127)) (in_bom yes) (on_board yes)
      (property "Reference" "U" (at 0 5.08 0) (effects (font (size 1.27 1.27))))
      (property "Value" "DualBank" (at 0 -5.08 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
      (property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
{subs}
    )"""


def _instance(ref, unit, x, y):
    u = str(uuid.uuid4())
    pin_lines = "\n".join(f'    (pin "{n}" (uuid "{uuid.uuid4()}"))' for n, *_ in _UNIT_PINS[unit])
    return f"""  (symbol (lib_id "Sim:DualBank") (at {x} {y} 0) (unit {unit})
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "{u}")
    (property "Reference" "{ref}" (at {x} {y - 7.62} 0) (effects (font (size 1.27 1.27))))
    (property "Value" "DualBank" (at {x} {y + 7.62} 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at {x} {y} 0) (effects (font (size 1.27 1.27)) hide))
    (property "Datasheet" "~" (at {x} {y} 0) (effects (font (size 1.27 1.27)) hide))
{pin_lines}
    (instances
      (project "test"
        (path "/" (reference "{ref}") (unit {unit}))
      )
    )
  )"""


def _write_schematic(instances) -> Path:
    body = "\n".join(instances)
    content = f"""(kicad_sch (version 20250114) (generator "test")
  (uuid {uuid.uuid4()})
  (paper "A4")
  (lib_symbols
{_lib_symbol_def()}
  )
{body}
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""
    path = Path(tempfile.mkdtemp()) / "dualbank.kicad_sch"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Core: get_pin_location must not fabricate for a single placed instance
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSingleInstanceNoFabrication:
    def test_unplaced_unit_pin_returns_none_with_one_instance(self):
        """The exact F1 bug: only unit 1 on the sheet (len(instances)==1)."""
        path = _write_schematic([_instance("U1", 1, 50, 50)])
        loc = PinLocator()
        # Unit-1 pins locate normally.
        assert loc.get_pin_location(path, "U1", "1") is not None
        # Unit-2 pins have NO placed instance → must not be fabricated.
        assert loc.get_pin_location(path, "U1", "3") is None
        assert loc.get_pin_location(path, "U1", "4") is None

    def test_get_all_symbol_pins_drops_unplaced_unit(self):
        path = _write_schematic([_instance("U1", 1, 50, 50)])
        all_pins = PinLocator().get_all_symbol_pins(path, "U1")
        assert set(all_pins) == {"1", "2"}  # unit-2 pins 3,4 dropped

    def test_both_units_placed_all_pins_resolve(self):
        path = _write_schematic([_instance("U1", 1, 50, 50), _instance("U1", 2, 150, 50)])
        loc = PinLocator()
        assert loc.get_pin_location(path, "U1", "3") is not None
        assert set(loc.get_all_symbol_pins(path, "U1")) == {"1", "2", "3", "4"}


@pytest.mark.unit
class TestUnitPlacementIntrospection:
    def test_unit_placement_reports_unplaced(self):
        path = _write_schematic([_instance("U1", 1, 50, 50)])
        info = PinLocator().get_unit_placement(path, "U1")
        assert info["is_multi_unit"] is True
        assert info["total_units"] == 2
        assert info["placed_units"] == [1]
        assert info["unplaced_units"] == [2]

    def test_diagnose_missing_pin_by_number_and_name(self):
        path = _write_schematic([_instance("U1", 1, 50, 50)])
        loc = PinLocator()
        by_num = loc.diagnose_missing_pin(path, "U1", "3")
        assert by_num["reason"] == "unplaced_unit"
        assert by_num["pin_unit"] == 2
        # Resolving by pin NAME (VDD → pin 3) must also flag the unplaced unit.
        by_name = loc.diagnose_missing_pin(path, "U1", "VDD")
        assert by_name["reason"] == "unplaced_unit"
        assert by_name["resolved_pin"] == "3"

    def test_diagnose_truly_missing_pin(self):
        path = _write_schematic([_instance("U1", 1, 50, 50)])
        diag = PinLocator().diagnose_missing_pin(path, "U1", "999")
        assert diag["reason"] == "not_found"


# ---------------------------------------------------------------------------
# Handler: get_schematic_pin_locations marks unplaced pins
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPinLocationsHandler:
    def test_unplaced_pins_marked_not_placed(self):
        from handlers.schematic_query import handle_get_schematic_pin_locations

        path = _write_schematic([_instance("U1", 1, 50, 50)])
        res = handle_get_schematic_pin_locations(
            None, {"schematicPath": str(path), "reference": "U1"}
        )
        assert res["success"] is True
        pins = res["pins"]
        # Placed unit-1 pins carry coords.
        assert pins["1"]["placed"] is True
        assert "x" in pins["1"] and "y" in pins["1"]
        # Unplaced unit-2 pins are marked and have NO coordinates.
        assert pins["3"]["placed"] is False
        assert "x" not in pins["3"]
        assert pins["3"]["unit"] == 2
        # Summary + warning name the unplaced unit.
        assert res["units"]["unplaced"] == [2]
        assert "2" in res["warning"] or "[2]" in res["warning"]


# ---------------------------------------------------------------------------
# Handler: net-label + connect refuse a pin on an unplaced unit
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRefusePhantomPin:
    def test_add_net_label_refuses_unplaced_unit_pin(self):
        from handlers.schematic_wire._labels import handle_add_schematic_net_label

        path = _write_schematic([_instance("U1", 1, 50, 50)])
        res = handle_add_schematic_net_label(
            None,
            {"schematicPath": str(path), "netName": "VDD", "componentRef": "U1", "pinNumber": "3"},
        )
        assert res["success"] is False
        assert res["needs_unit_placement"] is True
        assert res["unit"] == 2
        # Never a phantom connection.
        assert res.get("connected_to_pin") is None
        # Message shows the exact fix.
        assert "unit=2" in res["message"]

    def test_add_net_label_succeeds_after_placing_unit(self):
        from handlers.schematic_wire._labels import handle_add_schematic_net_label

        path = _write_schematic([_instance("U1", 1, 50, 50), _instance("U1", 2, 150, 50)])
        res = handle_add_schematic_net_label(
            None,
            {"schematicPath": str(path), "netName": "VDD", "componentRef": "U1", "pinNumber": "3"},
        )
        assert res["success"] is True
        assert res["connected_to_pin"] == {"ref": "U1", "pin": "3"}

    def test_connect_to_net_refuses_unplaced_unit_pin(self):
        path = _write_schematic([_instance("U1", 1, 50, 50)])
        res = ConnectionManager.connect_to_net(path, "U1", "3", "VDD")
        assert res["success"] is False
        assert res.get("needs_unit_placement") is True
        assert "unit=2" in res["message"]


# ---------------------------------------------------------------------------
# Handler: add_schematic_component reports units + placeAllUnits (real library)
# ---------------------------------------------------------------------------

_LIB_KICAD_SYM = """\
(kicad_symbol_lib (version 20211014) (generator test)
  (symbol "DualBank" (pin_names (offset 0.127)) (in_bom yes) (on_board yes)
    (property "Reference" "U" (at 0 5.08 0) (effects (font (size 1.27 1.27))))
    (property "Value" "DualBank" (at 0 -5.08 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (symbol "DualBank_1_1"
      (pin passive line (at -7.62 2.54 0) (length 2.54)
        (name "PA0" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at -7.62 0 0) (length 2.54)
        (name "PA1" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27)))))
    )
    (symbol "DualBank_2_1"
      (pin passive line (at -7.62 2.54 0) (length 2.54)
        (name "VDD" (effects (font (size 1.27 1.27))))
        (number "3" (effects (font (size 1.27 1.27)))))
      (pin passive line (at -7.62 -2.54 0) (length 2.54)
        (name "VSS" (effects (font (size 1.27 1.27))))
        (number "4" (effects (font (size 1.27 1.27)))))
    )
  )
)
"""

_EMPTY_SCH = Path(__file__).resolve().parent.parent / "python" / "templates" / "empty.kicad_sch"


def _project(tmp_path):
    """A tmp project dir with the 2-unit library + a project sym-lib-table."""
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
class TestAddComponentMultiUnit:
    def test_single_unit_placement_reports_units_and_next(self, tmp_path):
        from handlers.schematic_component._placement import handle_add_schematic_component

        sch = _project(tmp_path)
        res = handle_add_schematic_component(
            None,
            {
                "schematicPath": str(sch),
                "component": {
                    "library": "TestLib",
                    "type": "DualBank",
                    "reference": "U1",
                    "value": "DualBank",
                    "x": 100,
                    "y": 100,
                },
            },
        )
        assert res["success"] is True
        assert res["units"] == {"total": 2, "placed": [1], "unplaced": [2]}
        assert "warning" in res and "unit" in res["warning"].lower()
        # next hint uses the real schema (symbol=, unit=N) — not library=/componentName=.
        assert "unit=N" in res["next"] and "placeAllUnits" in res["next"]

    def test_place_all_units_places_every_unit(self, tmp_path):
        from handlers.schematic_component._placement import handle_add_schematic_component

        sch = _project(tmp_path)
        res = handle_add_schematic_component(
            None,
            {
                "schematicPath": str(sch),
                "component": {
                    "library": "TestLib",
                    "type": "DualBank",
                    "reference": "U1",
                    "value": "DualBank",
                    "x": 100,
                    "y": 100,
                    "placeAllUnits": True,
                },
            },
        )
        assert res["success"] is True
        assert res["units"]["placed"] == [1, 2]
        assert res["units"]["unplaced"] == []
        assert "warning" not in res
        # Each unit's position is reported and they don't overlap vertically.
        pos = res["unitPositions"]
        assert set(pos) == {"1", "2"}
        assert pos["2"]["y"] > pos["1"]["y"]
        # Pins on the (formerly unplaced) unit 2 now locate + label successfully.
        loc = PinLocator().get_pin_location(sch, "U1", "3")
        assert loc is not None
