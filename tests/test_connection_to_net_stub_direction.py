"""
End-to-end regression for ConnectionManager.connect_to_net stub direction.

The bug: connect_to_net drew a 2.54 mm wire stub + net label starting at a pin
endpoint and extending in what should be the OUTWARD direction (away from the
symbol body). For horizontal pins (left-side lib angle 0, right-side lib angle
180) the stub instead extended INTO the symbol body, because get_pin_angle
returned the inward angle for pins that end up horizontal in world space.

These tests build a real .kicad_sch (a resistor with vertical pins and an IC with
horizontal pins), call connect_to_net on every pin under rotations 0/90/180/270
and mirroring, and assert the stub end (and therefore the net label placed there)
lands on the FAR side of the pin tip from the symbol body — i.e. moving away from
the symbol placement origin. It also checks the label orientation the handler
derives points the text outward.
"""

from __future__ import annotations

import importlib.util
import math
import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading — bypass pcbnew (skip is real and works fine)
# ---------------------------------------------------------------------------
_PYTHON_DIR = os.path.join(os.path.dirname(__file__), "..", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

sys.modules.setdefault("pcbnew", MagicMock())

from commands.connection_schematic import ConnectionManager  # noqa: E402
from commands.pin_locator import PinLocator  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures: a resistor (vertical pins) and an IC (horizontal pins)
# ---------------------------------------------------------------------------
SYMBOL_X = 100.0
SYMBOL_Y = 100.0

# Device:R body is centred on the placement origin; both pins are vertical.
_R_LIB = (
    '(symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))\n'
    '  (symbol "R_1_1"\n'
    '    (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))\n'
    '    (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))))'
)

# A 4-pin IC: pins 1/2 on the LEFT (lib angle 0), pins 3/4 on the RIGHT (angle
# 180). Body rectangle centred on the origin, so "outward" == away from origin.
_IC_LIB = (
    '(symbol "T:IC4" (pin_numbers hide) (pin_names (offset 0))\n'
    '  (symbol "IC4_0_1" (rectangle (start -5.08 5.08) (end 5.08 -5.08)))\n'
    '  (symbol "IC4_1_1"\n'
    '    (pin input line (at -7.62 2.54 0) (length 2.54) (name "A") (number "1"))\n'
    '    (pin input line (at -7.62 -2.54 0) (length 2.54) (name "B") (number "2"))\n'
    '    (pin output line (at 7.62 2.54 180) (length 2.54) (name "C") (number "3"))\n'
    '    (pin output line (at 7.62 -2.54 180) (length 2.54) (name "D") (number "4"))))'
)


def _write_sch(tmp_path: Path, lib_id: str, lib: str, rotation: float, mirror: str | None) -> Path:
    # The placed symbol needs (unit 1), a uuid and an (instances ...) block or
    # kicad-skip's Schematic() loader (used by get_pin_location) raises on the
    # missing unit — get_pin_angle reads via sexpdata and doesn't require them,
    # but connect_to_net locates the pin through kicad-skip.
    mirror_line = {"x": "(mirror x)", "y": "(mirror y)"}.get(mirror or "", "")
    text = textwrap.dedent(f"""\
        (kicad_sch (version 20250114) (generator "test")
          (uuid "00000000-0000-0000-0000-0000000000aa")
          (paper "A4")
          (lib_symbols
            {lib}
          )
          (symbol (lib_id "{lib_id}") (at {SYMBOL_X} {SYMBOL_Y} {rotation}) (unit 1)
            {mirror_line}
            (in_bom yes) (on_board yes) (dnp no)
            (uuid "11111111-1111-1111-1111-1111111111aa")
            (property "Reference" "U1" (at {SYMBOL_X} {SYMBOL_Y} 0))
            (property "Value" "v" (at {SYMBOL_X} {SYMBOL_Y} 0))
            (instances
              (project "test"
                (path "/00000000-0000-0000-0000-0000000000aa" (reference "U1") (unit 1)))))
        )
        """)
    p = tmp_path / f"{lib_id.replace(':', '_')}_rot{int(rotation)}_{mirror or 'none'}.kicad_sch"
    p.write_text(text)
    return p


def _dist2(x: float, y: float) -> float:
    return (x - SYMBOL_X) ** 2 + (y - SYMBOL_Y) ** 2


ROTATIONS = [0, 90, 180, 270]
MIRRORS = [None, "x", "y"]


@pytest.mark.unit
@pytest.mark.parametrize("rotation", ROTATIONS)
@pytest.mark.parametrize("mirror", MIRRORS)
@pytest.mark.parametrize(
    "lib_id,lib,pins",
    [
        ("Device:R", _R_LIB, ["1", "2"]),
        ("T:IC4", _IC_LIB, ["1", "2", "3", "4"]),
    ],
    ids=["resistor_vertical_pins", "ic_horizontal_pins"],
)
def test_stub_extends_outward(tmp_path, lib_id, lib, pins, rotation, mirror):
    """Every connect_to_net stub/label must land farther from the body than the pin."""
    captured: dict = {}

    def fake_add_wire(path, start, end):
        captured["wire"] = (list(start), list(end))
        return True

    def fake_add_label(path, text, position, label_type="label", orientation=0):
        captured["label"] = (text, list(position), orientation)
        return True

    for pin in pins:
        sch = _write_sch(tmp_path, lib_id, lib, rotation, mirror)
        # Clear the process-wide PinLocator caches so each freshly-written file
        # (identical mtime is possible on fast filesystems) is re-parsed.
        PinLocator._SCHEMATIC_CACHE.clear()
        PinLocator._SEXP_CACHE.clear()
        PinLocator._PINDEF_CACHE.clear()

        with (
            patch("commands.wire_manager.WireManager.add_wire", side_effect=fake_add_wire),
            patch("commands.wire_manager.WireManager.add_label", side_effect=fake_add_label),
        ):
            result = ConnectionManager.connect_to_net(sch, "U1", pin, f"NET_{pin}")

        assert result["success"], f"connect failed for pin {pin}: {result.get('message')}"
        pin_loc = result["pin_location"]
        stub_end = result["label_location"]

        d_pin = _dist2(*pin_loc)
        d_stub = _dist2(*stub_end)
        assert d_stub > d_pin + 1e-6, (
            f"{lib_id} pin {pin} rot={rotation} mirror={mirror}: stub points INWARD "
            f"(pin={pin_loc} d={d_pin:.3f} -> stub={stub_end} d={d_stub:.3f})"
        )

        # Stub length is the standard 2.54 mm grid step.
        seg_len = math.dist(pin_loc, stub_end)
        assert seg_len == pytest.approx(2.54, abs=1e-3)

        # Label orientation should point the text outward: the label at stub_end,
        # advanced one more step along its orientation, keeps moving away.
        _, lbl_pos, orientation = captured["label"]
        assert orientation in (0, 90, 180, 270)
        adv_x = lbl_pos[0] + 2.54 * math.cos(math.radians(orientation))
        adv_y = lbl_pos[1] - 2.54 * math.sin(math.radians(orientation))
        assert _dist2(adv_x, adv_y) > d_stub - 1e-6, (
            f"{lib_id} pin {pin} rot={rotation} mirror={mirror}: label orientation "
            f"{orientation} points back toward the body"
        )


@pytest.mark.unit
def test_left_pin_stub_goes_left_and_right_pin_goes_right(tmp_path):
    """Concrete un-rotated check mirroring the reported NE555 symptom:
    a left-side pin (lib angle 0) stubs to the LEFT of its tip; a right-side pin
    (lib angle 180) stubs to the RIGHT. Pre-fix both went toward the body."""
    sch = _write_sch(tmp_path, "T:IC4", _IC_LIB, 0, None)
    PinLocator._SCHEMATIC_CACHE.clear()
    PinLocator._SEXP_CACHE.clear()
    PinLocator._PINDEF_CACHE.clear()

    with (
        patch("commands.wire_manager.WireManager.add_wire", return_value=True),
        patch("commands.wire_manager.WireManager.add_label", return_value=True),
    ):
        left = ConnectionManager.connect_to_net(sch, "U1", "1", "LNET")
        right = ConnectionManager.connect_to_net(sch, "U1", "3", "RNET")

    # Left pin tip is at world x = 100 - 7.62 = 92.38; stub must go further left.
    assert left["pin_location"][0] == pytest.approx(92.38, abs=1e-3)
    assert left["label_location"][0] < left["pin_location"][0]
    assert left["label_location"][1] == pytest.approx(left["pin_location"][1], abs=1e-3)

    # Right pin tip is at world x = 100 + 7.62 = 107.62; stub must go further right.
    assert right["pin_location"][0] == pytest.approx(107.62, abs=1e-3)
    assert right["label_location"][0] > right["pin_location"][0]
    assert right["label_location"][1] == pytest.approx(right["pin_location"][1], abs=1e-3)
