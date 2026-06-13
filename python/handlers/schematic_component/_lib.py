"""Refresh embedded lib_symbols handler.

Split out of the former handlers/schematic_component.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.schematic_component")


def handle_refresh_schematic_lib_symbols(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Re-inject every embedded ``lib_symbols`` entry from the current
    on-disk ``.kicad_sym`` library, silencing ERC's
    ``lib_symbol_mismatch`` warnings caused by stale snapshots.
    """
    from pathlib import Path

    from commands.dynamic_symbol_loader import DynamicSymbolLoader

    schematic_path = params.get("schematicPath")
    if not schematic_path:
        return {"success": False, "message": "schematicPath is required"}

    sch = Path(schematic_path)
    if not sch.exists():
        return {"success": False, "message": f"Schematic not found: {schematic_path}"}

    # Find a sensible project dir for project-local library resolution.
    derived_project = sch.parent
    for ancestor in sch.parents:
        if (ancestor / "sym-lib-table").exists() or list(ancestor.glob("*.kicad_pro")):
            derived_project = ancestor
            break

    loader = DynamicSymbolLoader(project_path=derived_project)
    return loader.refresh_embedded_lib_symbols(sch)
