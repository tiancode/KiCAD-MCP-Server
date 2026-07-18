"""Length-unit conversion shared by the SWIG and IPC backends.

KiCad's internal unit is the nanometre (1 mm = 1_000_000 nm; both pcbnew IU
and kipy report lengths in nm).  Tools that return coordinates/sizes accept a
``unit`` of mm/mil/inch — convert from nm here so the two backends can't drift
on the factors.  The inverse direction (parsing user input) goes through
``unit_to_nm_scale`` with the same constants.

Input parsing (``unit_to_nm_scale``) treats a missing/None unit as the uniform
default of **millimetres** and REFUSES an unrecognised non-None unit string
by raising :class:`InvalidUnitError` — historically the parse path silently
treated anything that wasn't ``"mm"``/``"mil"`` as inch, so a typo like
``"banana"`` scaled coordinates ×25.4 and landed copper hundreds of mm off the
board with ``success: true``.  Output formatting (``nm_to_unit`` /
``normalize_unit``) still defaults unknown units to mm rather than raising —
it only ever labels an already-computed value.
"""

from typing import Any, Optional

# nm per output unit.
_NM_PER_UNIT = {
    "mm": 1_000_000.0,
    "mil": 25_400.0,
    "inch": 25_400_000.0,
}


class InvalidUnitError(ValueError):
    """Raised by ``unit_to_nm_scale`` for an unrecognised (non-None) unit.

    Carries the offending ``unit`` so a refusal site can echo it.  The
    message names the valid units so the structured ``VALIDATION`` refusal it
    becomes (see ``utils.responses.failed`` / ``utils.failure``) is actionable.
    """

    def __init__(self, unit: Any) -> None:
        self.unit = unit
        super().__init__(
            f"Invalid unit {unit!r}: valid units are 'mm', 'mil', 'inch' "
            f"(omit the unit for the default 'mm')."
        )


def normalize_unit(unit: Optional[str]) -> str:
    """Return a supported unit string, defaulting unknown/None to ``"mm"``."""
    return unit if unit in _NM_PER_UNIT else "mm"


def nm_to_unit(nm: float, unit: Optional[str] = "mm") -> float:
    """Convert a length in KiCad nanometres to mm/mil/inch (default mm)."""
    return nm / _NM_PER_UNIT[normalize_unit(unit)]


def unit_to_nm_scale(unit: Any) -> int:
    """nm per input ``unit`` when parsing user coordinates/sizes.

    A missing/None unit is the uniform default of millimetres.  ``"mm"``,
    ``"mil"`` and ``"inch"`` map to their exact factors.  Any other non-None
    value raises :class:`InvalidUnitError` rather than silently falling
    through to inch (the historical bug — a unit typo scaled input ×25.4 and
    reported success).  Output formatting goes the other way via
    ``nm_to_unit``, which defaults unknown units to mm.
    """
    if unit is None:
        return 1_000_000
    try:
        scale = _NM_PER_UNIT.get(unit)
    except TypeError:  # unhashable unit (dict/list) — never a valid unit
        scale = None
    if scale is None:
        raise InvalidUnitError(unit)
    return int(scale)
