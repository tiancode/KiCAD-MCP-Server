"""IPC fast-path handlers package.

Re-exports every handler and helper so the dispatcher
(getattr(import_module("handlers.ipc_fastpath"), "handle_<cmd>")), the
``import handlers.ipc_fastpath as fp`` users, and the iface._ipc_<x>
trampoline all keep working after the split.
"""

from ._common import (
    _TO_MM_SCALE,
    extract_xy,
    to_mm,
)
from ._routing import (
    handle_add_net,
    handle_add_via,
    handle_delete_trace,
    handle_get_nets_list,
    handle_query_traces,
    handle_route_arc_trace,
    handle_route_trace,
)
from ._zones import (
    _ipc_board_edge_rect,
    handle_add_copper_pour,
    handle_refill_zones,
)
from ._components import (
    handle_delete_component,
    handle_get_component_list,
    handle_get_component_pads,
    handle_get_component_properties,
    handle_move_component,
    handle_place_component,
    handle_rotate_component,
)
from ._board import (
    handle_add_board_outline,
    handle_add_mounting_hole,
    handle_add_text,
    handle_get_board_info,
    handle_get_layer_list,
    handle_save_project,
    handle_set_board_size,
)

__all__ = [
    "_TO_MM_SCALE",
    "_ipc_board_edge_rect",
    "extract_xy",
    "handle_add_board_outline",
    "handle_add_copper_pour",
    "handle_add_mounting_hole",
    "handle_add_net",
    "handle_add_text",
    "handle_add_via",
    "handle_delete_component",
    "handle_delete_trace",
    "handle_get_board_info",
    "handle_get_component_list",
    "handle_get_component_pads",
    "handle_get_component_properties",
    "handle_get_layer_list",
    "handle_get_nets_list",
    "handle_move_component",
    "handle_place_component",
    "handle_query_traces",
    "handle_refill_zones",
    "handle_rotate_component",
    "handle_route_arc_trace",
    "handle_route_trace",
    "handle_save_project",
    "handle_set_board_size",
    "to_mm",
]
