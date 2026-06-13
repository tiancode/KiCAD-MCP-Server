"""ERC handler and its power-label / false-positive heuristics.

Split out of the former handlers/schematic_io.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import sexpdata

from ._project_libs import _merged_project_lib_env, _project_dir_for

if TYPE_CHECKING:
    from pathlib import Path

    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.schematic_io")


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


def _sexp_head(node: Any) -> Optional[str]:
    """The leading symbol of an S-expression list (e.g. 'symbol'), else None."""
    if isinstance(node, list) and node and hasattr(node[0], "value"):
        try:
            return node[0].value()
        except Exception:
            return None
    return None


def _kicad_sym_symbol_index(sym_path: str) -> Dict[str, Any]:
    """Map symbol-name -> S-expr node for every top-level symbol in a .kicad_sym."""
    out: Dict[str, Any] = {}
    try:
        with open(sym_path, encoding="utf-8") as f:
            tree = sexpdata.loads(f.read())
    except (OSError, ValueError):
        return out
    for node in tree[1:] if isinstance(tree, list) else []:
        if isinstance(node, list) and len(node) > 1 and _sexp_head(node) == "symbol":
            out[str(node[1]).strip('"')] = node
    return out


def _embedded_symbols_matching_disk(schematic_path: str, project_dir: "Path") -> set:
    """Bare names of embedded lib_symbols whose definition is identical to the
    resolved on-disk .kicad_sym symbol (ignoring indentation and the 'NICK:'
    name prefix).

    A kicad-cli ``lib_symbol_mismatch`` on one of these is a false positive: the
    embedded copy already matches the library, so the design is correct and
    refresh_schematic_lib_symbols cannot change anything. kicad-cli's headless
    ``LIB_SYMBOL::Compare`` has been observed to flag project-scoped custom libs
    (supplied via the merged sym-lib-table) even when the content is identical.
    Best-effort: returns an empty set on any failure (ERC then behaves as before).
    """
    matches: set = set()
    differs: set = set()
    try:
        from commands.library_symbol import get_symbol_library_manager

        with open(schematic_path, encoding="utf-8") as f:
            sch = sexpdata.loads(f.read())
        lib_symbols = next(
            (e for e in sch if isinstance(e, list) and _sexp_head(e) == "lib_symbols"),
            None,
        )
        if lib_symbols is not None:
            mgr = get_symbol_library_manager(project_path=project_dir)
            disk_index: Dict[str, Dict[str, Any]] = {}
            for entry in lib_symbols[1:]:
                if not (
                    isinstance(entry, list) and len(entry) > 1 and _sexp_head(entry) == "symbol"
                ):
                    continue
                full = str(entry[1]).strip('"')
                if ":" not in full:
                    continue
                nick, bare = full.split(":", 1)
                sym_path = mgr.libraries.get(nick)
                if not sym_path or not os.path.exists(sym_path):
                    continue
                if sym_path not in disk_index:
                    disk_index[sym_path] = _kicad_sym_symbol_index(sym_path)
                disk_node = disk_index[sym_path].get(bare)
                if disk_node is None:
                    continue
                normalized = list(entry)
                normalized[1] = bare  # strip the NICK: prefix so only content differs
                if sexpdata.dumps(normalized) == sexpdata.dumps(disk_node):
                    matches.add(bare)
                else:
                    differs.add(bare)
    except Exception as e:  # best-effort — never break ERC over this
        logger.debug("embedded-vs-disk lib_symbol compare failed: %s", e)
    # The violation message only quotes the symbol name, not the library, so a
    # bare name that matches one lib but differs in another is ambiguous — drop
    # it from the false-positive set rather than risk hiding a genuine mismatch.
    return matches - differs


def _mismatch_is_false_positive(vmsg: str, matches_disk: set) -> bool:
    """True when a lib_symbol_mismatch names a symbol (quoted in the message)
    whose embedded definition already matches disk — i.e. a kicad-cli false
    positive that refresh cannot fix."""
    for name in matches_disk:
        if f"'{name}'" in vmsg or f'"{name}"' in vmsg:
            return True
    return False


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

        # Locate the project root (the dir holding sym-lib-table / *.kicad_pro)
        # once — both the lib_symbols pre-refresh and the merged-config step
        # below resolve project-scoped libraries relative to it.
        from pathlib import Path

        sch_path_obj = Path(schematic_path)
        project_dir = _project_dir_for(schematic_path)

        # Pre-refresh embedded lib_symbols so kicad-cli ERC compares
        # against the actual current library, not an older snapshot left
        # over from earlier placements / a library upgrade.  Failures
        # here are non-fatal — we still run ERC but surface the refresh
        # status in the response so the agent can see what happened.
        lib_symbols_refresh: Optional[Dict[str, Any]] = None
        if bool(params.get("autoRefreshLibSymbols", True)):
            try:
                from commands.dynamic_symbol_loader import DynamicSymbolLoader

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

        # Pre-bound so the response builder below can reference it regardless of
        # whether the merge ran. kicad-cli ERC only reads the GLOBAL
        # sym-lib-table; _merged_project_lib_env merges the project-local table
        # into a throwaway config so project-scoped libs resolve and don't raise
        # spurious "library not in current configuration" warnings.
        merged_project_libs: List[str] = []

        try:
            with _merged_project_lib_env(project_dir) as (erc_env, merged_project_libs):
                if merged_project_libs:
                    logger.info(
                        "ERC: merged %d project sym-lib(s) into temp config (%s)",
                        len(merged_project_libs),
                        ", ".join(merged_project_libs),
                    )
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
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120, env=erc_env
                )

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
            pwrflag_fp_count = 0
            # ``lib_symbol_mismatch``: a genuine drift (embedded snapshot differs
            # from the on-disk .kicad_sym) is fixable with
            # refresh_schematic_lib_symbols. But kicad-cli's headless compare
            # also flags project-scoped custom libs whose embedded def is already
            # identical to disk — a false positive refresh can't fix (it would be
            # a no-op, the loop the user hit). We compare embedded vs disk
            # ourselves: matches → tag false positive (no refresh rec); only real
            # drift is counted and gets the refresh recommendation.
            lib_symbol_mismatch_count = 0
            lib_mismatch_fp_count = 0
            embedded_matches_disk = (
                _embedded_symbols_matching_disk(schematic_path, project_dir)
                if any(v.get("type") == "lib_symbol_mismatch" for v in all_violations)
                else set()
            )

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
                    pwrflag_fp_count += 1
                elif vtype == "lib_symbol_mismatch":
                    if _mismatch_is_false_positive(vmsg, embedded_matches_disk):
                        annotated["likely_false_positive"] = True
                        annotated["reason"] = (
                            "The embedded lib_symbols definition is identical to "
                            "the resolved on-disk .kicad_sym (normalized for "
                            "indentation and the library-nickname prefix); "
                            "kicad-cli's headless library compare flags "
                            "project-scoped libs anyway. The design is correct "
                            "and refresh_schematic_lib_symbols cannot change it."
                        )
                        lib_mismatch_fp_count += 1
                    else:
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
            if pwrflag_fp_count > 0:
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
                            f"{pwrflag_fp_count} violation(s) here are "
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
            # Only GENUINE drift (embedded != disk) is counted above, so it is
            # the only thing that drives the refresh recommendation. Byte-
            # identical mismatches were tagged likely_false_positive and skipped
            # — that is what breaks the no-op "recommend refresh -> re-run" loop
            # the user reported; a genuine drift instead self-resolves after one
            # refresh (which makes embedded == disk, a no-op on the next run).
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
                    # How many of the FPs are lib_symbol_mismatch warnings whose
                    # embedded def already matches disk (kicad-cli compare quirk,
                    # not user-actionable). 0 unless a project-scoped lib tripped it.
                    "lib_symbol_mismatch_false_positives": lib_mismatch_fp_count,
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
            if merged_project_libs:
                response["project_lib_table"] = {
                    "merged": True,
                    "libraries": merged_project_libs,
                    "note": (
                        "Project-local sym-lib-table libraries were merged into a "
                        "temporary config for ERC so kicad-cli resolves them "
                        "(it otherwise reads only the global table)."
                    ),
                }
            return response

        finally:
            if os.path.exists(json_output):
                os.unlink(json_output)

    except subprocess.TimeoutExpired:
        return {"success": False, "message": "ERC timed out after 120 seconds"}
    except Exception as e:
        logger.error(f"Error running ERC: {str(e)}")
        return {"success": False, "message": str(e)}
