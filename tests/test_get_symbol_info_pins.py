"""Regression tests for pin info on get_symbol_info.

User report: ``get_symbol_info`` returns properties only.  To plan
placement coordinates an agent had to call ``add_schematic_component``
first and then ``get_schematic_pin_locations`` — a round-trip just to
read what's already in the .kicad_sym file.  The handler now inlines
``pins[]`` (in the symbol's local coordinate frame) and a
``pin_bounding_box``.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


FIXTURE = Path(__file__).parent / "fixtures" / "Simulation_SPICE_minimal.kicad_sym"


def _manager() -> "SymbolLibraryManager":
    from commands.library_symbol import SymbolLibraryManager

    m = SymbolLibraryManager.__new__(SymbolLibraryManager)
    m.project_path = None
    m.libraries = {"Simulation_SPICE": str(FIXTURE)}
    m.symbol_cache = {}
    m._cache_mtimes = {}
    m._cache_dirty = False
    return m


# ---------------------------------------------------------------------------
# get_symbol_pins direct
# ---------------------------------------------------------------------------
def test_get_symbol_pins_extracts_opamp_pins_in_local_coords():
    manager = _manager()

    pins = manager.get_symbol_pins("Simulation_SPICE", "OPAMP")

    assert pins is not None
    by_number = {p["number"]: p for p in pins}
    # The fixture has +/-/V+/V-/out on OPAMP (5 pins) — at minimum pin 1 must
    # be at (-7.62, 2.54) per the local-coord block.
    assert "1" in by_number
    p1 = by_number["1"]
    assert p1["x"] == pytest.approx(-7.62)
    assert p1["y"] == pytest.approx(2.54)
    assert p1["length"] == pytest.approx(2.54)
    # Pin name is captured.
    assert p1["name"] == "+"
    # Pin type comes from the (pin <type> <shape>) tuple.
    assert p1["type"] == "input"


def test_get_symbol_pins_returns_sorted_list():
    """Pins must come back numeric-sorted for stable iteration."""
    manager = _manager()

    pins = manager.get_symbol_pins("Simulation_SPICE", "OPAMP")

    numbers = [p["number"] for p in pins]
    # Numeric pins sort numerically, not lexicographically.
    assert numbers == sorted(numbers, key=lambda n: int(n))


def test_get_symbol_pins_returns_none_for_unknown_symbol():
    manager = _manager()

    pins = manager.get_symbol_pins("Simulation_SPICE", "DOES_NOT_EXIST")

    assert pins is None


def test_get_symbol_pins_returns_none_for_unknown_library():
    manager = _manager()

    pins = manager.get_symbol_pins("Unknown_Library", "OPAMP")

    assert pins is None


# ---------------------------------------------------------------------------
# Handler-level get_symbol_info — pins inlined into response
# ---------------------------------------------------------------------------
def test_handler_inlines_pins_and_bounding_box(monkeypatch):
    from commands.library_symbol import SymbolLibraryCommands

    cmds = SymbolLibraryCommands.__new__(SymbolLibraryCommands)
    cmds.library_manager = _manager()
    cmds._ensure_manager_for = lambda params: None  # type: ignore[method-assign]

    out = cmds.get_symbol_info({"symbol": "Simulation_SPICE:OPAMP"})

    assert out["success"] is True
    info = out["symbol_info"]
    # Properties still there.
    assert info["full_ref"] == "Simulation_SPICE:OPAMP"
    # Pins inlined.
    assert "pins" in info
    assert info["pin_count"] == len(info["pins"])
    assert info["pin_count"] >= 5  # OPAMP fixture has 5 pins
    # Bounding box covers all pin endpoints in local coords.
    bbox = info["pin_bounding_box"]
    assert bbox["unit"] == "mm"
    xs = [p["x"] for p in info["pins"]]
    ys = [p["y"] for p in info["pins"]]
    assert bbox["min_x"] == min(xs)
    assert bbox["max_x"] == max(xs)
    assert bbox["min_y"] == min(ys)
    assert bbox["max_y"] == max(ys)


def test_handler_omits_pin_fields_when_extraction_fails(monkeypatch):
    """If pin extraction raises (corrupted lib, sexp parse error, …)
    the response must still succeed with the symbol's properties — pin
    info is best-effort, never the gating factor."""
    from commands.library_symbol import SymbolLibraryCommands

    cmds = SymbolLibraryCommands.__new__(SymbolLibraryCommands)
    cmds.library_manager = _manager()
    cmds._ensure_manager_for = lambda params: None  # type: ignore[method-assign]
    # Force the pin parser to fail.
    cmds.library_manager.get_symbol_pins = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("corrupted")
    )

    out = cmds.get_symbol_info({"symbol": "Simulation_SPICE:OPAMP"})

    assert out["success"] is True
    info = out["symbol_info"]
    assert info["full_ref"] == "Simulation_SPICE:OPAMP"
    # No pin fields when extraction failed.
    assert "pins" not in info
    assert "pin_bounding_box" not in info
