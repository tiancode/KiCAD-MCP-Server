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

# Default cap on the number of violations returned in the response list. The
# summary always carries the full totals, so the cap only bounds the payload
# size — callers raise it (or pass 0 for "all") via the ``maxViolations`` param.
_DEFAULT_MAX_VIOLATIONS = 30


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


# ---------------------------------------------------------------------------
# KiCad 10.x position-based net resolution.
#
# On KiCad 10.0.4 the ``power_pin_not_driven`` ERC JSON carries neither a net
# name in the description nor a per-item ``net`` field — only the pin position
# (``items[].pos``).  ``_violation_mentions_power_label`` therefore returns
# False and the violation is never tagged, so a label-driven power rail with no
# PWR_FLAG shows up as a hard error with no recommendation (the reported bug).
#
# To recover the tag we resolve the offending pin's net ourselves: walk the
# wire network out from the (rescaled) pin position to the label that names the
# net, then apply the same "labeled power net with no PWR_FLAG driver" rule the
# 9.x path uses.  All coordinates are schematic millimetres.
# ---------------------------------------------------------------------------

# Direct pin<->label spatial match: a connect_to_net stub is one 0.1in grid
# (2.54 mm); allow two so an off-by-a-grid label still resolves without jumping
# to an unrelated net.
_LABEL_MATCH_TOLERANCE_MM = 5.08
# Coincident-point tolerance when following the wire graph / matching a label
# that sits exactly on a wire vertex.
_WIRE_JOIN_EPS_MM = 0.05


def _is_power_ish_name(name: Optional[str]) -> bool:
    """True when a net name looks like a power rail (VCC / GND / +3V3 ...)."""
    if not name:
        return False
    upper = name.upper()
    return any(pat in upper for pat in _COMMON_POWER_NET_PATTERNS)


def _collect_net_label_geometry(schematic_path: str):
    """Parse a .kicad_sch for the geometry needed to resolve a pin's net.

    Returns ``(labels, wires, pwr_flag_positions)`` where

    * ``labels`` is ``[(name, x, y), ...]`` for local / global / hierarchical
      labels plus placed ``power:<NET>`` symbols (the symbol Value names the
      net),
    * ``wires`` is ``[(x1, y1, x2, y2), ...]`` segment endpoints, and
    * ``pwr_flag_positions`` is ``[(x, y), ...]`` for every placed
      ``power:PWR_FLAG`` symbol.

    All coordinates are schematic millimetres (the same frame as the rescaled
    ERC ``pos``).  Best-effort: returns empty lists on any parse failure.
    """
    labels: List = []
    wires: List = []
    pwr_flags: List = []
    try:
        with open(schematic_path, "r", encoding="utf-8") as f:
            tree = sexpdata.loads(f.read())
    except Exception as e:
        logger.debug("Could not parse %s for net geometry: %s", schematic_path, e)
        return labels, wires, pwr_flags

    def _sym(x: Any) -> str:
        try:
            return x.value() if hasattr(x, "value") else str(x)
        except Exception:
            return ""

    def _direct_at(node: Any):
        # The instance/label placement is a *direct* child ``(at x y ...)``;
        # nested property/pin ``at`` fields are ignored.
        for child in node[1:]:
            if isinstance(child, list) and child and _sym(child[0]) == "at" and len(child) >= 3:
                try:
                    return float(child[1]), float(child[2])
                except (TypeError, ValueError):
                    return None
        return None

    def _walk(node: Any) -> None:
        if not isinstance(node, list) or not node:
            return
        head = _sym(node[0])
        if head in ("label", "global_label", "hierarchical_label"):
            if len(node) >= 2 and isinstance(node[1], str):
                at = _direct_at(node)
                if at:
                    labels.append((node[1], at[0], at[1]))
        elif head == "wire":
            pts = next(
                (c for c in node[1:] if isinstance(c, list) and c and _sym(c[0]) == "pts"),
                None,
            )
            if pts is not None:
                xy = [
                    c
                    for c in pts[1:]
                    if isinstance(c, list) and c and _sym(c[0]) == "xy" and len(c) >= 3
                ]
                if len(xy) >= 2:
                    try:
                        wires.append(
                            (
                                float(xy[0][1]),
                                float(xy[0][2]),
                                float(xy[1][1]),
                                float(xy[1][2]),
                            )
                        )
                    except (TypeError, ValueError):
                        pass
        elif head == "symbol":
            # Only *placed* symbols carry a direct ``lib_id`` child; lib_symbols
            # definitions name themselves via node[1] and are skipped here.
            lib_id_text = None
            value_text = None
            for child in node[1:]:
                if isinstance(child, list) and child:
                    chead = _sym(child[0])
                    if chead == "lib_id" and len(child) >= 2 and isinstance(child[1], str):
                        lib_id_text = child[1]
                    elif (
                        chead == "property"
                        and len(child) >= 3
                        and child[1] == "Value"
                        and isinstance(child[2], str)
                    ):
                        value_text = child[2]
            at = _direct_at(node)
            if lib_id_text and at and lib_id_text.lower().startswith("power:"):
                if lib_id_text.lower().endswith(":pwr_flag"):
                    pwr_flags.append((at[0], at[1]))
                elif value_text:
                    labels.append((value_text, at[0], at[1]))
        for child in node[1:]:
            _walk(child)

    _walk(tree)
    return labels, wires, pwr_flags


def _nearest_net_label(x: float, y: float, labels: List, tolerance: float) -> Optional[str]:
    """Name of the label closest to (x, y) within ``tolerance`` mm, else None."""
    best: Optional[str] = None
    best_d: Optional[float] = None
    for name, lx, ly in labels:
        d = ((lx - x) ** 2 + (ly - y) ** 2) ** 0.5
        if d <= tolerance and (best_d is None or d < best_d):
            best_d = d
            best = name
    return best


def _resolve_net_via_geometry(
    x: Optional[float], y: Optional[float], labels: List, wires: List
) -> Optional[str]:
    """Net name for the pin at (x, y) mm, resolved from schematic geometry.

    Walks the wire network out from the pin (each ``connect_to_net`` stub is a
    ``wire pin -> label``) and returns the name of any label sitting on a
    reachable vertex.  Falls back to the nearest label within a couple of grid
    units when the pin is not on a traceable vertex.  Returns None when nothing
    plausibly names the net (e.g. a genuinely floating pin).
    """
    if x is None or y is None:
        return None
    visited: set = set()
    stack = [(x, y)]
    while stack:
        cx, cy = stack.pop()
        key = (round(cx, 3), round(cy, 3))
        if key in visited:
            continue
        visited.add(key)
        hit = _nearest_net_label(cx, cy, labels, _WIRE_JOIN_EPS_MM)
        if hit:
            return hit
        for x1, y1, x2, y2 in wires:
            if abs(cx - x1) <= _WIRE_JOIN_EPS_MM and abs(cy - y1) <= _WIRE_JOIN_EPS_MM:
                stack.append((x2, y2))
            elif abs(cx - x2) <= _WIRE_JOIN_EPS_MM and abs(cy - y2) <= _WIRE_JOIN_EPS_MM:
                stack.append((x1, y1))
    return _nearest_net_label(x, y, labels, _LABEL_MATCH_TOLERANCE_MM)


def _classify_power_pin_fp(
    vtype: str,
    vmsg: str,
    items: Optional[list],
    loc: Dict[str, Any],
    power_label_names: set,
    power_ish_labels: set,
    net_labels: List,
    wires: List,
    nets_with_pwr_flag: set,
    has_any_pwr_flag: bool,
) -> Optional[Dict[str, Any]]:
    """Decide whether a power-pin-not-driven violation is the PWR_FLAG
    false-positive class.

    Returns ``None`` when it is not (a genuine issue, or not this class), else
    ``{"net": str | None, "reason": str, "rec_nets": [str, ...]}`` where
    ``rec_nets`` are the nets to fold into the aggregate add_pwr_flag
    recommendation.

    Detection paths:

    * **KiCad 9** — a net name / power label is present in the description or a
      per-item field (``_violation_mentions_power_label``).  Unchanged.
    * **KiCad 10** — the message is the generic "Input Power pin not driven by
      any Output Power pins" with no net anywhere.  The pin position (``loc``,
      already rescaled to mm) is walked out along the wires to the label that
      names the net; a power-ish net with no PWR_FLAG driver is the FP.
    * **Fallback** — the position could not be resolved, but the schematic has
      power rails and zero PWR_FLAG symbols, so every power-input pin is
      undriven for exactly the PWR_FLAG reason.
    """
    if not _is_power_not_driven(vtype, vmsg):
        return None

    if _violation_mentions_power_label(vmsg, power_label_names, items):
        net = _extract_net_from_violation(vmsg, items)
        return {
            "net": net,
            "reason": (
                "A power label on this net is the only driver. "
                "kicad-cli ERC expects a PWR_FLAG symbol on power "
                "inputs even when labels make the netlist correct."
            ),
            "rec_nets": [net] if net else [],
        }

    # KiCad 10: no net name in the description or items — resolve it from the
    # pin position via schematic geometry.
    if loc:
        net = _resolve_net_via_geometry(loc.get("x"), loc.get("y"), net_labels, wires)
        if net and not _is_power_ish_name(net):
            # A power-input pin wired onto a non-power (signal) net is a real
            # design error, not the PWR_FLAG false positive — leave it counted.
            return None
        if net and net in nets_with_pwr_flag:
            # The net already carries a PWR_FLAG yet ERC still complains: do not
            # mask what is most likely a genuine wiring problem.
            return None
        if net:
            return {
                "net": net,
                "reason": (
                    "ERC reported no net name (KiCad 10.x). The flagged pin at "
                    f"({loc.get('x'):.2f}, {loc.get('y'):.2f}) mm resolves to power "
                    f"net '{net}', whose only driver is a label (no PWR_FLAG). "
                    "kicad-cli ERC flags label-driven power inputs even when the "
                    "netlist is correct; add a PWR_FLAG on this net to clear it."
                ),
                "rec_nets": [net],
            }

    # Fallback: position unresolved.  Only safe to tag when there is no PWR_FLAG
    # anywhere AND the schematic actually has power rails — then this whole
    # violation class is the PWR_FLAG false positive.  The named nets are a best
    # guess, so the reason says so.
    if not has_any_pwr_flag and power_ish_labels:
        return {
            "net": None,
            "reason": (
                "ERC reported no net name and the pin position could not be "
                "matched to a wire/label, but the schematic has power rails ("
                + ", ".join(sorted(power_ish_labels))
                + ") and no PWR_FLAG symbols at all, so label-driven power "
                "inputs are flagged for the PWR_FLAG reason. Heuristic: confirm "
                "the pin is on a power rail before relying on this."
            ),
            "rec_nets": sorted(power_ish_labels),
        }
    return None


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

    from utils.kicad_cli import c_locale_env

    try:
        schematic_path = params.get("schematicPath")
        if not schematic_path or not os.path.exists(schematic_path):
            return {
                "success": False,
                "message": "Schematic file not found",
                "errorDetails": f"Path does not exist: {schematic_path}",
            }

        # How many violations to include in the returned list. The full counts
        # always live in ``summary`` so truncation is explicit; a non-positive
        # value means "no cap" (return every violation).
        try:
            max_violations = int(params.get("maxViolations", _DEFAULT_MAX_VIOLATIONS))
        except (TypeError, ValueError):
            max_violations = _DEFAULT_MAX_VIOLATIONS
        if max_violations < 0:
            max_violations = 0

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
                # Force the C locale so kicad-cli's violation descriptions come
                # back in stable English (they otherwise follow the user's UI
                # locale, breaking downstream pattern-matching). Layer it on top
                # of erc_env so the merged KICAD_CONFIG_HOME, if any, survives.
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=c_locale_env(erc_env),
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

            # KiCad 10.x emits power_pin_not_driven with NO net name (only a pin
            # position), so the 9.x message/items check can't tag it.  Parse the
            # schematic geometry once — labels, wires and PWR_FLAG placements —
            # so those violations can be resolved to a net by position below.
            # Only pay for it when such a violation is actually present.
            need_net_geometry = any(
                _is_power_not_driven(v.get("type", ""), v.get("description", ""))
                for v in all_violations
            )
            if need_net_geometry:
                net_labels, net_wires, pwr_flag_positions = _collect_net_label_geometry(
                    schematic_path
                )
            else:
                net_labels, net_wires, pwr_flag_positions = [], [], []
            power_ish_labels = {n for n in power_label_names if _is_power_ish_name(n)}
            has_any_pwr_flag = bool(pwr_flag_positions)
            # Nets that already carry a PWR_FLAG driver — a power_pin_not_driven
            # on one of these is NOT the false positive (do not mask it).
            nets_with_pwr_flag: set = set()
            for _pfx, _pfy in pwr_flag_positions:
                _pf_net = _resolve_net_via_geometry(_pfx, _pfy, net_labels, net_wires)
                if _pf_net:
                    nets_with_pwr_flag.add(_pf_net)

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
                power_fp = _classify_power_pin_fp(
                    vtype,
                    vmsg,
                    items,
                    loc,
                    power_label_names,
                    power_ish_labels,
                    net_labels,
                    net_wires,
                    nets_with_pwr_flag,
                    has_any_pwr_flag,
                )
                if power_fp is not None:
                    annotated["likely_false_positive"] = True
                    annotated["reason"] = power_fp["reason"]
                    if power_fp["net"]:
                        annotated["net"] = power_fp["net"]
                    for _net in power_fp["rec_nets"]:
                        if _net:
                            pwrflag_target_nets.add(_net)
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

            # Cap the returned list so a huge ERC doesn't blow the MCP text
            # budget. The cap is explicit: summary reports total vs shown and a
            # `truncated` flag; `maxViolations` (0 = all) controls it end-to-end.
            total_violations = len(violations)
            if max_violations and total_violations > max_violations:
                shown_violations = violations[:max_violations]
            else:
                shown_violations = violations
            truncated = len(shown_violations) < total_violations

            response: Dict[str, Any] = {
                "success": True,
                "message": (
                    f"ERC complete: {total_violations} violation(s)"
                    + (
                        f" ({tagged_false_positives} tagged likely_false_positive)"
                        if tagged_false_positives
                        else ""
                    )
                ),
                "summary": {
                    # Headline numbers FIRST — real_errors (excluding PWR_FLAG
                    # false positives) is the single most important field, so it
                    # and the error/warning totals lead the payload.
                    "real_errors": real_by_severity.get("error", 0),
                    "real_warnings": real_by_severity.get("warning", 0),
                    "errors": severity_counts.get("error", 0),
                    "warnings": severity_counts.get("warning", 0),
                    "total": total_violations,
                    # Explicit truncation contract: how many of `total` are in
                    # the returned `violations` list and whether it was capped.
                    "shown": len(shown_violations),
                    "truncated": truncated,
                    "max_violations": max_violations,
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
                    # Top-level remediation hints: one entry per known
                    # fix-class.  Currently only PWR_FLAG.  Empty list
                    # means "no auto-actionable suggestion".
                    "recommendations": recommendations,
                },
                "violations": shown_violations,
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
