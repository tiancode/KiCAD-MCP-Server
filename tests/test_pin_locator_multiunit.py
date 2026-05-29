"""
Regression tests for multi-unit symbol pin location.

Background
----------
A multi-unit part (op-amp, gate array, …) is stored in the schematic as one
``(symbol …)`` instance *per unit* — all sharing the reference (e.g. ``U41``)
but each with its own ``(at …)`` and ``(unit N)``. Inside ``lib_symbols`` the
pins are split across ``<base>_<unit>_<style>`` sub-symbols (``DualOp_1_1``,
``DualOp_2_1`` …), and crucially every unit's pins are drawn relative to the
*same* local origin — only the placed instance's position distinguishes them.

The bug
-------
``PinLocator.parse_symbol_definition`` flattened every unit's pins into one
number-keyed dict with no unit tag, and ``WireDragger.find_symbol`` returned
the *first* instance matching the reference (always unit A). So a pin on unit
B/C/D got unit A's transform and collapsed onto unit A's coordinates. When a
net label then snapped "to pin N", it landed on the wrong unit and reported the
wrong ``connected_to_pin`` — silently shorting two channels.

The fix
-------
Pins now carry ``unit`` (from the sub-symbol naming convention), and
``PinLocator`` transforms each pin by *its own unit's* placed instance via
``WireDragger.find_symbol_instances``.
"""

import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from sexpdata import Symbol

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from commands.pin_locator import PinLocator  # noqa: E402
from commands.wire_dragger import WireDragger  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture: a 3-unit op-amp-style symbol placed once per unit at distinct spots
# ---------------------------------------------------------------------------

# Library pin layout (Y-up, relative to the shared symbol origin), grouped by
# unit. World position with rotation 0 / no mirror is (sym_x + px, sym_y - py).
_UNIT_PINS = {
    1: [("1", 7.62, 0.0, 180), ("2", -7.62, -2.54, 0), ("3", -7.62, 2.54, 0)],
    2: [("5", -7.62, 2.54, 0), ("6", -7.62, -2.54, 0), ("7", 7.62, 0.0, 180)],
    3: [("4", -2.54, -7.62, 90), ("8", -2.54, 7.62, 270)],
}


def _pin_text(number, px, py, angle):
    return (
        f"        (pin passive line (at {px} {py} {angle}) (length 2.54)\n"
        f'          (name "p{number}" (effects (font (size 1.27 1.27))))\n'
        f'          (number "{number}" (effects (font (size 1.27 1.27))))\n'
        f"        )"
    )


def _unit_subsymbol(unit):
    pins = "\n".join(_pin_text(n, px, py, a) for n, px, py, a in _UNIT_PINS[unit])
    return f'      (symbol "DualOp_{unit}_1"\n{pins}\n      )'


def _lib_symbol_def():
    subs = "\n".join(_unit_subsymbol(u) for u in (1, 2, 3))
    return f"""    (symbol "Sim:DualOp" (pin_names (offset 0.127)) (in_bom yes) (on_board yes)
      (property "Reference" "U" (at 0 5.08 0) (effects (font (size 1.27 1.27))))
      (property "Value" "DualOp" (at 0 -5.08 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
      (property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
{subs}
    )"""


def _instance(ref, unit, x, y, rotation=0):
    u = str(uuid.uuid4())
    pin_lines = "\n".join(f'    (pin "{n}" (uuid "{uuid.uuid4()}"))' for n, *_ in _UNIT_PINS[unit])
    return f"""  (symbol (lib_id "Sim:DualOp") (at {x} {y} {rotation}) (unit {unit})
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "{u}")
    (property "Reference" "{ref}" (at {x} {y - 7.62} 0) (effects (font (size 1.27 1.27))))
    (property "Value" "DualOp" (at {x} {y + 7.62} 0) (effects (font (size 1.27 1.27))))
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
    """Build a complete .kicad_sch with the multi-unit lib def + given instances."""
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
    path = Path(tempfile.mkdtemp()) / "multiunit.kicad_sch"
    path.write_text(content, encoding="utf-8")
    return path


def _world(sym_x, sym_y, px, py):
    """Expected world coord for rotation 0 / no mirror: (x+px, y-py)."""
    return (sym_x + px, sym_y - py)


# ---------------------------------------------------------------------------
# Unit-level: pin/unit tagging and instance enumeration (no file I/O)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUnitTagging:
    def test_parse_tags_each_pin_with_its_unit(self) -> None:
        symbol_def = [
            Symbol("symbol"),
            "Sim:DualOp",
            [
                Symbol("symbol"),
                "DualOp_1_1",
                [
                    Symbol("pin"),
                    Symbol("passive"),
                    Symbol("line"),
                    [Symbol("at"), 7.62, 0, 180],
                    [Symbol("length"), 2.54],
                    [Symbol("number"), '"1"'],
                ],
            ],
            [
                Symbol("symbol"),
                "DualOp_2_1",
                [
                    Symbol("pin"),
                    Symbol("passive"),
                    Symbol("line"),
                    [Symbol("at"), -7.62, 2.54, 0],
                    [Symbol("length"), 2.54],
                    [Symbol("number"), '"5"'],
                ],
            ],
        ]

        pins = PinLocator.parse_symbol_definition(symbol_def)

        assert pins["1"]["unit"] == 1
        assert pins["5"]["unit"] == 2

    def test_base_name_with_digits_does_not_confuse_unit(self) -> None:
        # "74LS00_2_1" → unit 2, not unit 0 from the "74LS00".
        symbol_def = [
            Symbol("symbol"),
            "Logic:74LS00",
            [
                Symbol("symbol"),
                "74LS00_2_1",
                [
                    Symbol("pin"),
                    Symbol("input"),
                    Symbol("line"),
                    [Symbol("at"), 0, 0, 0],
                    [Symbol("length"), 2.54],
                    [Symbol("number"), '"4"'],
                ],
            ],
        ]
        pins = PinLocator.parse_symbol_definition(symbol_def)
        assert pins["4"]["unit"] == 2

    def test_find_symbol_instances_returns_every_unit(self) -> None:
        path = _write_schematic(
            [
                _instance("U41", 1, 50, 50),
                _instance("U41", 2, 150, 50),
                _instance("U41", 3, 100, 100),
            ]
        )
        import sexpdata

        data = sexpdata.loads(path.read_text(encoding="utf-8"))

        instances = WireDragger.find_symbol_instances(data, "U41")
        units = sorted(inst[7] for inst in instances)
        assert units == [1, 2, 3]

        # find_symbol stays back-compatible: first instance, 7-tuple.
        first = WireDragger.find_symbol(data, "U41")
        assert len(first) == 7
        assert first[1:3] == (50.0, 50.0)


# ---------------------------------------------------------------------------
# End-to-end: get_pin_location routes each pin to its own unit's instance
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMultiUnitPinLocation:
    def _all_units_path(self):
        return _write_schematic(
            [
                _instance("U41", 1, 50, 50),
                _instance("U41", 2, 150, 50),
                _instance("U41", 3, 100, 100),
            ]
        )

    def test_unit2_pin_lands_on_unit2_not_unit1(self) -> None:
        """The core bug: pin 5 (unit 2, placed at x=150) must NOT collapse onto
        unit 1's position (x=50)."""
        path = self._all_units_path()
        loc = PinLocator().get_pin_location(path, "U41", "5")
        assert loc is not None
        expected = _world(150, 50, -7.62, 2.54)  # (142.38, 47.46)
        assert loc[0] == pytest.approx(expected[0], abs=1e-3)
        assert loc[1] == pytest.approx(expected[1], abs=1e-3)
        # Guard against the regression: the buggy value was unit-1-relative.
        buggy = _world(50, 50, -7.62, 2.54)  # (42.38, 47.46)
        assert abs(loc[0] - buggy[0]) > 50

    def test_every_pin_resolves_to_its_own_unit(self) -> None:
        path = self._all_units_path()
        positions = {1: (50, 50), 2: (150, 50), 3: (100, 100)}
        locator = PinLocator()
        for unit, pins in _UNIT_PINS.items():
            sx, sy = positions[unit]
            for number, px, py, _angle in pins:
                loc = locator.get_pin_location(path, "U41", number)
                assert loc is not None, f"pin {number} unresolved"
                ex, ey = _world(sx, sy, px, py)
                assert loc[0] == pytest.approx(ex, abs=1e-3), f"pin {number} x"
                assert loc[1] == pytest.approx(ey, abs=1e-3), f"pin {number} y"

    def test_get_all_symbol_pins_spans_units(self) -> None:
        path = self._all_units_path()
        all_pins = PinLocator().get_all_symbol_pins(path, "U41")
        # All 8 pins present, each at its own unit's location.
        assert set(all_pins) == {"1", "2", "3", "4", "5", "6", "7", "8"}
        assert all_pins["1"][0] == pytest.approx(57.62, abs=1e-3)  # unit 1
        assert all_pins["7"][0] == pytest.approx(157.62, abs=1e-3)  # unit 2
        assert all_pins["8"][1] == pytest.approx(92.38, abs=1e-3)  # unit 3

    def test_pin_on_unplaced_unit_returns_none(self) -> None:
        """When a multi-unit part has only some units on the sheet, a pin that
        belongs to a missing unit must NOT be mislocated onto a placed unit."""
        path = _write_schematic(
            [
                _instance("U41", 1, 50, 50),
                _instance("U41", 2, 150, 50),
            ]
        )  # unit 3 (pins 4, 8) not placed
        locator = PinLocator()
        assert locator.get_pin_location(path, "U41", "4") is None
        assert locator.get_pin_location(path, "U41", "8") is None
        # Placed units still resolve.
        assert locator.get_pin_location(path, "U41", "1") is not None
        assert locator.get_pin_location(path, "U41", "5") is not None
        # get_all_symbol_pins drops the unplaced unit's pins.
        all_pins = locator.get_all_symbol_pins(path, "U41")
        assert set(all_pins) == {"1", "2", "3", "5", "6", "7"}


# ---------------------------------------------------------------------------
# Regression: single-unit parts are unchanged
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSingleUnitUnchanged:
    def test_single_unit_resistor_unchanged(self) -> None:
        template = (
            Path(__file__).resolve().parent.parent / "python" / "templates" / "empty.kicad_sch"
        )
        content = template.read_text(encoding="utf-8")
        r = """  (symbol (lib_id "Device:R") (at 100 100 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "%s")
    (property "Reference" "R1" (at 102 100 90) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 100 90) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "%s"))
    (pin "2" (uuid "%s"))
    (instances (project "t" (path "/" (reference "R1") (unit 1))))
  )""" % (uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
        idx = content.rfind(")")
        content = content[:idx] + "\n" + r + "\n)"
        path = Path(tempfile.mkdtemp()) / "single.kicad_sch"
        path.write_text(content, encoding="utf-8")

        locator = PinLocator()
        p1 = locator.get_pin_location(path, "R1", "1")
        p2 = locator.get_pin_location(path, "R1", "2")
        # Device:R pins are at lib (0, ±3.81); world = (100, 100 ∓ 3.81).
        assert p1 == pytest.approx([100.0, 96.19], abs=1e-3)
        assert p2 == pytest.approx([100.0, 103.81], abs=1e-3)


# ---------------------------------------------------------------------------
# Handler exposes unit per pin
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerExposesUnit:
    def test_handler_reports_unit_and_correct_coords(self) -> None:
        from handlers.schematic_query import handle_get_schematic_pin_locations

        path = _write_schematic(
            [
                _instance("U41", 1, 50, 50),
                _instance("U41", 2, 150, 50),
                _instance("U41", 3, 100, 100),
            ]
        )
        res = handle_get_schematic_pin_locations(
            None, {"schematicPath": str(path), "reference": "U41"}
        )
        assert res["success"] is True
        pins = res["pins"]
        assert pins["1"]["unit"] == 1
        assert pins["5"]["unit"] == 2
        assert pins["4"]["unit"] == 3
        # Pin 5 reports unit-2 coords, not unit-1.
        assert pins["5"]["x"] == pytest.approx(142.38, abs=1e-3)
