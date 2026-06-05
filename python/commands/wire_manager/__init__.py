"""Wire-manager package.

Re-exports WireManager and the module-level helpers so existing
``from commands.wire_manager import ...`` imports keep working after the
split from a single module into this package.
"""

from ._manager import WireManager
from ._text import (
    _normalize_label_type,
    _validate_schematic_sexpr,
    _serialize_validated,
    _find_insertion_point,
    _text_insert,
    _make_hierarchical_label_text,
    _make_sheet_pin_text,
    _make_sheet_text,
)

__all__ = [
    "WireManager",
    "_normalize_label_type",
    "_validate_schematic_sexpr",
    "_serialize_validated",
    "_find_insertion_point",
    "_text_insert",
    "_make_hierarchical_label_text",
    "_make_sheet_pin_text",
    "_make_sheet_text",
]
