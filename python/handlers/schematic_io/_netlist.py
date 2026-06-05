"""Netlist generation and export handlers.

Split out of the former handlers/schematic_io.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Tuple

import pcbnew  # type: ignore[import-not-found]
import sexpdata
from commands.schematic import SchematicManager

from ._project_libs import _merged_project_lib_env, _project_dir_for

if TYPE_CHECKING:
    from pathlib import Path

    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.schematic_io")


def handle_generate_netlist(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Generate netlist from schematic and return structured JSON.

    Uses kicad-cli to export KiCad XML netlist to a temp file, then
    parses it into {components, nets} structure expected by the TS handler.
    """
    import subprocess
    import tempfile
    import xml.etree.ElementTree as ET

    logger.info("Generating netlist from schematic via kicad-cli")
    try:
        schematic_path = params.get("schematicPath")
        if not schematic_path:
            return {"success": False, "message": "Schematic path is required"}
        if not os.path.exists(schematic_path):
            return {"success": False, "message": f"Schematic not found: {schematic_path}"}

        kicad_cli = iface._find_kicad_cli_static()
        if not kicad_cli:
            return {"success": False, "message": "kicad-cli not found in PATH"}

        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            cmd = [
                kicad_cli,
                "sch",
                "export",
                "netlist",
                "--format",
                "kicadxml",
                "--output",
                tmp_path,
                schematic_path,
            ]
            logger.info(f"Running: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

            if result.returncode != 0:
                return {
                    "success": False,
                    "message": f"kicad-cli failed (exit {result.returncode}): {result.stderr.strip()}",
                }

            tree = ET.parse(tmp_path)
            root = tree.getroot()

            components = []
            for comp in root.findall("./components/comp"):
                ref = comp.get("ref", "")
                value = comp.findtext("value", "")
                footprint = comp.findtext("footprint", "")
                components.append({"reference": ref, "value": value, "footprint": footprint})

            nets = []
            for net in root.findall("./nets/net"):
                net_name = net.get("name", "")
                connections = []
                for node in net.findall("node"):
                    connections.append(
                        {
                            "component": node.get("ref", ""),
                            "pin": node.get("pin", ""),
                        }
                    )
                nets.append({"name": net_name, "connections": connections})

            logger.info(f"Generated netlist: {len(components)} components, {len(nets)} nets")
            return {"success": True, "netlist": {"components": components, "nets": nets}}

        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    except FileNotFoundError:
        return {"success": False, "message": "kicad-cli not found in PATH"}
    except subprocess.TimeoutExpired:
        return {"success": False, "message": "kicad-cli timed out after 60 seconds"}
    except Exception as e:
        logger.error(f"Error generating netlist: {e}")
        return {"success": False, "message": str(e)}


def handle_export_netlist(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Export netlist to a file using kicad-cli."""
    import subprocess

    logger.info("Exporting netlist via kicad-cli")
    try:
        schematic_path = params.get("schematicPath")
        output_path = params.get("outputPath")
        fmt = params.get("format", "KiCad")

        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}
        if not output_path:
            return {"success": False, "message": "outputPath is required"}
        if not os.path.exists(schematic_path):
            return {"success": False, "message": f"Schematic not found: {schematic_path}"}

        kicad_cli = iface._find_kicad_cli_static()
        if not kicad_cli:
            return {"success": False, "message": "kicad-cli not found in PATH"}

        fmt_map = {
            "KiCad": "kicadxml",
            "Spice": "spice",
            "Cadstar": "cadstar",
            "OrcadPCB2": "orcadpcb2",
        }
        cli_format = fmt_map.get(fmt, "kicadxml")

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        cmd = [
            kicad_cli,
            "sch",
            "export",
            "netlist",
            "--format",
            cli_format,
            "--output",
            output_path,
            schematic_path,
        ]
        logger.info(f"Running: {' '.join(cmd)}")
        # Merge the project-local sym-lib-table so the exported netlist's
        # <libraries> block includes project-scoped custom libs (kicad-cli
        # otherwise reads only the global table and silently omits them).
        with _merged_project_lib_env(_project_dir_for(schematic_path)) as (env, merged_libs):
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)

        if result.returncode == 0:
            resp: Dict[str, Any] = {"success": True, "outputPath": output_path, "format": fmt}
            if merged_libs:
                resp["mergedProjectLibraries"] = merged_libs
            return resp
        else:
            return {
                "success": False,
                "message": f"kicad-cli failed (exit {result.returncode}): {result.stderr.strip()}",
            }

    except FileNotFoundError:
        return {"success": False, "message": "kicad-cli not found in PATH"}
    except subprocess.TimeoutExpired:
        return {"success": False, "message": "kicad-cli timed out after 60 seconds"}
    except Exception as e:
        logger.error(f"Error exporting netlist: {e}")
        return {"success": False, "message": str(e)}
