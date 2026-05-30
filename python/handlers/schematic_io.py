"""
Schematic Io handlers, extracted from kicad_interface.py.

See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import pcbnew  # type: ignore[import-not-found]
import sexpdata
from commands.schematic import SchematicManager

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


_NET_FROM_DESCRIPTION = re.compile(r"on net\s+([A-Za-z_+\-][\w+\-/]*)", re.IGNORECASE)


def _extract_net_from_violation(vmsg: str, items: Optional[list] = None) -> Optional[str]:
    """Best-effort net-name extraction from a pin_not_driven violation.

    kicad-cli's description usually contains "on net <NAME>"; per-item
    fields sometimes carry the net under ``netname`` / ``net``.  Returns
    the first match or None.
    """
    if vmsg:
        m = _NET_FROM_DESCRIPTION.search(vmsg)
        if m:
            return m.group(1)
    for item in items or ():
        if not isinstance(item, dict):
            continue
        for key in ("netname", "net", "net_name"):
            val = item.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def _violation_mentions_power_label(
    vmsg: str, label_names: set, items: Optional[list] = None
) -> bool:
    """True when the violation references any of the schematic's labels.

    Checks both the description string AND the per-item structured fields
    that kicad-cli emits — net name often lives in ``items[].net`` /
    ``items[].netname`` rather than in the human-readable description,
    so a description-only check would miss it.

    Also passes when *any* of the conventional power-rail names appears in
    the message — kicad-cli sometimes omits the net name and just says
    'Input Power pin', in which case the presence of *some* power label
    in the schematic is still a strong false-positive signal.
    """

    def _matches(haystack: str) -> bool:
        if not haystack:
            return False
        for name in label_names:
            if name and name in haystack:
                return True
        upper = haystack.upper()
        return any(pat in upper for pat in _COMMON_POWER_NET_PATTERNS)

    if _matches(vmsg or ""):
        return True
    for item in items or ():
        # Try several plausible field names — kicad-cli has used
        # different ones across versions.
        for key in ("net", "netname", "net_name", "name"):
            val = item.get(key) if isinstance(item, dict) else None
            if isinstance(val, str) and _matches(val):
                return True
    return False


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
    """Run Electrical Rules Check on a schematic via kicad-cli.

    By default ``refresh_schematic_lib_symbols`` runs first so the
    embedded ``lib_symbols`` block matches the current on-disk
    ``.kicad_sym`` library — every MCP-placed component otherwise
    triggers a ``lib_symbol_mismatch`` warning the moment our injection
    format drifts from KiCad's expected canonical form (which is exactly
    what the user reported after sync_schematic_to_board).  Pass
    ``autoRefreshLibSymbols: false`` to skip the pre-refresh.
    """
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

        # Pre-refresh embedded lib_symbols so kicad-cli ERC compares
        # against the actual current library, not an older snapshot left
        # over from earlier placements / a library upgrade.  Failures
        # here are non-fatal — we still run ERC but surface the refresh
        # status in the response so the agent can see what happened.
        lib_symbols_refresh: Optional[Dict[str, Any]] = None
        if bool(params.get("autoRefreshLibSymbols", True)):
            try:
                from pathlib import Path

                from commands.dynamic_symbol_loader import DynamicSymbolLoader

                sch_path_obj = Path(schematic_path)
                project_dir = sch_path_obj.parent
                for ancestor in sch_path_obj.parents:
                    if (ancestor / "sym-lib-table").exists() or list(ancestor.glob("*.kicad_pro")):
                        project_dir = ancestor
                        break
                lib_symbols_refresh = DynamicSymbolLoader(
                    project_path=project_dir
                ).refresh_embedded_lib_symbols(sch_path_obj)
                if lib_symbols_refresh.get("refreshed"):
                    logger.info(
                        "Pre-ERC refresh updated %d lib_symbols entry(ies): %s",
                        len(lib_symbols_refresh["refreshed"]),
                        ", ".join(lib_symbols_refresh["refreshed"]),
                    )
            except Exception as e:
                # Pre-refresh is best-effort; never let it fail the ERC call.
                logger.warning("Pre-ERC lib_symbols refresh failed: %s", e)
                lib_symbols_refresh = {"success": False, "message": str(e)}

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

            # KiCad-CLI ERC JSON unit bug (observed on 10.0.3): the header
            # claims ``coordinate_units: "mm"`` but ``items[].pos`` is
            # actually serialised as schematic internal-units / 10000 — a
            # symbol at (129.84, 94.92) mm comes back as (1.2984, 0.9492).
            # Re-scale to mm here so the location lines up with the
            # schematic coordinate system the caller queried with.  Drop
            # this multiplier when kicad upstream fixes the writer.
            _ERC_POS_TO_MM = 100.0

            # Collect net names that triggered PWR_FLAG-fixable violations so
            # the response can surface a single actionable "add PWR_FLAG"
            # recommendation instead of leaving the agent to interpret each
            # tagged violation individually.
            pwrflag_target_nets: set = set()
            # Same idea for ``lib_symbol_mismatch`` — every hit means the
            # schematic's embedded snapshot drifted from the on-disk
            # .kicad_sym and ``refresh_schematic_lib_symbols`` is the
            # one-shot fix.
            lib_symbol_mismatch_count = 0

            for v in all_violations:
                vseverity = v.get("severity", "error")
                vtype = v.get("type", "unknown")
                vmsg = v.get("description", "")
                items = v.get("items", [])
                loc = {}
                if items and "pos" in items[0]:
                    loc = {
                        "x": items[0]["pos"].get("x", 0) * _ERC_POS_TO_MM,
                        "y": items[0]["pos"].get("y", 0) * _ERC_POS_TO_MM,
                        "unit": "mm",
                    }
                annotated = {
                    "type": vtype,
                    "severity": vseverity,
                    "message": vmsg,
                    "location": loc,
                }
                if _is_power_not_driven(vtype, vmsg) and _violation_mentions_power_label(
                    vmsg, power_label_names, items
                ):
                    annotated["likely_false_positive"] = True
                    annotated["reason"] = (
                        "A power label on this net is the only driver. "
                        "kicad-cli ERC expects a PWR_FLAG symbol on power "
                        "inputs even when labels make the netlist correct."
                    )
                    extracted_net = _extract_net_from_violation(vmsg, items)
                    if extracted_net:
                        annotated["net"] = extracted_net
                        pwrflag_target_nets.add(extracted_net)
                elif vtype == "lib_symbol_mismatch":
                    lib_symbol_mismatch_count += 1
                violations.append(annotated)
                if vseverity in severity_counts:
                    severity_counts[vseverity] += 1

            tagged_false_positives = sum(1 for v in violations if v.get("likely_false_positive"))

            # Build a single structured recommendation when PWR_FLAG-fixable
            # violations are present.  The agent gets the affected nets and
            # a concrete next step instead of having to interpret the
            # per-violation `reason` strings.  Empty list when there's
            # nothing to recommend.
            recommendations: List[Dict[str, Any]] = []
            if tagged_false_positives > 0:
                # Fall back to the labels we discovered in the schematic if
                # extraction didn't produce a net list (older kicad-cli
                # versions phrase the message differently).
                nets_for_action = (
                    sorted(pwrflag_target_nets)
                    if pwrflag_target_nets
                    else sorted(name for name in power_label_names if name)
                )
                recommendations.append(
                    {
                        "kind": "add_pwr_flag",
                        "nets": nets_for_action,
                        "message": (
                            "kicad-cli ERC requires an explicit PWR_FLAG symbol "
                            "on every power-input net even when a power label "
                            "(GND / VCC / +3V3 ...) makes the netlist correct. "
                            f"{tagged_false_positives} violation(s) here are "
                            "this exact false-positive class."
                        ),
                        "action": (
                            "Add one power:PWR_FLAG symbol per net listed in "
                            "'nets', wire each flag to its rail, and re-run "
                            "run_erc.  Example tool call: "
                            "add_schematic_component(schematicPath=..., "
                            "component={library:'power', type:'PWR_FLAG', "
                            "reference:'#FLG?', x:..., y:...}) then "
                            "add_schematic_wire to connect it to the net."
                        ),
                    }
                )
            if lib_symbol_mismatch_count > 0:
                recommendations.append(
                    {
                        "kind": "refresh_lib_symbols",
                        "count": lib_symbol_mismatch_count,
                        "message": (
                            f"{lib_symbol_mismatch_count} ``lib_symbol_mismatch`` "
                            "warning(s): the schematic's embedded lib_symbols "
                            "block drifted from the current .kicad_sym on "
                            "disk (typical after a KiCad upgrade or hand-edit "
                            "of a library file)."
                        ),
                        "action": (
                            "Call ``refresh_schematic_lib_symbols(schematicPath=...)`` "
                            "to re-inject the current library copy into the "
                            "schematic, then re-run run_erc."
                        ),
                    }
                )
            # Demote tagged FPs out of the headline severity buckets so
            # `by_severity.error` reflects real errors only — otherwise
            # the agent sees "2 errors" even when both are pedantic
            # PWR_FLAG complaints on a netlist that's actually correct,
            # which (the user reported) makes them doubt their work.
            real_by_severity = dict(severity_counts)
            for v in violations:
                if v.get("likely_false_positive"):
                    sev = v.get("severity", "error")
                    if sev in real_by_severity and real_by_severity[sev] > 0:
                        real_by_severity[sev] -= 1
            # Sort real issues first, FPs last — same severity preserved
            # within each group.  Keeps the most actionable items at the
            # top of the list when the agent scans it.
            violations.sort(key=lambda v: 1 if v.get("likely_false_positive") else 0)

            response: Dict[str, Any] = {
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
                    # by_severity now counts ONLY real issues per bucket;
                    # tagged FPs are surfaced separately via
                    # likely_false_positives + raw_by_severity.
                    "by_severity": real_by_severity,
                    "raw_by_severity": severity_counts,
                    "likely_false_positives": tagged_false_positives,
                    "real_errors": real_by_severity.get("error", 0),
                    # Top-level remediation hints: one entry per known
                    # fix-class.  Currently only PWR_FLAG.  Empty list
                    # means "no auto-actionable suggestion".
                    "recommendations": recommendations,
                },
                "violations": violations,
            }
            if lib_symbols_refresh is not None:
                response["lib_symbols_refresh"] = lib_symbols_refresh
            return response

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
