"""Length-unit conversion shared by the SWIG and IPC backends.

KiCad's internal unit is the nanometre (1 mm = 1_000_000 nm; both pcbnew IU
and kipy report lengths in nm).  Tools that return coordinates/sizes accept a
``unit`` of mm/mil/inch — convert from nm here so the two backends can't drift
on the factors.  The inverse direction (parsing user input) goes through
``unit_to_nm_scale`` with the same constants.
"""

from typing import Any, Optional

# nm per output unit.
_NM_PER_UNIT = {
    "mm": 1_000_000.0,
    "mil": 25_400.0,
    "inch": 25_400_000.0,
}


def normalize_unit(unit: Optional[str]) -> str:
    """Return a supported unit string, defaulting unknown/None to ``"mm"``."""
    return unit if unit in _NM_PER_UNIT else "mm"


def nm_to_unit(nm: float, unit: Optional[str] = "mm") -> float:
    """Convert a length in KiCad nanometres to mm/mil/inch (default mm)."""
    return nm / _NM_PER_UNIT[normalize_unit(unit)]


def unit_to_nm_scale(unit: Any) -> int:
    """nm per input ``unit`` when parsing user coordinates/sizes.

    Preserves the historical inline-ternary semantics shared by every parse
    site this replaces: "mm" and "mil" map exactly, anything else (including
    a missing/None unit) is treated as inch.  Output formatting goes the
    other way via nm_to_unit, which defaults unknown units to mm.
    """
    return 1_000_000 if unit == "mm" else (25_400 if unit == "mil" else 25_400_000)
