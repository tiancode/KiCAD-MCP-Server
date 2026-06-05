"""Schematic analysis package.

Re-exports every analysis function so existing
``from commands.schematic_analysis import ...`` imports keep working after
the split from one module into this package.
"""

from ._parsing import (
    _extract_lib_symbols,
    _load_sexp,
    _parse_labels,
    _parse_lib_symbol_graphics,
    _parse_symbols,
    _parse_wires,
)
from ._geometry import (
    _aabb_overlap,
    _compute_pin_positions_direct,
    _compute_symbol_bbox_direct,
    _distance,
    _line_segment_intersects_aabb,
    _point_in_rect,
    _transform_local_point,
    compute_symbol_bbox,
)
from ._queries import (
    _check_wire_overlap,
    find_orphaned_wires,
    find_overlapping_elements,
    find_wires_crossing_symbols,
    get_elements_in_region,
)

__all__ = [
    "_aabb_overlap",
    "_check_wire_overlap",
    "_compute_pin_positions_direct",
    "_compute_symbol_bbox_direct",
    "_distance",
    "_extract_lib_symbols",
    "_line_segment_intersects_aabb",
    "_load_sexp",
    "_parse_labels",
    "_parse_lib_symbol_graphics",
    "_parse_symbols",
    "_parse_wires",
    "_point_in_rect",
    "_transform_local_point",
    "compute_symbol_bbox",
    "find_orphaned_wires",
    "find_overlapping_elements",
    "find_wires_crossing_symbols",
    "get_elements_in_region",
]
