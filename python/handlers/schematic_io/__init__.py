"""Schematic Io handlers package.

Re-exports every handler and helper so the dispatcher
(getattr(import_module("handlers.schematic_io"), "handle_<cmd>")) and existing
``from handlers.schematic_io import ...`` imports keep working after the split.
"""

from ._io import (
    handle_sync_schematic_to_board,
    handle_export_schematic_pdf,
    handle_create_schematic,
)
from ._netlist import handle_export_netlist, handle_generate_netlist
from ._project_libs import (
    _project_dir_for,
    _build_project_lib_config_home,
    _merged_project_lib_env,
)
from ._erc import (
    _COMMON_POWER_NET_PATTERNS,
    _collect_power_label_names,
    _is_power_not_driven,
    _NET_FROM_DESCRIPTION,
    _extract_net_from_violation,
    _violation_mentions_power_label,
    _sexp_head,
    _kicad_sym_symbol_index,
    _embedded_symbols_matching_disk,
    _mismatch_is_false_positive,
    handle_run_erc,
)

__all__ = [
    "_COMMON_POWER_NET_PATTERNS",
    "_NET_FROM_DESCRIPTION",
    "_build_project_lib_config_home",
    "_collect_power_label_names",
    "_embedded_symbols_matching_disk",
    "_extract_net_from_violation",
    "_is_power_not_driven",
    "_kicad_sym_symbol_index",
    "_merged_project_lib_env",
    "_mismatch_is_false_positive",
    "_project_dir_for",
    "_sexp_head",
    "_violation_mentions_power_label",
    "handle_create_schematic",
    "handle_export_netlist",
    "handle_export_schematic_pdf",
    "handle_generate_netlist",
    "handle_run_erc",
    "handle_sync_schematic_to_board",
]
