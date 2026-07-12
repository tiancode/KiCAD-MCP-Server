"""
Regression for add_schematic_net_label deriving label orientation from the pin
it lands on.

The bug: handle_add_schematic_net_label always defaulted orientation to 0, so a
label snapped onto a LEFT-side pin (outward angle 180) was written with angle 0 /
justify left — its text ran rightward INTO the symbol body and the anchor sat on
the wrong end. Fix: when the caller omits `orientation` and the final coordinates
land on a known pin, derive the orientation from the pin's OUTWARD angle
(PinLocator.get_pin_angle rounded to 0/90/180/270), exactly like connect_to_net.
Explicit values — including 0 — are honored verbatim; free-floating labels stay 0.

These tests build a real .kicad_sch (an IC with horizontal left/right pins and a
resistor with vertical top/bottom pins), call the handler, and assert both the
handler response fields (orientation / orientation_source) and the written file
(the (at ... angle) and (justify ...) tokens).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest
import sexpdata
from sexpdata import Symbol

# ---------------------------------------------------------------------------
# Module loading — bypass pcbnew (skip is real and works fine)
# ---------------------------------------------------------------------------
_PYTHON_DIR = os.path.join(os.path.dirname(__file__), "..", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

sys.modules.setdefault("pcbnew", MagicMock())

from commands.pin_locator import PinLocator  # noqa: E402
from handlers.schematic_wire._labels import handle_add_schematic_net_label  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures: an IC (horizontal pins) and a resistor (vertical pins)
# ---------------------------------------------------------------------------
SYMBOL_X = 100.0
SYMBOL_Y = 100.0

# A 4-pin IC: pins 1/2 on the LEFT (lib angle 0 → outward 180), pins 3/4 on the
# RIGHT (lib angle 180 → outward 0). Body rectangle centred on the origin.
_IC_LIB = (
    '(symbol "T:IC4" (pin_numbers hide) (pin_names (offset 0))\n'
    '  (symbol "IC4_0_1" (rectangle (start -5.08 5.08) (end 5.08 -5.08)))\n'
    '  (symbol "IC4_1_1"\n'
    '    (pin input line (at -7.62 2.54 0) (length 2.54) (name "A") (number "1"))\n'
    '    (pin input line (at -7.62 -2.54 0) (length 2.54) (name "B") (number "2"))\n'
    '    (pin output line (at 7.62 2.54 180) (length 2.54) (name "C") (number "3"))\n'
    '    (pin output line (at 7.62 -2.54 180) (length 2.54) (name "D") (number "4"))))'
)

# Device:R with vertical pins: pin 1 at the TOP (outward 90), pin 2 at the
# BOTTOM (outward 270).
_R_LIB = (
    '(symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))\n'
    '  (symbol "R_1_1"\n'
    '    (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))\n'
    '    (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))))'
)


def _write_sch(tmp_path: Path, lib_id: str, lib: str, rotation: float = 0) -> Path:
    text = (
        '(kicad_sch (version 20250114) (generator "test")\n'
        '  (uuid "00000000-0000-0000-0000-0000000000aa")\n'
        '  (paper "A4")\n'
        "  (lib_symbols\n"
        f"    {lib}\n"
        "  )\n"
        f'  (symbol (lib_id "{lib_id}") (at {SYMBOL_X} {SYMBOL_Y} {rotation}) (unit 1)\n'
        "    (in_bom yes) (on_board yes) (dnp no)\n"
        '    (uuid "11111111-1111-1111-1111-1111111111aa")\n'
        f'    (property "Reference" "U1" (at {SYMBOL_X} {SYMBOL_Y} 0))\n'
        f'    (property "Value" "v" (at {SYMBOL_X} {SYMBOL_Y} 0))\n'
        "    (instances\n"
        '      (project "test"\n'
        '        (path "/00000000-0000-0000-0000-0000000000aa" (reference "U1") (unit 1)))))\n'
        '  (sheet_instances (path "/" (page "1")))\n'
        ")\n"
    )
    p = tmp_path / f"{lib_id.replace(':', '_')}_rot{int(rotation)}.kicad_sch"
    p.write_text(text)
    return p


def _clear_pin_caches() -> None:
    # The PinLocator caches are process-wide and keyed by path+mtime; freshly
    # written files can collide on a fast filesystem, so clear them per file.
    PinLocator._SCHEMATIC_CACHE.clear()
    PinLocator._SEXP_CACHE.clear()
    PinLocator._PINDEF_CACHE.clear()


def _add(sch: Path, **params: Any) -> dict:
    return handle_add_schematic_net_label(MagicMock(), {"schematicPath": str(sch), **params})


def _find_label(path: Path, element: str, net: str) -> Optional[list]:
    data = sexpdata.loads(path.read_text())
    for item in data:
        if (
            isinstance(item, list)
            and len(item) >= 2
            and isinstance(item[0], Symbol)
            and str(item[0]) == element
            and item[1] == net
        ):
            return item
    return None


def _label_angle(label_sexp: list) -> Optional[float]:
    for part in label_sexp[2:]:
        if isinstance(part, list) and part and str(part[0]) == "at":
            return float(part[3]) if len(part) > 3 else 0.0
    return None


def _label_justify(label_sexp: list) -> Optional[List[str]]:
    for part in label_sexp[2:]:
        if isinstance(part, list) and part and str(part[0]) == "effects":
            for eff in part[1:]:
                if isinstance(eff, list) and eff and str(eff[0]) == "justify":
                    return [str(t) for t in eff[1:] if isinstance(t, Symbol)]
    return None


# ---------------------------------------------------------------------------
# Explicit componentRef+pin: orientation derived from the pin's outward angle
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_left_pin_derives_180_justify_right(tmp_path):
    sch = _write_sch(tmp_path, "T:IC4", _IC_LIB)
    _clear_pin_caches()
    res = _add(sch, netName="L", componentRef="U1", pinNumber="1")
    assert res["success"]
    assert res["orientation"] == 180
    assert res["orientation_source"] == "pin_outward"

    label = _find_label(sch, "label", "L")
    assert label is not None
    assert _label_angle(label) == 180
    assert _label_justify(label) == ["right", "bottom"]


@pytest.mark.unit
def test_right_pin_derives_0_justify_left(tmp_path):
    sch = _write_sch(tmp_path, "T:IC4", _IC_LIB)
    _clear_pin_caches()
    res = _add(sch, netName="R", componentRef="U1", pinNumber="3")
    assert res["success"]
    assert res["orientation"] == 0
    assert res["orientation_source"] == "pin_outward"

    label = _find_label(sch, "label", "R")
    assert label is not None
    assert _label_angle(label) == 0
    assert _label_justify(label) == ["left", "bottom"]


@pytest.mark.unit
def test_top_and_bottom_pins_derive_90_and_270(tmp_path):
    # Assert against outward semantics — the same orientation connect_to_net
    # would produce for these pins (top → up/90, bottom → down/270).
    for pin, expected, justify in (("1", 90, "left"), ("2", 270, "right")):
        sch = _write_sch(tmp_path, "Device:R", _R_LIB)
        _clear_pin_caches()

        # Sanity: this is exactly what connect_to_net's formula yields.
        angle = PinLocator().get_pin_angle(sch, "U1", pin)
        assert int(round(float(angle) / 90.0) * 90) % 360 == expected

        res = _add(sch, netName=f"V{pin}", componentRef="U1", pinNumber=pin)
        assert res["success"]
        assert res["orientation"] == expected
        assert res["orientation_source"] == "pin_outward"

        label = _find_label(sch, "label", f"V{pin}")
        assert label is not None
        assert _label_angle(label) == expected
        assert _label_justify(label) == [justify, "bottom"]


# ---------------------------------------------------------------------------
# Raw-position placement modes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_auto_snap_raw_position_derives_orientation(tmp_path):
    # Raw position 0.02 mm off the LEFT pin 1 tip (92.38, 97.46): auto-snap moves
    # it onto the pin, and the orientation is derived from that pin.
    sch = _write_sch(tmp_path, "T:IC4", _IC_LIB)
    _clear_pin_caches()
    res = _add(sch, netName="SNAP", position=[92.40, 97.46])
    assert res["success"]
    assert res["snapped_to_pin"]["pin"] == "1"
    assert res["orientation"] == 180
    assert res["orientation_source"] == "pin_outward"

    label = _find_label(sch, "label", "SNAP")
    assert _label_angle(label) == 180
    assert _label_justify(label) == ["right", "bottom"]


@pytest.mark.unit
def test_exact_hit_raw_position_derives_orientation(tmp_path):
    # Raw position exactly on the LEFT pin 1 tip: no snap movement occurs
    # (snapped_to_pin stays None), but the coordinates still land on the pin and
    # must get the derived orientation.
    sch = _write_sch(tmp_path, "T:IC4", _IC_LIB)
    _clear_pin_caches()
    res = _add(sch, netName="EXACT", position=[92.38, 97.46])
    assert res["success"]
    assert res.get("snapped_to_pin") is None
    assert res["connected_to_pin"] == {"ref": "U1", "pin": "1"}
    assert res["orientation"] == 180
    assert res["orientation_source"] == "pin_outward"

    label = _find_label(sch, "label", "EXACT")
    assert _label_angle(label) == 180
    assert _label_justify(label) == ["right", "bottom"]


@pytest.mark.unit
def test_free_floating_label_stays_zero(tmp_path):
    # Raw position nowhere near a pin, snapping disabled: no pin → default 0.
    sch = _write_sch(tmp_path, "T:IC4", _IC_LIB)
    _clear_pin_caches()
    res = _add(sch, netName="FLOAT", position=[50.0, 50.0], snapTolerance=0)
    assert res["success"]
    assert res["connected_to_pin"] is None
    assert res["orientation"] == 0
    assert res["orientation_source"] == "default"

    label = _find_label(sch, "label", "FLOAT")
    assert _label_angle(label) == 0
    assert _label_justify(label) == ["left", "bottom"]


# ---------------------------------------------------------------------------
# Explicit orientation is always honored verbatim (0 included)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_explicit_orientation_zero_on_left_pin_stays_zero(tmp_path):
    sch = _write_sch(tmp_path, "T:IC4", _IC_LIB)
    _clear_pin_caches()
    res = _add(sch, netName="Z", componentRef="U1", pinNumber="1", orientation=0)
    assert res["success"]
    assert res["orientation"] == 0
    assert res["orientation_source"] == "explicit"

    label = _find_label(sch, "label", "Z")
    assert _label_angle(label) == 0
    assert _label_justify(label) == ["left", "bottom"]


@pytest.mark.unit
def test_explicit_orientation_90_is_honored(tmp_path):
    sch = _write_sch(tmp_path, "T:IC4", _IC_LIB)
    _clear_pin_caches()
    res = _add(sch, netName="N", componentRef="U1", pinNumber="1", orientation=90)
    assert res["success"]
    assert res["orientation"] == 90
    assert res["orientation_source"] == "explicit"

    label = _find_label(sch, "label", "N")
    assert _label_angle(label) == 90


# ---------------------------------------------------------------------------
# The global_label / hierarchical_label write paths carry the same justify
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_global_label_left_pin_derives_180_justify_right(tmp_path):
    sch = _write_sch(tmp_path, "T:IC4", _IC_LIB)
    _clear_pin_caches()
    res = _add(sch, netName="GL", componentRef="U1", pinNumber="1", labelType="global_label")
    assert res["success"]
    assert res["orientation"] == 180
    assert res["orientation_source"] == "pin_outward"

    label = _find_label(sch, "global_label", "GL")
    assert label is not None
    assert _label_angle(label) == 180
    assert _label_justify(label) == ["right", "bottom"]


@pytest.mark.unit
def test_hierarchical_label_bottom_pin_derives_270_justify_right(tmp_path):
    sch = _write_sch(tmp_path, "Device:R", _R_LIB)
    _clear_pin_caches()
    res = _add(sch, netName="HL", componentRef="U1", pinNumber="2", labelType="hierarchical_label")
    assert res["success"]
    assert res["orientation"] == 270
    assert res["orientation_source"] == "pin_outward"

    label = _find_label(sch, "hierarchical_label", "HL")
    assert label is not None
    assert _label_angle(label) == 270
    assert _label_justify(label) == ["right", "bottom"]
