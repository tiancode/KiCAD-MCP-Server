"""Feature: add_schematic_net_labels (batch) places many labels in one call.

Reuses the single-label logic (including the Fix-5 collision + idempotency
guards). Per-item failures do not abort the batch; overall success is True when
at least one label was placed (or already present). Reports per-item results plus
aggregate counts.
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
from handlers.schematic_wire._labels import (  # noqa: E402
    handle_add_schematic_net_label,
    handle_add_schematic_net_labels,
)

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
        + _placed("R3", 200, 100, 3)
        + '  (sheet_instances (path "/" (page "1")))\n'
        ")\n"
    )
    p = tmp_path / "board.kicad_sch"
    p.write_text(text)
    # R1/2 pre-labeled GND (so the collision + idempotent items have a target).
    res = handle_add_schematic_net_label(
        MagicMock(),
        {"schematicPath": str(p), "netName": "GND", "componentRef": "R1", "pinNumber": "2"},
    )
    assert res["success"]
    _clear()
    return p


@pytest.mark.unit
def test_batch_mixed_results(sch: Path) -> None:
    res = handle_add_schematic_net_labels(
        MagicMock(),
        {
            "schematicPath": str(sch),
            "labels": [
                {"componentRef": "R2", "pinNumber": "1", "netName": "SIG"},  # fresh success
                {"componentRef": "R1", "pinNumber": "2", "netName": "VCC"},  # collision (GND there)
                {"componentRef": "R1", "pinNumber": "2", "netName": "GND"},  # already labeled
            ],
        },
    )

    # ≥1 placed → overall success.
    assert res["success"] is True
    counts = res["counts"]
    assert counts == {
        "total": 3,
        "placed": 1,
        "already_labeled": 1,
        "collisions": 1,
        "failed": 1,
    }

    results = res["results"]
    assert len(results) == 3
    # Item 1: fresh placement succeeded.
    assert results[0]["net"] == "SIG" and results[0].get("connected_to_pin", {}).get("ref") == "R2"
    assert "error" not in results[0]
    # Item 2: refused with a label_collision naming GND.
    assert results[1].get("label_collision", {}).get("existing_net") == "GND"
    assert "error" in results[1]
    # Item 3: idempotent.
    assert results[2].get("already_labeled") is True

    # File state: original GND + the one new SIG label (VCC never written).
    assert _count_labels(sch) == 2
    assert '(label "SIG"' in sch.read_text(encoding="utf-8")
    assert '(label "VCC"' not in sch.read_text(encoding="utf-8")


@pytest.mark.unit
def test_batch_all_fail_is_failure(sch: Path) -> None:
    res = handle_add_schematic_net_labels(
        MagicMock(),
        {
            "schematicPath": str(sch),
            "labels": [{"componentRef": "R1", "pinNumber": "2", "netName": "VCC"}],  # collision
        },
    )
    assert res["success"] is False
    assert res["counts"]["failed"] == 1
    assert res["counts"]["placed"] == 0


@pytest.mark.unit
def test_batch_requires_labels_list(sch: Path) -> None:
    res = handle_add_schematic_net_labels(MagicMock(), {"schematicPath": str(sch)})
    assert res["success"] is False
    assert "labels" in res["message"]
