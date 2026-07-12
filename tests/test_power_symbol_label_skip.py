"""
Regression for the redundant double-label when attaching power symbols (F4).

A power PORT (#PWR…, lib_id "power:*") already joins the net named by its Value
and self-labels its own pin. Placing a matching net label at that pin — the
workaround Phase A used — produced a doubled label (a type:"power" label AND a
type:"net" label at the same coordinate). The fix:

  * add_schematic_net_label / connect_to_net SKIP the write when the target pin
    is a power port and the requested netName == the symbol's Value, returning
    ``already_connected: true`` and no label.
  * A MISMATCHED name still writes but carries a ``warnings`` entry (the pin
    would otherwise silently carry both names).
  * PWR_FLAG (#FLG, power:PWR_FLAG) is NOT a named port — labeling its pin IS
    the correct attachment idiom — so its behavior is unchanged (write, no
    warning).

connect_to_net is exercised with WireManager mocked, exactly like
tests/test_connection_to_net_stub_direction.py.
"""

from __future__ import annotations

import os
import sys
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
from handlers.schematic_wire._labels import handle_add_schematic_net_label  # noqa: E402

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
_FLAG_LIB = (
    '(symbol "power:PWR_FLAG" (power) (pin_numbers hide) (pin_names (offset 0) hide)'
    " (in_bom no) (on_board yes)\n"
    '  (symbol "PWR_FLAG_0_0"\n'
    '    (pin power_out line (at 0 0 90) (length 0) (name "pwr") (number "1"))))'
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


def _clear_caches() -> None:
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
        f"    {_R_LIB}\n    {_PWR5V_LIB}\n    {_FLAG_LIB}\n"
        "  )\n"
        + _placed("Device:R", "R1", "1k", 100, 100, 1)
        + _placed("power:+5V", "#PWR01", "+5V", 100, 90, 2)
        + _placed("power:PWR_FLAG", "#FLG01", "PWR_FLAG", 120, 90, 3)
        + '  (sheet_instances (path "/" (page "1")))\n'
        ")\n"
    )
    p = tmp_path / "board.kicad_sch"
    p.write_text(text)
    _clear_caches()
    return p


def _count_labels(p: Path) -> int:
    return p.read_text().count("(label ")


def _add(sch: Path, **params: object) -> dict:
    _clear_caches()
    return handle_add_schematic_net_label(MagicMock(), {"schematicPath": str(sch), **params})


# ---------------------------------------------------------------------------
# add_schematic_net_label
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_label_on_power_symbol_matching_value_is_skipped(sch: Path) -> None:
    res = _add(sch, netName="+5V", componentRef="#PWR01", pinNumber="1")
    assert res["success"] is True
    assert res["already_connected"] is True
    assert res["skipped_label"] is True
    assert res["power_symbol"] == {"ref": "#PWR01", "value": "+5V"}
    assert res["connected_to_pin"] == {"ref": "#PWR01", "pin": "1"}
    assert "power symbol" in res["message"].lower()
    # Nothing was written to the file.
    assert _count_labels(sch) == 0


@pytest.mark.unit
def test_label_on_power_symbol_via_raw_position_is_skipped(sch: Path) -> None:
    # Raw position landing exactly on #PWR01's pin (100, 90) must be detected
    # via the landing-pin scan, not just the componentRef path.
    res = _add(sch, netName="+5V", position=[100.0, 90.0])
    assert res["success"] is True
    assert res["already_connected"] is True
    assert res["skipped_label"] is True
    assert _count_labels(sch) == 0


@pytest.mark.unit
def test_label_on_power_symbol_mismatched_value_writes_with_warning(sch: Path) -> None:
    res = _add(sch, netName="+3V3", componentRef="#PWR01", pinNumber="1")
    assert res["success"] is True
    assert res.get("already_connected") is None
    assert "warnings" in res and res["warnings"]
    assert "+5V" in res["warnings"][0] and "+3V3" in res["warnings"][0]
    # The label WAS written (the mismatch is surfaced, not silently dropped).
    assert _count_labels(sch) == 1


@pytest.mark.unit
def test_label_on_pwr_flag_is_written_normally(sch: Path) -> None:
    # #FLG (PWR_FLAG) is NOT a named port — labeling its pin is the correct
    # attachment idiom, so behavior is unchanged: write, no skip, no warning.
    res = _add(sch, netName="+5V", componentRef="#FLG01", pinNumber="1")
    assert res["success"] is True
    assert res.get("already_connected") is None
    assert res.get("warnings") is None
    assert res["connected_to_pin"] == {"ref": "#FLG01", "pin": "1"}
    assert _count_labels(sch) == 1


@pytest.mark.unit
def test_label_on_regular_pin_unaffected(sch: Path) -> None:
    # A non-power pin still writes and derives its outward orientation — the
    # power short-circuit must not disturb the existing (F2/orientation) path.
    res = _add(sch, netName="SIG", componentRef="R1", pinNumber="1")
    assert res["success"] is True
    assert res.get("already_connected") is None
    assert res.get("warnings") is None
    assert res["orientation_source"] == "pin_outward"
    assert _count_labels(sch) == 1


# ---------------------------------------------------------------------------
# connect_to_net (WireManager mocked)
# ---------------------------------------------------------------------------


def _inject_wire(sch: Path, x: float, y: float) -> None:
    """Write a wire whose endpoint touches (x, y) straight into the file, so the
    pin there reads as physically connected without going through the (mocked)
    WireManager.add_wire. Used to exercise the F3 "already connected → skip" path.
    """
    import uuid as _uuid

    text = sch.read_text()
    wire = (
        f"  (wire (pts (xy {x} {y}) (xy {x} {y - 2.54})) "
        f'(stroke (width 0) (type default)) (uuid "{_uuid.uuid4()}"))\n'
    )
    marker = "  (sheet_instances"
    assert marker in text
    sch.write_text(text.replace(marker, wire + marker, 1))
    _clear_caches()


@pytest.mark.unit
def test_connect_to_net_connected_power_symbol_matching_value_skips_write(sch: Path) -> None:
    # F3: a power pin that is ALREADY physically connected (a wire touches
    # #PWR01's pin at (100, 90)) still short-circuits — no duplicate wire/label.
    _inject_wire(sch, 100.0, 90.0)
    _clear_caches()
    with (
        patch("commands.wire_manager.WireManager.add_wire", return_value=True) as add_wire,
        patch("commands.wire_manager.WireManager.add_label", return_value=True) as add_label,
    ):
        res = ConnectionManager.connect_to_net(sch, "#PWR01", "1", "+5V")

    assert res["success"] is True
    assert res["already_connected"] is True
    assert res["skipped_label"] is True
    assert res["power_symbol"] == {"ref": "#PWR01", "value": "+5V"}
    # Neither a stub wire nor a label was drawn.
    add_wire.assert_not_called()
    add_label.assert_not_called()


@pytest.mark.unit
def test_connect_to_net_floating_power_symbol_draws_stub(sch: Path) -> None:
    # F3 core fix: a FLOATING power pin (#PWR01 pin at (100, 90), no wire, no
    # coincident pin) must NOT be silently skipped — connect_to_net draws a stub
    # WIRE so ERC no longer reports "Pin not connected", but NO label (the power
    # symbol already names the net; a label would duplicate it).
    _clear_caches()
    with (
        patch("commands.wire_manager.WireManager.add_wire", return_value=True) as add_wire,
        patch("commands.wire_manager.WireManager.add_label", return_value=True) as add_label,
    ):
        res = ConnectionManager.connect_to_net(sch, "#PWR01", "1", "+5V")

    assert res["success"] is True
    assert res.get("already_connected") is None
    assert res["drew_stub_wire"] is True
    assert res["label_location"] is None
    assert res["power_symbol"] == {"ref": "#PWR01", "value": "+5V"}
    add_wire.assert_called_once()
    add_label.assert_not_called()


@pytest.mark.unit
def test_connect_to_net_power_symbol_mismatch_writes_with_warning(sch: Path) -> None:
    _clear_caches()
    with (
        patch("commands.wire_manager.WireManager.add_wire", return_value=True) as add_wire,
        patch("commands.wire_manager.WireManager.add_label", return_value=True) as add_label,
    ):
        res = ConnectionManager.connect_to_net(sch, "#PWR01", "1", "+3V3")

    assert res["success"] is True
    assert res.get("already_connected") is None
    assert "warnings" in res and res["warnings"]
    assert "+5V" in res["warnings"][0] and "+3V3" in res["warnings"][0]
    add_wire.assert_called_once()
    add_label.assert_called_once()


@pytest.mark.unit
def test_connect_to_net_pwr_flag_writes_normally(sch: Path) -> None:
    _clear_caches()
    with (
        patch("commands.wire_manager.WireManager.add_wire", return_value=True) as add_wire,
        patch("commands.wire_manager.WireManager.add_label", return_value=True) as add_label,
    ):
        res = ConnectionManager.connect_to_net(sch, "#FLG01", "1", "+5V")

    assert res["success"] is True
    assert res.get("already_connected") is None
    assert res.get("warnings") is None
    add_wire.assert_called_once()
    add_label.assert_called_once()


@pytest.mark.unit
def test_connect_to_net_regular_pin_unaffected(sch: Path) -> None:
    _clear_caches()
    with (
        patch("commands.wire_manager.WireManager.add_wire", return_value=True) as add_wire,
        patch("commands.wire_manager.WireManager.add_label", return_value=True) as add_label,
    ):
        res = ConnectionManager.connect_to_net(sch, "R1", "1", "SIG")

    assert res["success"] is True
    assert res.get("already_connected") is None
    assert res.get("warnings") is None
    add_wire.assert_called_once()
    add_label.assert_called_once()
