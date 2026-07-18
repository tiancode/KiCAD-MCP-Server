"""Fix 4 regression: set_no_connect refuses a CONNECTED pin.

A no-connect flag on a pin that already has a wire endpoint or a net label at its
endpoint is contradictory (KiCad ERC: "pin connected but marked no-connect"). The
handler refuses with ``pin_connected: {net, via}`` unless ``force=true``. A
floating pin is unaffected.
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
from handlers.schematic_wire._wires import handle_add_no_connect  # noqa: E402

_R_LIB = (
    '(symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))\n'
    '  (symbol "R_1_1"\n'
    '    (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))\n'
    '    (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))))'
)


def _placed(ref: str, x: float, y: float, u: int) -> str:
    return (
        f'  (symbol (lib_id "Device:R") (at {x} {y} 0) (unit 1)\n'
        "    (in_bom yes) (on_board yes) (dnp no)\n"
        f'    (uuid "1111111{u}-1111-1111-1111-1111111111aa")\n'
        f'    (property "Reference" "{ref}" (at {x} {y} 0))\n'
        f'    (property "Value" "1k" (at {x} {y} 0))\n'
        "    (instances\n"
        '      (project "test"\n'
        f'        (path "/00000000-0000-0000-0000-0000000000aa" (reference "{ref}") (unit 1)))))\n'
    )


def _clear() -> None:
    PinLocator._SCHEMATIC_CACHE.clear()
    PinLocator._SEXP_CACHE.clear()
    PinLocator._PINDEF_CACHE.clear()


def _count_nc(p: Path) -> int:
    return p.read_text(encoding="utf-8").count("(no_connect")


@pytest.fixture()
def sch(tmp_path: Path) -> Path:
    text = (
        '(kicad_sch (version 20250114) (generator "test")\n'
        '  (uuid "00000000-0000-0000-0000-0000000000aa")\n'
        '  (paper "A4")\n'
        "  (lib_symbols\n"
        f"    {_R_LIB}\n"
        "  )\n" + _placed("R1", 100, 100, 1) + '  (sheet_instances (path "/" (page "1")))\n'
        ")\n"
    )
    p = tmp_path / "board.kicad_sch"
    p.write_text(text)
    # R1/1 is labeled CC1 (connected via label); R1/2 is floating.
    r1p1 = PinLocator().get_pin_location(p, "R1", "1")
    WireManager.add_label(p, "CC1", r1p1)
    _clear()
    return p


@pytest.mark.unit
def test_refuses_label_connected_pin(sch: Path) -> None:
    res = handle_add_no_connect(
        MagicMock(), {"schematicPath": str(sch), "componentRef": "R1", "pinNumber": "1"}
    )
    assert res["success"] is False
    assert res["pin_connected"] == {"net": "CC1", "via": "label"}
    assert _count_nc(sch) == 0  # nothing written


@pytest.mark.unit
def test_force_overrides(sch: Path) -> None:
    res = handle_add_no_connect(
        MagicMock(),
        {"schematicPath": str(sch), "componentRef": "R1", "pinNumber": "1", "force": True},
    )
    assert res["success"] is True
    assert _count_nc(sch) == 1


@pytest.mark.unit
def test_floating_pin_unaffected(sch: Path) -> None:
    res = handle_add_no_connect(
        MagicMock(), {"schematicPath": str(sch), "componentRef": "R1", "pinNumber": "2"}
    )
    assert res["success"] is True
    assert _count_nc(sch) == 1


@pytest.mark.unit
def test_refuses_wire_connected_pin(sch: Path) -> None:
    # Add a wire onto R1/2's endpoint so it is physically (wire) connected.
    r1p2 = PinLocator().get_pin_location(sch, "R1", "2")
    WireManager.add_wire(sch, r1p2, [r1p2[0], r1p2[1] + 2.54])
    _clear()
    res = handle_add_no_connect(
        MagicMock(), {"schematicPath": str(sch), "componentRef": "R1", "pinNumber": "2"}
    )
    assert res["success"] is False
    assert res["pin_connected"]["via"] == "wire"
    assert _count_nc(sch) == 0
