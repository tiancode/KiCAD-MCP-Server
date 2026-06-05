"""Wire connectivity package.

Re-exports every function/constant so existing
``from commands.wire_connectivity import ...`` imports keep working.
"""

# Re-exported so tests can patch commands.wire_connectivity.PinLocator (the
# former single module exposed it). Patching a method on this class object is
# global, so the per-area submodules see it.
from commands.pin_locator import PinLocator

from ._parsing import (
    PWRFLAG_LABEL_SENTINEL,
    _IU_PER_MM,
    _load_sexp,
    _parse_labels_sexp,
    _parse_symbol_instances_sexp,
    _parse_virtual_connections,
    _parse_wires,
    _parse_wires_sexp,
    _point_on_segment,
    _to_iu,
    is_pwrflag_label,
)
from ._traversal import (
    _build_adjacency,
    _discover_sub_sheets,
    _find_connected_wires,
    _find_pins_on_net,
    _process_single_sheet,
)
from ._queries import (
    count_pins_on_net,
    get_connections_for_net,
    get_net_at_point,
    get_wire_connections,
    list_floating_labels,
)

__all__ = [
    "PWRFLAG_LABEL_SENTINEL",
    "_IU_PER_MM",
    "_build_adjacency",
    "_discover_sub_sheets",
    "_find_connected_wires",
    "_find_pins_on_net",
    "_load_sexp",
    "_parse_labels_sexp",
    "_parse_symbol_instances_sexp",
    "_parse_virtual_connections",
    "_parse_wires",
    "_parse_wires_sexp",
    "_point_on_segment",
    "_process_single_sheet",
    "_to_iu",
    "count_pins_on_net",
    "get_connections_for_net",
    "get_net_at_point",
    "get_wire_connections",
    "is_pwrflag_label",
    "list_floating_labels",
]
