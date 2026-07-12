"""
Design rules command implementations for KiCAD interface
"""

import logging
import os
from typing import Any, Dict, Optional

import pcbnew

logger = logging.getLogger("kicad_interface")


class DesignRuleCommands:
    """Handles design rule checking and configuration"""

    def __init__(self, board: Optional[pcbnew.BOARD] = None):
        """Initialize with optional board instance"""
        self.board = board

    def set_design_rules(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Set design rules for the PCB"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            design_settings = self.board.GetDesignSettings()

            # Convert mm to nanometers for KiCAD internal units
            scale = 1000000  # mm to nm

            # Set clearance
            if "clearance" in params:
                design_settings.m_MinClearance = int(params["clearance"] * scale)

            # KiCAD 9.0: Use SetCustom* methods instead of SetCurrent* (which were removed)
            # Track if we set any custom track/via values
            custom_values_set = False

            if "trackWidth" in params:
                design_settings.SetCustomTrackWidth(int(params["trackWidth"] * scale))
                custom_values_set = True

            # Via settings
            if "viaDiameter" in params:
                design_settings.SetCustomViaSize(int(params["viaDiameter"] * scale))
                custom_values_set = True
            if "viaDrill" in params:
                design_settings.SetCustomViaDrill(int(params["viaDrill"] * scale))
                custom_values_set = True

            # KiCAD 9.0: Activate custom track/via values so they become the current values
            if custom_values_set:
                design_settings.UseCustomTrackViaSize(True)

            # Set micro via settings (use properties - methods removed in KiCAD 9.0)
            if "microViaDiameter" in params:
                design_settings.m_MicroViasMinSize = int(params["microViaDiameter"] * scale)
            if "microViaDrill" in params:
                design_settings.m_MicroViasMinDrill = int(params["microViaDrill"] * scale)

            # Set minimum values
            if "minTrackWidth" in params:
                design_settings.m_TrackMinWidth = int(params["minTrackWidth"] * scale)
            if "minViaDiameter" in params:
                design_settings.m_ViasMinSize = int(params["minViaDiameter"] * scale)

            # KiCAD 9.0: m_ViasMinDrill removed - use m_MinThroughDrill instead
            if "minViaDrill" in params:
                design_settings.m_MinThroughDrill = int(params["minViaDrill"] * scale)

            if "minMicroViaDiameter" in params:
                design_settings.m_MicroViasMinSize = int(params["minMicroViaDiameter"] * scale)
            if "minMicroViaDrill" in params:
                design_settings.m_MicroViasMinDrill = int(params["minMicroViaDrill"] * scale)

            # KiCAD 9.0: m_MinHoleDiameter removed - use m_MinThroughDrill
            if "minHoleDiameter" in params:
                design_settings.m_MinThroughDrill = int(params["minHoleDiameter"] * scale)

            # KiCAD 9.0: Added hole clearance settings
            if "holeClearance" in params:
                design_settings.m_HoleClearance = int(params["holeClearance"] * scale)
            if "holeToHoleMin" in params:
                design_settings.m_HoleToHoleMin = int(params["holeToHoleMin"] * scale)

            # Build response with KiCAD 9.0 compatible properties
            # After UseCustomTrackViaSize(True), GetCurrent* returns the custom values
            response_rules = {
                "clearance": design_settings.m_MinClearance / scale,
                "trackWidth": design_settings.GetCurrentTrackWidth() / scale,
                "viaDiameter": design_settings.GetCurrentViaSize() / scale,
                "viaDrill": design_settings.GetCurrentViaDrill() / scale,
                "microViaDiameter": design_settings.m_MicroViasMinSize / scale,
                "microViaDrill": design_settings.m_MicroViasMinDrill / scale,
                "minTrackWidth": design_settings.m_TrackMinWidth / scale,
                "minViaDiameter": design_settings.m_ViasMinSize / scale,
                "minThroughDrill": design_settings.m_MinThroughDrill / scale,
                "minMicroViaDiameter": design_settings.m_MicroViasMinSize / scale,
                "minMicroViaDrill": design_settings.m_MicroViasMinDrill / scale,
                "holeClearance": design_settings.m_HoleClearance / scale,
                "holeToHoleMin": design_settings.m_HoleToHoleMin / scale,
                "viasMinAnnularWidth": design_settings.m_ViasMinAnnularWidth / scale,
            }

            # Persist to the .kicad_pro project JSON.  In KiCad 9/10 the
            # design-rule minimums live in board.design_settings.rules in the
            # project file, NOT the .kicad_pcb — the in-memory mutation above is
            # never written by board.Save().  This read-modify-write is what
            # actually makes the rules survive on disk.
            persisted = self._persist_design_rules_to_project(params)

            return {
                "success": True,
                "message": "Updated design rules",
                "persisted": persisted.get("persisted", False),
                "projectFile": persisted.get("projectFile"),
                "rules": response_rules,
            }

        except Exception as e:
            logger.error(f"Error setting design rules: {str(e)}")
            return {
                "success": False,
                "message": "Failed to set design rules",
                "errorDetails": str(e),
            }

    def _persist_design_rules_to_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Write design-rule minimums to the sibling .kicad_pro JSON.

        Returns ``{"persisted": bool, "projectFile": str | None}``.  Lengths in
        ``params`` are mm floats and are written verbatim (the project JSON
        stores mm, not nm).  Never raises — a persistence failure is reported
        via the flag rather than turning a successful mutation into an error.
        """
        from utils import kicad_pro

        project_file = kicad_pro.project_path_for_board(self.board)
        if not project_file or not os.path.exists(project_file):
            logger.warning(
                "set_design_rules: no .kicad_pro found for board; "
                "rules not persisted (project_file=%s)",
                project_file,
            )
            return {"persisted": False, "projectFile": project_file}

        # MCP/TS param name -> board.design_settings.rules key (all mm floats).
        rule_key_map = {
            "clearance": "min_clearance",
            "minTrackWidth": "min_track_width",
            "minViaDiameter": "min_via_diameter",
            "minViaDrill": "min_through_hole_diameter",
            "minHoleDiameter": "min_through_hole_diameter",
            "minMicroViaDiameter": "min_microvia_diameter",
            "minMicroViaDrill": "min_microvia_drill",
            "holeToHoleMin": "min_hole_to_hole",
            "holeClearance": "min_hole_clearance",
            "copperEdgeClearance": "min_copper_edge_clearance",
        }

        try:
            data, indent = kicad_pro.load_kicad_pro(project_file)

            board = data.get("board")
            if not isinstance(board, dict):
                board = {}
                data["board"] = board
            design_settings = board.get("design_settings")
            if not isinstance(design_settings, dict):
                design_settings = {}
                board["design_settings"] = design_settings
            rules = design_settings.get("rules")
            if not isinstance(rules, dict):
                rules = {}
                design_settings["rules"] = rules

            for param_key, rule_key in rule_key_map.items():
                if param_key in params and params[param_key] is not None:
                    rules[rule_key] = params[param_key]

            # The board's default track/via widths come from the Default net
            # class entry; mirror trackWidth/viaDiameter/viaDrill there so the
            # UI default reflects the change.
            net_settings = kicad_pro._net_settings(data)
            default_overrides = {
                "track_width": params.get("trackWidth"),
                "via_diameter": params.get("viaDiameter"),
                "via_drill": params.get("viaDrill"),
            }
            if any(v is not None for v in default_overrides.values()):
                kicad_pro.upsert_netclass(net_settings, "Default", default_overrides)

            kicad_pro.save_kicad_pro(project_file, data, indent)
            return {"persisted": True, "projectFile": project_file}
        except Exception as e:
            logger.error("set_design_rules: failed to persist to project: %s", e)
            return {"persisted": False, "projectFile": project_file}

    def get_design_rules(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get current design rules - KiCAD 9.0 compatible"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            design_settings = self.board.GetDesignSettings()
            scale = 1000000  # nm to mm

            # Build rules dict with KiCAD 9.0 compatible properties
            rules = {
                # Core clearance and track settings
                "clearance": design_settings.m_MinClearance / scale,
                "trackWidth": design_settings.GetCurrentTrackWidth() / scale,
                "minTrackWidth": design_settings.m_TrackMinWidth / scale,
                # Via settings (current values from methods)
                "viaDiameter": design_settings.GetCurrentViaSize() / scale,
                "viaDrill": design_settings.GetCurrentViaDrill() / scale,
                # Via minimum values
                "minViaDiameter": design_settings.m_ViasMinSize / scale,
                "viasMinAnnularWidth": design_settings.m_ViasMinAnnularWidth / scale,
                # Micro via settings
                "microViaDiameter": design_settings.m_MicroViasMinSize / scale,
                "microViaDrill": design_settings.m_MicroViasMinDrill / scale,
                "minMicroViaDiameter": design_settings.m_MicroViasMinSize / scale,
                "minMicroViaDrill": design_settings.m_MicroViasMinDrill / scale,
                # KiCAD 9.0: Hole and drill settings (replaces removed m_ViasMinDrill and m_MinHoleDiameter)
                "minThroughDrill": design_settings.m_MinThroughDrill / scale,
                "holeClearance": design_settings.m_HoleClearance / scale,
                "holeToHoleMin": design_settings.m_HoleToHoleMin / scale,
                # Other constraints
                "copperEdgeClearance": design_settings.m_CopperEdgeClearance / scale,
                "silkClearance": design_settings.m_SilkClearance / scale,
            }

            return {"success": True, "rules": rules}

        except Exception as e:
            logger.error(f"Error getting design rules: {str(e)}")
            return {
                "success": False,
                "message": "Failed to get design rules",
                "errorDetails": str(e),
            }

    def run_drc(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Run Design Rule Check using kicad-cli"""
        import json
        import subprocess
        import tempfile

        from utils.kicad_cli import c_locale_env

        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            report_path = params.get("reportPath")
            # Caller-overridable timeout (seconds). Defaults to 600s for big boards
            # but smaller MCP transport budgets (e.g. 120s) can lower it explicitly.
            try:
                timeout_sec = int(params.get("timeoutSec", 600))
            except (TypeError, ValueError):
                timeout_sec = 600
            timeout_sec = max(10, min(timeout_sec, 1800))  # clamp to [10, 1800]

            # Cap on the violations returned inline (mirrors run_erc's
            # maxViolations contract: default 30, 0 = all).  The full list —
            # items included — is always written to the violations file.
            try:
                max_violations = int(params.get("maxViolations", 30))
            except (TypeError, ValueError):
                max_violations = 30
            if max_violations <= 0:
                max_violations = 0

            # Get the board file path
            board_file = self.board.GetFileName()
            if not board_file or not os.path.exists(board_file):
                return {
                    "success": False,
                    "message": "Board file not found",
                    "errorDetails": "Cannot run DRC without a saved board file",
                }

            # Find kicad-cli executable
            kicad_cli = self._find_kicad_cli()
            if not kicad_cli:
                return {
                    "success": False,
                    "message": "kicad-cli not found",
                    "errorDetails": "KiCAD CLI tool not found in system. Install KiCAD 8.0+ or set PATH.",
                }

            # Create temporary JSON output file
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
                json_output = tmp.name

            try:
                # Build command
                cmd = [
                    kicad_cli,
                    "pcb",
                    "drc",
                    "--format",
                    "json",
                    "--output",
                    json_output,
                    "--units",
                    "mm",
                    board_file,
                ]

                logger.info(f"Running DRC command (timeout={timeout_sec}s): {' '.join(cmd)}")

                # Force English violation text: kicad-cli reads its UI language
                # from the KiCad config (kicad_common.json), not LC_ALL — see
                # utils.kicad_cli.c_locale_env, which builds a derived English
                # config and keeps LC_ALL=C.
                drc_env = c_locale_env()

                # Run DRC. subprocess.run kills the child on TimeoutExpired.
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                    env=drc_env,
                )

                if result.returncode != 0:
                    logger.error(f"DRC command failed: {result.stderr}")
                    return {
                        "success": False,
                        "message": "DRC command failed",
                        "errorDetails": result.stderr,
                    }

                # Read JSON output
                with open(json_output, "r", encoding="utf-8") as f:
                    drc_data = json.load(f)

                # Parse violations from kicad-cli output
                violations = []
                violation_counts: dict[str, int] = {}
                severity_counts = {"error": 0, "warning": 0, "info": 0}

                for violation in drc_data.get("violations", []):
                    vtype = violation.get("type", "unknown")
                    vseverity = violation.get("severity", "error")

                    # Preserve per-violation items — each carries the offending
                    # object's description and pos.  Without them callers had to
                    # grep the .kicad_pcb to locate offenders.  kicad-cli was
                    # invoked with --units mm, so items[].pos is already mm (the
                    # ERC-side IU/10000 bug does not apply to pcb drc output).
                    items = violation.get("items", [])
                    parsed_items = []
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        entry: Dict[str, Any] = {"description": item.get("description", "")}
                        pos = item.get("pos")
                        if isinstance(pos, dict):
                            entry["pos"] = {
                                "x": pos.get("x", 0),
                                "y": pos.get("y", 0),
                                "unit": "mm",
                            }
                        # Layer info when the report provides it (some KiCad
                        # versions omit it; it then only appears inside the
                        # description text).
                        if "layer" in item:
                            entry["layer"] = item["layer"]
                        elif "layers" in item:
                            entry["layers"] = item["layers"]
                        parsed_items.append(entry)

                    # Location = first item's pos (kicad-cli JSON format)
                    loc_x, loc_y = 0, 0
                    if parsed_items and "pos" in parsed_items[0]:
                        loc_x = parsed_items[0]["pos"]["x"]
                        loc_y = parsed_items[0]["pos"]["y"]

                    violations.append(
                        {
                            "type": vtype,
                            "severity": vseverity,
                            "message": violation.get("description", ""),
                            "location": {
                                "x": loc_x,
                                "y": loc_y,
                                "unit": "mm",
                            },
                            "items": parsed_items,
                        }
                    )

                    # Count violations by type
                    violation_counts[vtype] = violation_counts.get(vtype, 0) + 1

                    # Count by severity
                    if vseverity in severity_counts:
                        severity_counts[vseverity] += 1

                # Determine where to save the violations file
                board_dir = os.path.dirname(board_file)
                board_name = os.path.splitext(os.path.basename(board_file))[0]
                violations_file = os.path.join(board_dir, f"{board_name}_drc_violations.json")

                # Always save violations to JSON file (for large result sets)
                with open(violations_file, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "board": board_file,
                            "timestamp": drc_data.get("date", "unknown"),
                            "total_violations": len(violations),
                            "violation_counts": violation_counts,
                            "severity_counts": severity_counts,
                            "violations": violations,
                        },
                        f,
                        indent=2,
                    )

                # Save text report if requested
                if report_path:
                    report_path = os.path.abspath(os.path.expanduser(report_path))
                    cmd_report = [
                        kicad_cli,
                        "pcb",
                        "drc",
                        "--format",
                        "report",
                        "--output",
                        report_path,
                        "--units",
                        "mm",
                        board_file,
                    ]
                    subprocess.run(
                        cmd_report,
                        capture_output=True,
                        timeout=timeout_sec,
                        env=drc_env,
                    )

                # Return the violations inline (capped) so callers can locate
                # offenders without opening the violations file.  Explicit
                # truncation contract mirrors run_erc: summary reports
                # total vs shown + a truncated flag; maxViolations (0 = all)
                # controls the cap; the file always has the full list.
                total_violations = len(violations)
                if max_violations and total_violations > max_violations:
                    shown_violations = violations[:max_violations]
                else:
                    shown_violations = violations
                truncated = len(shown_violations) < total_violations

                message = f"Found {total_violations} DRC violations"
                if truncated:
                    message += f" (showing {len(shown_violations)} of {total_violations})"

                return {
                    "success": True,
                    "message": message,
                    "summary": {
                        "total": total_violations,
                        "shown": len(shown_violations),
                        "truncated": truncated,
                        "max_violations": max_violations,
                        "by_severity": severity_counts,
                        "by_type": violation_counts,
                    },
                    "violations": shown_violations,
                    "violationsFile": violations_file,
                    "reportPath": report_path if report_path else None,
                }

            finally:
                # Clean up temp JSON file
                if os.path.exists(json_output):
                    os.unlink(json_output)

        except subprocess.TimeoutExpired:
            logger.error(f"DRC command timed out after {timeout_sec}s")
            return {
                "success": False,
                "message": "DRC command timed out",
                "errorDetails": (
                    f"Command took longer than {timeout_sec} seconds; "
                    "raise timeoutSec param for very large boards"
                ),
            }
        except Exception as e:
            logger.error(f"Error running DRC: {str(e)}")
            return {
                "success": False,
                "message": "Failed to run DRC",
                "errorDetails": str(e),
            }

    def _find_kicad_cli(self) -> Optional[str]:
        """Find kicad-cli executable (see utils.kicad_cli.find_kicad_cli)."""
        from utils.kicad_cli import find_kicad_cli

        return find_kicad_cli()

    # Consumed by python/resources/resource_definitions.py (drc_violations
    # resource); the MCP command route was removed as redundant with run_drc.
    def get_drc_violations(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get list of DRC violations

        Note: This command internally uses run_drc() which calls kicad-cli.
        The old BOARD.GetDRCMarkers() API was removed in KiCAD 9.0.
        This implementation provides backward compatibility by parsing kicad-cli output.
        """
        import json

        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            severity = params.get("severity", "all")

            # Run DRC using kicad-cli (this saves violations to JSON file)
            drc_result = self.run_drc({})

            if not drc_result.get("success"):
                return drc_result  # Return the error from run_drc

            # Read violations from the saved JSON file
            violations_file = drc_result.get("violationsFile")
            if not violations_file or not os.path.exists(violations_file):
                return {
                    "success": False,
                    "message": "Violations file not found",
                    "errorDetails": "run_drc did not create violations file",
                }

            # Load violations from file
            with open(violations_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            all_violations = data.get("violations", [])

            # Filter by severity if specified
            if severity != "all":
                filtered_violations = [v for v in all_violations if v.get("severity") == severity]
            else:
                filtered_violations = all_violations

            from utils.pagination import paginate

            filtered_violations, page = paginate(filtered_violations, params)
            return {
                "success": True,
                "violations": filtered_violations,
                "violationsFile": violations_file,  # Include file path for reference
                **page,
            }

        except Exception as e:
            logger.error(f"Error getting DRC violations: {str(e)}")
            return {
                "success": False,
                "message": "Failed to get DRC violations",
                "errorDetails": str(e),
            }
