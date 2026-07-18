"""Fix 5 regression: add_schematic_net_label guards against different-net stacking.

Placing a DIFFERENT-named label on an already-labeled point silently shorts the
two nets. The handler now refuses with ``label_collision: {point, existing_net}``
unless ``force=true``; a SAME-named label already there is idempotent
(``already_labeled: true``, no duplicate written). Normal placement on a fresh
pin is unaffected.
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
from handlers.schematic_wire._labels import handle_add_schematic_net_label  # noqa: E402

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


def _count_labels(p: Path) -> int:
    return p.read_text(encoding="utf-8").count("(label ")


def _add(sch: Path, **params: object) -> dict:
    _clear()
    return handle_add_schematic_net_label(MagicMock(), {"schematicPath": str(sch), **params})


@pytest.fixture()
def sch(tmp_path: Path) -> Path:
    text = (
        '(kicad_sch (version 20250114) (generator "test")\n'
        '  (uuid "00000000-0000-0000-0000-0000000000aa")\n'
        '  (paper "A4")\n'
        "  (lib_symbols\n"
        f"    {_R_LIB}\n"
        "  )\n"
        + _placed("R1", 100, 100, 1)
        + _placed("R2", 150, 100, 2)
        + '  (sheet_instances (path "/" (page "1")))\n'
        ")\n"
    )
    p = tmp_path / "board.kicad_sch"
    p.write_text(text)
    # R1/2 gets a GND label up front (the already-labeled node).
    res = _add(p, netName="GND", componentRef="R1", pinNumber="2")
    assert res["success"] and res.get("connected_to_pin", {}).get("ref") == "R1"
    assert _count_labels(p) == 1
    return p


@pytest.mark.unit
def test_different_net_refused(sch: Path) -> None:
    res = _add(sch, netName="VCC_TEST", componentRef="R1", pinNumber="2")
    assert res["success"] is False
    assert res["label_collision"]["existing_net"] == "GND"
    assert "point" in res["label_collision"]
    # Nothing written — the short was prevented.
    assert _count_labels(sch) == 1


@pytest.mark.unit
def test_force_overrides(sch: Path) -> None:
    res = _add(sch, netName="VCC_TEST", componentRef="R1", pinNumber="2", force=True)
    assert res["success"] is True
    assert _count_labels(sch) == 2


@pytest.mark.unit
def test_same_net_is_idempotent(sch: Path) -> None:
    res = _add(sch, netName="GND", componentRef="R1", pinNumber="2")
    assert res["success"] is True
    assert res.get("already_labeled") is True
    # No duplicate GND label written.
    assert _count_labels(sch) == 1


@pytest.mark.unit
def test_fresh_pin_unaffected(sch: Path) -> None:
    res = _add(sch, netName="SIG", componentRef="R2", pinNumber="1")
    assert res["success"] is True
    assert res.get("already_labeled") is None
    assert res.get("label_collision") is None
    assert _count_labels(sch) == 2
