"""
Schematic Io handlers, extracted from kicad_interface.py.

See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

import pcbnew  # type: ignore[import-not-found]
import sexpdata
from commands.schematic import SchematicManager
from commands.wire_manager import WireManager

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


# Net names whose presence as a label on the offending net strongly suggests
# kicad-cli's "Input Power pin not driven" is a false positive — the netlist
# is correct, kicad-cli just won't accept a label as a driver without an
# explicit PWR_FLAG symbol.
_COMMON_POWER_NET_PATTERNS = (
    "VCC",
    "VDD",
    "VEE",
    "VSS",
    "VBUS",
    "GND",
    "AGND",
    "DGND",
    "PGND",
    "+3V3",
    "+5V",
    "+12V",
    "-12V",
    "+24V",
    "-24V",
)


def _collect_power_label_names(schematic_path: str) -> set:
    """Return the set of net-label names that look like power nets.

    Best-effort: parses the .kicad_sch S-expression tree for label /
    global_label / hierarchical_label entries plus power: lib_ids and
    returns the names. Used to tag pin_not_driven ERC false positives.
    """
    names: set = set()
    try:
        with open(schematic_path, "r", encoding="utf-8") as f:
            tree = sexpdata.loads(f.read())
    except Exception as e:
        logger.debug(f"Could not parse {schematic_path} for power labels: {e}")
        return names

    def _sym(x: Any) -> str:
        try:
            return x.value() if hasattr(x, "value") else str(x)
        except Exception:
            return ""

    def _walk(node: Any) -> None:
        if not isinstance(node, list) or not node:
            return
        head = _sym(node[0])
        if head in ("label", "global_label", "hierarchical_label"):
            if len(node) >= 2 and isinstance(node[1], str):
                names.add(node[1])
        # power:VCC style symbols expose their net name as the value field
        elif head == "symbol":
            value_text = None
            lib_id_text = None
            for child in node[1:]:
                if isinstance(child, list) and child:
                    chead = _sym(child[0])
                    if chead == "lib_id" and len(child) >= 2 and isinstance(child[1], str):
                        lib_id_text = child[1]
                    elif chead == "property" and len(child) >= 3 and isinstance(child[1], str):
                        if child[1] == "Value" and isinstance(child[2], str):
                            value_text = child[2]
            if lib_id_text and lib_id_text.lower().startswith("power:") and value_text:
                names.add(value_text)
        for child in node[1:]:
            _walk(child)

    _walk(tree)
    return names


def _is_power_not_driven(vtype: str, vmsg: str) -> bool:
    """Heuristic match for kicad-cli's 'Input Power pin not driven' family."""
    if not vtype and not vmsg:
        return False
    haystack = f"{vtype} {vmsg}".lower()
    return (
        "pin_not_driven" in haystack
        or "power_pin_not_driven" in haystack
        or ("power" in haystack and "not driven" in haystack)
    )


def _violation_mentions_power_label(vmsg: str, label_names: set) -> bool:
    """True when the violation text references any of the schematic's labels.

    Also passes when *any* of the conventional power-rail names appears in
    the message — kicad-cli sometimes omits the net name and just says
    'Input Power pin', in which case the presence of *some* power label
    in the schematic is still a strong false-positive signal.
    """
    if not vmsg:
        return False
    msg = vmsg
    for name in label_names:
        if name and name in msg:
            return True
    upper = msg.upper()
    return any(pat in upper for pat in _COMMON_POWER_NET_PATTERNS)


def handle_sync_schematic_to_board(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Sync schematic netlist to PCB board (equivalent to KiCAD F8 'Update PCB from Schematic').
    Reads net connections from the schematic and assigns them to the matching pads in the PCB.
    """
    logger.info("Syncing schematic to board")
    try:
        from pathlib import Path

        schematic_path = params.get("schematicPath")
        board_path = params.get("boardPath")

        # Determine board to work with
        board = None
        if board_path:
            board = iface._safe_load_board(board_path)
            if board is None:
                return {
                    "success": False,
                    "message": f"Could not load board from {board_path}",
                    "errorDetails": (
                        "pcbnew.LoadBoard failed or returned a dehydrated "
                        "SWIG proxy that could not be recovered"
                    ),
                }
        elif iface.board:
            board = iface.board
            board_path = board.GetFileName() if not board_path else board_path
        else:
            return {
                "success": False,
                "message": "No board loaded. Use open_project first or provide boardPath.",
            }

        if not board_path:
            board_path = board.GetFileName()

        # Determine schematic path if not provided
        if not schematic_path:
            sch = Path(board_path).with_suffix(".kicad_sch")
            if sch.exists():
                schematic_path = str(sch)
            else:
                project_dir = Path(board_path).parent
                sch_files = list(project_dir.glob("*.kicad_sch"))
                if sch_files:
                    schematic_path = str(sch_files[0])

        if not schematic_path or not Path(schematic_path).exists():
            return {
                "success": False,
                "message": f"Schematic not found. Provide schematicPath. Tried: {schematic_path}",
            }

        # Build hierarchical pad→net map (walks all sub-sheets)
        pad_net_map, net_names = iface._build_hierarchical_pad_net_map(schematic_path)

        # Add missing footprints from the schematic to the board *before*
        # we add nets and assign pads — F8 in KiCad does this implicitly
        # ("Update PCB from Schematic"), but our previous implementation
        # only mutated nets, leaving newly-added schematic symbols with no
        # PCB footprint at all.
        added_footprints, skipped_footprints = iface._add_missing_footprints_from_schematic(
            board, schematic_path
        )

        # Add all nets to board
        netinfo = board.GetNetInfo()
        nets_by_name = netinfo.NetsByName()
        added_nets = []
        for net_name in net_names:
            if not nets_by_name.has_key(net_name):
                net_item = pcbnew.NETINFO_ITEM(board, net_name)
                board.Add(net_item)
                added_nets.append(net_name)

        # Refresh nets map after additions
        netinfo = board.GetNetInfo()
        nets_by_name = netinfo.NetsByName()

        # Assign nets to pads (now also covers any footprints we just added)
        assigned_pads = 0
        unmatched = []
        for fp in board.GetFootprints():
            ref = fp.GetReference()
            for pad in fp.Pads():
                pad_num = pad.GetNumber()
                key = (ref, str(pad_num))
                if key in pad_net_map:
                    net_name = pad_net_map[key]
                    if nets_by_name.has_key(net_name):
                        pad.SetNet(nets_by_name[net_name])
                        assigned_pads += 1
                else:
                    unmatched.append(f"{ref}/{pad_num}")

        # Route through the iface helper so the in-memory signature tracks
        # the new on-disk hash; otherwise the dispatcher's follow-up
        # _auto_save_board() sees a mismatch and refuses the next write.
        iface._save_board_and_record(board, board_path)

        # If board was loaded fresh, update internal reference
        if params.get("boardPath"):
            iface.board = board
            iface._update_command_handlers()

        logger.info(
            f"sync_schematic_to_board: {len(added_nets)} nets added, "
            f"{len(added_footprints)} footprints added, {assigned_pads} pads assigned"
        )
        # Surface the grid-placement contract so agents know each new
        # footprint landed at a distinct position and which positions
        # they were — previously they all stacked at (0, 0) and the
        # caller had to issue N move_component calls before anything
        # was visible.
        layout_note: Optional[str] = None
        if added_footprints:
            positions = [fp.get("position") for fp in added_footprints if fp.get("position")]
            if positions:
                xs = [p["x_mm"] for p in positions]
                ys = [p["y_mm"] for p in positions]
                layout_note = (
                    f"{len(added_footprints)} new footprints grid-placed: "
                    f"x in [{min(xs)}, {max(xs)}] mm, "
                    f"y in [{min(ys)}, {max(ys)}] mm. "
                    f"Call move_component on each ref to reposition."
                )
        return {
            "success": True,
            "message": (
                f"PCB updated from schematic: {len(added_footprints)} footprints added, "
                f"{len(added_nets)} nets added, {assigned_pads} pads assigned"
            ),
            "nets_added": added_nets,
            "nets_total": len(net_names),
            "pads_assigned": assigned_pads,
            "unmatched_pads_sample": unmatched[:10],
            "footprints_added": added_footprints,
            "footprints_skipped": skipped_footprints,
            "layout_note": layout_note,
        }

    except Exception as e:
        logger.error(f"Error in sync_schematic_to_board: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode == 0:
            return {"success": True, "outputPath": output_path, "format": fmt}
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


def handle_run_erc(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Run Electrical Rules Check on a schematic via kicad-cli"""
    logger.info("Running ERC on schematic")
    import os
    import subprocess
    import tempfile

    try:
        schematic_path = params.get("schematicPath")
        if not schematic_path or not os.path.exists(schematic_path):
            return {
                "success": False,
                "message": "Schematic file not found",
                "errorDetails": f"Path does not exist: {schematic_path}",
            }

        kicad_cli = iface.design_rule_commands._find_kicad_cli()
        if not kicad_cli:
            return {
                "success": False,
                "message": "kicad-cli not found",
                "errorDetails": "Install KiCAD 8.0+ or add kicad-cli to PATH.",
            }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            json_output = tmp.name

        try:
            cmd = [
                kicad_cli,
                "sch",
                "erc",
                "--format",
                "json",
                "--output",
                json_output,
                schematic_path,
            ]
            logger.info(f"Running ERC command: {' '.join(cmd)}")

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

            # kicad-cli returns non-zero when ERC violations are found —
            # this is normal, not an error.  Only fail when no JSON was
            # produced (genuine CLI failure).
            if not os.path.exists(json_output) or os.path.getsize(json_output) == 0:
                logger.error(f"ERC command produced no output: {result.stderr}")
                return {
                    "success": False,
                    "message": "ERC command failed - no output produced",
                    "errorDetails": result.stderr,
                }

            with open(json_output, "r", encoding="utf-8") as f:
                erc_data = json.load(f)

            violations = []
            severity_counts = {"error": 0, "warning": 0, "info": 0}

            # KiCad 9 nests violations under sheets[].violations
            # instead of (or in addition to) the top-level violations
            # array used by KiCad 8.
            all_violations = erc_data.get("violations", [])
            for sheet in erc_data.get("sheets", []):
                all_violations.extend(sheet.get("violations", []))

            # Collect net-label names from the schematic so we can tag
            # `pin_not_driven` violations that are almost certainly false
            # positives — power input pins driven only by a label rather than
            # a PWR_FLAG, which kicad-cli ERC has historically flagged even
            # though the netlist is correct.
            power_label_names = _collect_power_label_names(schematic_path)

            for v in all_violations:
                vseverity = v.get("severity", "error")
                vtype = v.get("type", "unknown")
                vmsg = v.get("description", "")
                items = v.get("items", [])
                loc = {}
                if items and "pos" in items[0]:
                    loc = {
                        "x": items[0]["pos"].get("x", 0),
                        "y": items[0]["pos"].get("y", 0),
                    }
                annotated = {
                    "type": vtype,
                    "severity": vseverity,
                    "message": vmsg,
                    "location": loc,
                }
                if _is_power_not_driven(vtype, vmsg) and _violation_mentions_power_label(
                    vmsg, power_label_names
                ):
                    annotated["likely_false_positive"] = True
                    annotated["reason"] = (
                        "A power label on this net is the only driver. "
                        "kicad-cli ERC expects a PWR_FLAG symbol on power "
                        "inputs even when labels make the netlist correct."
                    )
                violations.append(annotated)
                if vseverity in severity_counts:
                    severity_counts[vseverity] += 1

            tagged_false_positives = sum(1 for v in violations if v.get("likely_false_positive"))
            return {
                "success": True,
                "message": (
                    f"ERC complete: {len(violations)} violation(s)"
                    + (
                        f" ({tagged_false_positives} tagged likely_false_positive)"
                        if tagged_false_positives
                        else ""
                    )
                ),
                "summary": {
                    "total": len(violations),
                    "by_severity": severity_counts,
                    "likely_false_positives": tagged_false_positives,
                },
                "violations": violations,
            }

        finally:
            if os.path.exists(json_output):
                os.unlink(json_output)

    except subprocess.TimeoutExpired:
        return {"success": False, "message": "ERC timed out after 120 seconds"}
    except Exception as e:
        logger.error(f"Error running ERC: {str(e)}")
        return {"success": False, "message": str(e)}


def handle_export_schematic_svg(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Export schematic to SVG using kicad-cli"""
    logger.info("Exporting schematic SVG")
    import glob
    import shutil
    import subprocess

    try:
        schematic_path = params.get("schematicPath")
        output_path = params.get("outputPath")

        if not schematic_path or not output_path:
            return {
                "success": False,
                "message": "schematicPath and outputPath are required",
            }

        if not os.path.exists(schematic_path):
            return {
                "success": False,
                "message": f"Schematic not found: {schematic_path}",
            }

        # kicad-cli's --output flag for SVG export expects a directory, not a file path.
        # The output file is auto-named based on the schematic name.
        output_dir = os.path.dirname(output_path)
        if not output_dir:
            output_dir = "."

        os.makedirs(output_dir, exist_ok=True)

        cmd = [
            "kicad-cli",
            "sch",
            "export",
            "svg",
            schematic_path,
            "-o",
            output_dir,
        ]

        if params.get("blackAndWhite"):
            cmd.append("--black-and-white")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode != 0:
            return {
                "success": False,
                "message": f"kicad-cli failed: {result.stderr}",
            }

        # kicad-cli names the file after the schematic, so find the generated SVG
        svg_files = glob.glob(os.path.join(output_dir, "*.svg"))
        if not svg_files:
            return {
                "success": False,
                "message": "No SVG file produced by kicad-cli",
            }

        generated_svg = svg_files[0]

        # Move/rename to the user-specified output path if it differs
        if os.path.abspath(generated_svg) != os.path.abspath(output_path):
            shutil.move(generated_svg, output_path)

        return {"success": True, "file": {"path": output_path}}

    except FileNotFoundError:
        return {"success": False, "message": "kicad-cli not found in PATH"}
    except Exception as e:
        logger.error(f"Error exporting schematic SVG: {e}")
        return {"success": False, "message": str(e)}


def handle_export_schematic_pdf(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Export schematic to PDF"""
    logger.info("Exporting schematic to PDF")
    try:
        schematic_path = params.get("schematicPath")
        output_path = params.get("outputPath")

        if not schematic_path:
            return {"success": False, "message": "Schematic path is required"}
        if not output_path:
            return {"success": False, "message": "Output path is required"}

        if not os.path.exists(schematic_path):
            return {
                "success": False,
                "message": f"Schematic not found: {schematic_path}",
            }

        import subprocess

        cmd = [
            "kicad-cli",
            "sch",
            "export",
            "pdf",
            "--output",
            output_path,
            schematic_path,
        ]

        if params.get("blackAndWhite"):
            cmd.insert(-1, "--black-and-white")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode == 0:
            return {"success": True, "file": {"path": output_path}}
        else:
            return {
                "success": False,
                "message": f"kicad-cli failed: {result.stderr}",
            }

    except FileNotFoundError:
        return {"success": False, "message": "kicad-cli not found in PATH"}
    except Exception as e:
        logger.error(f"Error exporting schematic to PDF: {str(e)}")
        return {"success": False, "message": str(e)}


def handle_load_schematic(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Load an existing schematic"""
    logger.info("Loading schematic")
    try:
        filename = params.get("filename")

        if not filename:
            return {"success": False, "message": "Filename is required"}

        schematic = SchematicManager.load_schematic(filename)
        success = schematic is not None

        if success:
            metadata = SchematicManager.get_schematic_metadata(schematic)
            return {"success": success, "metadata": metadata}
        else:
            return {"success": False, "message": "Failed to load schematic"}
    except Exception as e:
        logger.error(f"Error loading schematic: {str(e)}")
        return {"success": False, "message": str(e)}


def handle_create_schematic(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new schematic"""
    logger.info("Creating schematic")
    try:
        # Support multiple parameter naming conventions for compatibility:
        # - TypeScript tools use: name, path
        # - Python schema uses: filename, title
        # - Legacy uses: projectName, path, metadata
        project_name = params.get("projectName") or params.get("name") or params.get("title")

        # Handle filename parameter - it may contain full path
        filename = params.get("filename")
        if filename:
            # If filename provided, extract name and path from it
            if filename.endswith(".kicad_sch"):
                filename = filename[:-10]  # Remove .kicad_sch extension
            path = os.path.dirname(filename) or "."
            project_name = project_name or os.path.basename(filename)
        else:
            path = params.get("path", ".")
        metadata = params.get("metadata", {})

        if not project_name:
            return {
                "success": False,
                "message": "Schematic name is required. Provide 'name', 'projectName', or 'filename' parameter.",
            }

        sch_path = path if path and path != "." else None
        schematic = SchematicManager.create_schematic(
            project_name, path=sch_path, metadata=metadata
        )
        base_name = (
            project_name if project_name.endswith(".kicad_sch") else f"{project_name}.kicad_sch"
        )
        normalized_path = path or "."
        file_path = os.path.join(normalized_path, base_name)
        success = SchematicManager.save_schematic(schematic, file_path)

        return {"success": success, "file_path": file_path}
    except Exception as e:
        logger.error(f"Error creating schematic: {str(e)}")
        return {"success": False, "message": str(e)}
