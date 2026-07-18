"""Fix 6 regression: get_net_connections flags a dangling power-symbol pin.

A power symbol placed in empty space joins its net BY NAME, but its pin is
physically dangling — kicad-cli ERC reports "Pin not connected". The query used
to list it as a net member with no hint. Now each power_symbols entry carries
``floating: bool`` and a top-level ``warnings`` entry names the dangling pin.
A wired power symbol is NOT flagged.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PYTHON_DIR = os.path.join(os.path.dirname(__file__), "..", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

sys.modules.setdefault("pcbnew", MagicMock())

from commands.pin_locator import PinLocator  # noqa: E402
from commands.wire_manager import WireManager  # noqa: E402
from handlers.schematic_query import handle_get_net_connections  # noqa: E402

_R_LIB = (
    '(symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))\n'
    '  (symbol "R_1_1"\n'
    '    (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))\n'
    '    (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))))'
)
_PWR5V_LIB = (
    '(symbol "power:+5V" (power) (pin_names (offset 0) hide) (in_bom no) (on_board yes)\n'
    '  (symbol "+5V_1_1"\n'
    '    (pin power_in line (at 0 0 90) (length 0) (name "+5V") (number "1"))))'
)


def _placed(lib_id: str, ref: str, value: str, x: float, y: float, u: int) -> str:
    return (
        f'  (symbol (lib_id "{lib_id}") (at {x} {y} 0) (unit 1)\n'
        "    (in_bom yes) (on_board yes) (dnp no)\n"
        f'    (uuid "1111111{u}-1111-1111-1111-1111111111aa")\n'
        f'    (property "Reference" "{ref}" (at {x} {y} 0))\n'
        f'    (property "Value" "{value}" (at {x} {y} 0))\n'
        "    (instances\n"
        '      (project "test"\n'
        f'        (path "/00000000-0000-0000-0000-0000000000aa" (reference "{ref}") (unit 1)))))\n'
    )


def _clear() -> None:
    PinLocator._SCHEMATIC_CACHE.clear()
    PinLocator._SEXP_CACHE.clear()
    PinLocator._PINDEF_CACHE.clear()


@pytest.fixture()
def sch(tmp_path: Path) -> Path:
    """+5V net: #PWR01 floating in empty space; #PWR02 wired to R1/1."""
    text = (
        '(kicad_sch (version 20250114) (generator "test")\n'
        '  (uuid "00000000-0000-0000-0000-0000000000aa")\n'
        '  (paper "A4")\n'
        "  (lib_symbols\n"
        f"    {_R_LIB}\n    {_PWR5V_LIB}\n"
        "  )\n"
        + _placed("Device:R", "R1", "1k", 100, 100, 1)
        + _placed("power:+5V", "#PWR01", "+5V", 30, 40, 2)  # floating (empty space)
        + _placed("power:+5V", "#PWR02", "+5V", 100, 90, 3)  # wired below
        + '  (sheet_instances (path "/" (page "1")))\n'
        ")\n"
    )
    p = tmp_path / "board.kicad_sch"
    p.write_text(text)
    r1p1 = PinLocator().get_pin_location(p, "R1", "1")
    pwr2 = PinLocator().get_pin_location(p, "#PWR02", "1")
    WireManager.add_wire(p, pwr2, r1p1)  # +5V rail joins #PWR02 to R1/1
    _clear()
    return p


@pytest.mark.unit
def test_floating_power_symbol_flagged(sch: Path) -> None:
    res = handle_get_net_connections(MagicMock(), {"schematicPath": str(sch), "netName": "+5V"})
    assert res["success"]

    by_ref = {p["ref"]: p for p in res["power_symbols"]}
    assert set(by_ref) == {"#PWR01", "#PWR02"}
    assert by_ref["#PWR01"]["floating"] is True
    assert by_ref["#PWR02"]["floating"] is False

    # A top-level warning names the dangling pin.
    warnings = res.get("warnings") or []
    assert any("#PWR01" in w and "dangling" in w for w in warnings)
    assert all("#PWR02" not in w for w in warnings)
