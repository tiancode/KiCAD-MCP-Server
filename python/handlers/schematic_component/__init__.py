"""Schematic Component handlers package.

Re-exports every handler and helper so the dispatcher and existing
``from handlers.schematic_component import ...`` imports keep working.
"""

# Re-exported so tests can patch handlers.schematic_component.SchematicManager
# (the former single module exposed it). Patching a method on this class object
# is global, so the per-area submodules see the patch.
from commands.schematic import SchematicManager

from ._placement import (
    _SCHEMATIC_GRID_MM,
    _apply_grid_snap,
    _snap_to_schematic_grid,
    handle_add_schematic_component,
    handle_annotate_schematic,
    handle_delete_schematic_component,
    handle_move_schematic_component,
    handle_rotate_schematic_component,
)
from ._properties import (
    handle_edit_schematic_component,
    handle_get_schematic_component,
    handle_remove_schematic_component_property,
    handle_set_schematic_component_property,
)
from ._duplicate import (
    handle_duplicate_schematic_component,
)
from ._lib import (
    handle_refresh_schematic_lib_symbols,
)

__all__ = [
    "_SCHEMATIC_GRID_MM",
    "_apply_grid_snap",
    "_snap_to_schematic_grid",
    "handle_add_schematic_component",
    "handle_annotate_schematic",
    "handle_delete_schematic_component",
    "handle_duplicate_schematic_component",
    "handle_edit_schematic_component",
    "handle_get_schematic_component",
    "handle_move_schematic_component",
    "handle_refresh_schematic_lib_symbols",
    "handle_remove_schematic_component_property",
    "handle_rotate_schematic_component",
    "handle_set_schematic_component_property",
]
