"""Shared unit conversion and coordinate-extraction helpers.

Split out of the former handlers/ipc_fastpath.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.ipc_fastpath")


# Length conversion factors → millimetres.
# inch = 25.4 mm; mil = 0.001 inch = 0.0254 mm.  Unknown units pass through
# unchanged at the to_mm() call site rather than raise — schema validation
# upstream is the right place to reject bad units.
_TO_MM_SCALE = {"mm": 1.0, "inch": 25.4, "mil": 0.0254}


def to_mm(value: Any, unit: str) -> Any:
    """Convert ``value`` from ``unit`` to millimetres.

    Preserves the input type when ``unit == "mm"`` (so an int passed in as
    mm comes back as an int) — IPC consumers accept either, but keeping the
    original shape avoids accidental float coercion changes downstream.
    """
    scale = _TO_MM_SCALE.get(unit, 1.0)
    return value if scale == 1.0 else value * scale


def extract_xy(
    params: Dict[str, Any],
    key: str = "position",
    flat_x: str = "x",
    flat_y: str = "y",
    default_unit: str = "mm",
) -> Tuple[Any, Any, str]:
    """Pull (x, y, unit) out of ``params``.

    Accepts both nested-dict and flat-top-level shapes:

        {"position": {"x": 1, "y": 2, "unit": "mm"}}    # preferred
        {"x": 1, "y": 2}                                # legacy flat form

    For commands whose flat names aren't ``x``/``y`` (e.g. route_trace uses
    ``startX``/``startY``), pass ``flat_x``/``flat_y`` explicitly.  The
    ``unit`` is only meaningful when the nested form is used; the flat form
    has no place to carry it, so it falls back to ``default_unit``.
    """
    nested = params.get(key)
    if isinstance(nested, dict):
        return (
            nested.get("x", 0),
            nested.get("y", 0),
            nested.get("unit", default_unit),
        )
    return params.get(flat_x, 0), params.get(flat_y, 0), default_unit
