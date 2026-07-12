"""F4 regression tests — unit-aware symbol bounding boxes in overlap detection.

Background / bug
----------------
``find_overlapping_elements`` computed each symbol's bounding box from ALL of
its library pins (and body graphics). A multi-unit part draws every unit's pins
at the SAME library origin, so a unit-1 instance's box was inflated to also
cover unit 2's pins/body — producing phantom "symbol_overlap" reports between
the MCU and parts tens of mm away (the E2E tester saw ``U1↔C5 67mm`` /
``U1↔J3 77mm``), while the single genuine overlap (two adjacent caps) was buried.

The fix
-------
``_parse_symbols`` records each placed instance's ``(unit N)``;
``_compute_pin_positions_direct`` / ``_compute_symbol_bbox_direct`` include only
unit 0 (common) plus THAT instance's unit — for both pins and body graphics
(``graphics_by_unit``). A genuine body intersection of any size is still
reported (strict-inequality AABB, no shrink margin).
"""

import sys
import tempfile
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from commands.schematic_analysis._geometry import (  # noqa: E402
    _compute_pin_positions_direct,
    _compute_symbol_bbox_direct,
)
from commands.schematic_analysis._parsing import (  # noqa: E402
    _extract_lib_symbols,
    _load_sexp,
    _parse_symbols,
)
from commands.schematic_analysis._queries import find_overlapping_elements  # noqa: E402

# ---------------------------------------------------------------------------
# A 2-unit part: unit 1 body near origin, unit 2 body offset far in +x.
# Both units share the library origin, so a naive all-pins bbox for the unit-1
# instance would reach across to unit 2's coordinates.
# ---------------------------------------------------------------------------


def _mcu_lib_def():
    return """    (symbol "Sim:BigMcu" (pin_names (offset 0.127)) (in_bom yes) (on_board yes)
      (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "BigMcu" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "BigMcu_1_1"
        (rectangle (start -15 30) (end 15 -30)
          (stroke (width 0) (type default)) (fill (type background)))
        (pin bidirectional line (at -20 20 0) (length 5)
          (name "PA0" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin bidirectional line (at -20 -20 0) (length 5)
          (name "PA1" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27)))))
      )
      (symbol "BigMcu_2_1"
        (rectangle (start 40 10) (end 60 -10)
          (stroke (width 0) (type default)) (fill (type background)))
        (pin power_in line (at 35 5 0) (length 5)
          (name "VDD" (effects (font (size 1.27 1.27))))
          (number "3" (effects (font (size 1.27 1.27)))))
        (pin power_in line (at 35 -5 0) (length 5)
          (name "VSS" (effects (font (size 1.27 1.27))))
          (number "4" (effects (font (size 1.27 1.27)))))
      )
    )"""


def _cap_lib_def():
    return """    (symbol "Device:C" (pin_names (offset 0.254)) (in_bom yes) (on_board yes)
      (property "Reference" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "C_0_1"
        (polyline (pts (xy -2.032 0.762) (xy 2.032 0.762))
          (stroke (width 0.508) (type default)) (fill (type none)))
        (polyline (pts (xy -2.032 -0.762) (xy 2.032 -0.762))
          (stroke (width 0.508) (type default)) (fill (type none))))
      (symbol "C_1_1"
        (pin passive line (at 0 3.81 270) (length 2.794)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 2.794)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27)))))
      )
    )"""


def _mcu_instance(ref, unit, x, y):
    return f"""  (symbol (lib_id "Sim:BigMcu") (at {x} {y} 0) (unit {unit})
    (uuid "{uuid.uuid4()}")
    (property "Reference" "{ref}" (at {x} {y} 0) (effects (font (size 1.27 1.27))))
    (property "Value" "BigMcu" (at {x} {y} 0) (effects (font (size 1.27 1.27))))
  )"""


def _cap_instance(ref, x, y):
    return f"""  (symbol (lib_id "Device:C") (at {x} {y} 0) (unit 1)
    (uuid "{uuid.uuid4()}")
    (property "Reference" "{ref}" (at {x} {y} 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at {x} {y} 0) (effects (font (size 1.27 1.27))))
  )"""


def _write(instances):
    content = f"""(kicad_sch (version 20250114) (generator "test")
  (uuid {uuid.uuid4()})
  (paper "A4")
  (lib_symbols
{_mcu_lib_def()}
{_cap_lib_def()}
  )
{chr(10).join(instances)}
  (sheet_instances (path "/" (page "1")))
)
"""
    p = Path(tempfile.mkdtemp()) / "layout.kicad_sch"
    p.write_text(content, encoding="utf-8")
    return p


def _pairs(result):
    return {
        frozenset((e["element1"]["reference"], e["element2"]["reference"]))
        for e in result["overlappingSymbols"]
    }


# ---------------------------------------------------------------------------
# Unit-level: pins/graphics filtered to the instance's unit
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUnitFilteredBbox:
    def _lib(self):
        p = _write([_mcu_instance("U1", 1, 100, 100)])
        return _extract_lib_symbols(_load_sexp(p))["Sim:BigMcu"]

    def test_pin_positions_exclude_other_unit(self):
        lib = self._lib()
        sym1 = {"x": 100, "y": 100, "rotation": 0, "unit": 1}
        pos1 = _compute_pin_positions_direct(sym1, lib["pins"])
        assert set(pos1) == {"1", "2"}  # unit-2 pins (3,4) excluded
        sym2 = {"x": 250, "y": 100, "rotation": 0, "unit": 2}
        pos2 = _compute_pin_positions_direct(sym2, lib["pins"])
        assert set(pos2) == {"3", "4"}

    def test_bbox_uses_only_own_unit_body(self):
        lib = self._lib()
        sym1 = {"x": 100, "y": 100, "rotation": 0, "unit": 1}
        bbox = _compute_symbol_bbox_direct(
            sym1, lib["pins"], graphics_by_unit=lib["graphics_by_unit"]
        )
        # Unit-1 body/pins live near x=100; must NOT stretch to unit-2's
        # library x (~40..60 → world ~140..160).
        assert bbox is not None
        assert bbox[2] < 130, f"unit-1 bbox leaked into unit-2 space: {bbox}"


# ---------------------------------------------------------------------------
# End-to-end: no phantom overlaps, real overlaps still caught
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFindOverlapsUnitAware:
    def test_multiunit_ab_pair_no_phantom_overlap_with_far_part(self):
        # Real E2E shape: U1 placed as A/B pair; a cap sits between the two units
        # where the phantom (all-unit) bbox used to reach.
        path = _write(
            [
                _mcu_instance("U1", 1, 100, 100),
                _mcu_instance("U1", 2, 250, 100),
                _cap_instance("C1", 150, 105),
            ]
        )
        result = find_overlapping_elements(path, tolerance=0.5)
        assert frozenset(("U1", "C1")) not in _pairs(result)
        assert result["overlappingSymbols"] == []

    def test_single_unit_placed_no_phantom_overlap(self):
        # Only unit 1 on the sheet; a cap where unit-2's phantom pins would land.
        path = _write([_mcu_instance("U1", 1, 100, 100), _cap_instance("C1", 150, 100)])
        result = find_overlapping_elements(path, tolerance=0.5)
        assert _pairs(result) == set()

    def test_genuinely_overlapping_caps_still_detected(self):
        # Two caps stacked ~1mm apart — bodies overlap, must be reported.
        path = _write([_cap_instance("C1", 100, 100), _cap_instance("C2", 100, 101)])
        result = find_overlapping_elements(path, tolerance=0.5)
        assert frozenset(("C1", "C2")) in _pairs(result)

    def test_parse_symbols_records_unit(self):
        path = _write([_mcu_instance("U1", 1, 100, 100), _mcu_instance("U1", 2, 250, 100)])
        syms = {(s["reference"], s["unit"]) for s in _parse_symbols(_load_sexp(path))}
        assert ("U1", 1) in syms and ("U1", 2) in syms
