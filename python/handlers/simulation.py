"""SPICE simulation handler (ngspice batch mode).

Thin wrapper over commands/simulation.py: kicad-cli exports the spice
netlist, ngspice -b runs it, parsers return structured data. All heavy
lifting (deck building, output parsing, binary resolution) lives in the
command module where it is unit-tested with injected subprocess runners.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger("kicad_interface")


def handle_run_simulation(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Run an ngspice analysis (op/tran/dc/ac) on a schematic."""
    logger.info("Running SPICE simulation")
    try:
        from commands.simulation import run_simulation

        schematic_path = params.get("schematicPath")
        analysis = params.get("analysis")
        if not schematic_path or not analysis:
            return {
                "success": False,
                "message": "schematicPath and analysis are required",
            }
        return run_simulation(
            schematic_path,
            analysis=str(analysis),
            params=params.get("analysisParams"),
            signals=params.get("signals"),
            max_points=int(params.get("maxPoints", 2000)),
            timeout=float(params.get("timeout", 120.0)),
        )
    except Exception as e:  # API boundary; bucket: catch + return
        logger.error(f"Error running simulation: {e}", exc_info=True)
        return {"success": False, "message": f"Failed to run simulation: {e}"}
