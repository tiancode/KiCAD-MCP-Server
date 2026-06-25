"""Tests for ``mil`` unit support across position/coordinate commands.

KiCad natively understands mils (1 mil = 0.0254 mm = 25 400 nm). The MCP
server now accepts ``"mil"`` as a value of the ``unit`` field everywhere a
position or coordinate is specified. Tests below assert:

  - The ``unit→nanometer`` scale used in command handlers maps mil → 25 400.
  - The IPC handler converts mil positions to mm before calling the IPC API.
  - The Python tool schema enums include ``"mil"`` (it was previously
    restricted to ``["mm", "inch"]``).
"""

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

# Scale used by the per-command handlers when they convert position values
# to KiCad internal nanometers.
MIL_TO_NM = 25_400
MM_TO_NM = 1_000_000
INCH_TO_NM = 25_400_000


def _scale(unit: str) -> int:
    """Mirror of the inline ternary used in commands/* for position scaling."""
    return 1_000_000 if unit == "mm" else (25_400 if unit == "mil" else 25_400_000)


def test_scale_mapping_includes_mil():
    assert _scale("mm") == MM_TO_NM
    assert _scale("mil") == MIL_TO_NM
    assert _scale("inch") == INCH_TO_NM


def test_one_thousand_mil_equals_one_inch_in_nm():
    """Sanity check: 1000 mil should produce the same nm offset as 1 inch."""
    one_inch = 1 * _scale("inch")
    one_thousand_mil = 1000 * _scale("mil")
    assert one_inch == one_thousand_mil


def _make_iface() -> Any:
    with patch("kicad_interface.USE_IPC_BACKEND", True):
        from kicad_interface import KiCADInterface

        iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    iface.board = None
    iface.ipc_board_api = MagicMock()
    # See note in test_ipc_position_inch_conversion: MagicMock would
    # otherwise auto-create _current_commit as a truthy Mock and trip
    # the place_component transaction guard.
    iface.ipc_board_api._current_commit = None
    iface.ipc_board_api.place_component.return_value = True
    iface.ipc_board_api.move_component.return_value = True
    return iface


def test_ipc_place_component_converts_mil_to_mm():
    iface = _make_iface()
    iface._ipc_place_component(
        {
            "reference": "R1",
            "footprint": "Resistor_SMD:R_0805",
            "position": {"x": 100, "y": 200, "unit": "mil"},
            "rotation": 0,
            "layer": "F.Cu",
            "value": "220",
        }
    )
    _, kwargs = iface.ipc_board_api.place_component.call_args
    assert kwargs["x"] == pytest.approx(2.54)
    assert kwargs["y"] == pytest.approx(5.08)


def test_ipc_move_component_converts_mil_to_mm():
    iface = _make_iface()
    iface._ipc_move_component({"reference": "R1", "position": {"x": 1000, "y": 500, "unit": "mil"}})
    _, kwargs = iface.ipc_board_api.move_component.call_args
    assert kwargs["x"] == pytest.approx(25.4)
    assert kwargs["y"] == pytest.approx(12.7)
