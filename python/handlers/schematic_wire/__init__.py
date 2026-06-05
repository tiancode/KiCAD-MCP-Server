"""Schematic Wire handlers package.

Re-exports every handler and helper so the dispatcher and existing
``from handlers.schematic_wire import ...`` imports keep working after the split.
"""

from ._sheets import (
    handle_add_schematic_sheet,
    handle_add_sheet_pin,
)
from ._labels import (
    _LABEL_PIN_CONNECT_TOLERANCE_MM,
    _scan_all_pin_positions,
    handle_add_schematic_hierarchical_label,
    handle_add_schematic_net_label,
    handle_delete_schematic_net_label,
    handle_edit_schematic_net_label,
    handle_move_schematic_net_label,
)
from ._wires import (
    handle_add_no_connect,
    handle_add_schematic_wire,
    handle_connect_passthrough,
    handle_connect_to_net,
    handle_delete_no_connect,
    handle_delete_schematic_wire,
)

__all__ = [
    "_LABEL_PIN_CONNECT_TOLERANCE_MM",
    "_scan_all_pin_positions",
    "handle_add_no_connect",
    "handle_add_schematic_hierarchical_label",
    "handle_add_schematic_net_label",
    "handle_add_schematic_sheet",
    "handle_add_schematic_wire",
    "handle_add_sheet_pin",
    "handle_connect_passthrough",
    "handle_connect_to_net",
    "handle_delete_no_connect",
    "handle_delete_schematic_net_label",
    "handle_delete_schematic_wire",
    "handle_edit_schematic_net_label",
    "handle_move_schematic_net_label",
]
