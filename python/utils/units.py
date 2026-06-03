"""Length-unit conversion shared by the SWIG and IPC backends.

KiCad's internal unit is the nanometre (1 mm = 1_000_000 nm; both pcbnew IU
and kipy report lengths in nm).  Tools that return coordinates/sizes accept a
``unit`` of mm/mil/inch — convert from nm here so the two backends can't drift
on the factors (the inverse, parsing user input, is done inline elsewhere with
the same constants: 1_000_000 / 25_400 / 25_400_000).
"""

from typing import Optional

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
