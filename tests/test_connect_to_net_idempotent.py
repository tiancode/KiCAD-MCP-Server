"""Fix 3 regression: connect_to_net is idempotent when the pin is label-connected.

A pin that already carries a same-net label at its endpoint is already on the
net (via:"label", the same membership get_wire_connections reports). Re-running
connect_to_net must NOT stack a redundant duplicate stub + label — it returns the
already_connected response with no geometry written. A genuinely unconnected pin
still gets its stub + label.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PYTHON_DIR = os.path.join(os.path.dirname(__file__), "..", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

sys.modules.setdefault("pcbnew", MagicMock())

from commands.connection_schematic import ConnectionManager  # noqa: E402
from commands.pin_locator import PinLocator  # noqa: E402
from commands.wire_manager import WireManager  # noqa: E402

_R_LIB = (
    '(symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))\n'
    '  (symbol "R_1_1"\n'
    '    (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))\n'
    '    (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))))'
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
    text = (
        '(kicad_sch (version 20250114) (generator "test")\n'
        '  (uuid "00000000-0000-0000-0000-0000000000aa")\n'
        '  (paper "A4")\n'
        "  (lib_symbols\n"
        f"    {_R_LIB}\n"
        "  )\n"
        + _placed("Device:R", "R1", "1k", 100, 100, 1)
        + '  (sheet_instances (path "/" (page "1")))\n'
        ")\n"
    )
    p = tmp_path / "board.kicad_sch"
    p.write_text(text)
    _clear()
    return p


@pytest.mark.unit
def test_label_connected_pin_is_noop(sch: Path) -> None:
    # R1/1 gets a "CC1" label placed directly on its pin endpoint.
    r1p1 = PinLocator().get_pin_location(sch, "R1", "1")
    assert WireManager.add_label(sch, "CC1", r1p1)
    _clear()

    with (
        patch("commands.wire_manager.WireManager.add_wire", return_value=True) as add_wire,
        patch("commands.wire_manager.WireManager.add_label", return_value=True) as add_label,
    ):
        res = ConnectionManager.connect_to_net(sch, "R1", "1", "CC1")

    assert res["success"] is True
    assert res["already_connected"] is True
    assert res.get("via") == "label"
    # No duplicate stub or label written.
    add_wire.assert_not_called()
    add_label.assert_not_called()


@pytest.mark.unit
def test_unconnected_pin_still_connects(sch: Path) -> None:
    # R1/2 carries no label — connect_to_net must still draw the stub + label.
    _clear()
    with (
        patch("commands.wire_manager.WireManager.add_wire", return_value=True) as add_wire,
        patch("commands.wire_manager.WireManager.add_label", return_value=True) as add_label,
    ):
        res = ConnectionManager.connect_to_net(sch, "R1", "2", "SIG")

    assert res["success"] is True
    assert res.get("already_connected") is None
    add_wire.assert_called_once()
    add_label.assert_called_once()


@pytest.mark.unit
def test_different_net_label_not_treated_as_connected(sch: Path) -> None:
    # R1/1 has a "CC1" label; connecting it to a DIFFERENT net must NOT no-op.
    r1p1 = PinLocator().get_pin_location(sch, "R1", "1")
    assert WireManager.add_label(sch, "CC1", r1p1)
    _clear()
    with (
        patch("commands.wire_manager.WireManager.add_wire", return_value=True) as add_wire,
        patch("commands.wire_manager.WireManager.add_label", return_value=True) as add_label,
    ):
        res = ConnectionManager.connect_to_net(sch, "R1", "1", "CC2")

    # A different net is a real (new) connection request, not idempotent.
    assert res.get("already_connected") is None
    assert add_wire.called or add_label.called
