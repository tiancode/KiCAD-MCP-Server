"""LCSC / JLCPCB → KiCAD symbol+footprint import via easyeda2kicad.

The other JLCPCB tools (``search_jlcpcb_parts`` / ``get_jlcpcb_part`` …) only
query the parts *database*; they never produce a placeable KiCAD symbol, so
for any IC without a KiCAD stock symbol the user has to hand-build one with
``create_symbol``.  This module closes that gap.

Given an LCSC part number it shells out to ``easyeda2kicad`` (``python -m
easyeda2kicad``) to generate a real ``.kicad_sym`` symbol and ``.pretty``
footprint into a **shared cache library** at ``~/.kicad-mcp/easyeda.kicad_sym``
(+ ``easyeda.pretty/``), registers the ``easyeda`` nickname in the user-global
``sym-lib-table`` / ``fp-lib-table``, and reports the symbol name so
``add_schematic_component(library="easyeda", componentName=…)`` can place it.

easyeda2kicad's exit code is unreliable (it prints ``[ERROR] … already
exists`` yet still exits 0), so success is determined by re-parsing the
library and locating the symbol whose ``LCSC Part`` property matches the
requested id — not by the return code.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import sexpdata
from utils.sexpr import find_block_end, rewrite_pin_blocks

# Back-compat alias: the quote-aware paren matcher used to live here as
# ``_match_paren`` before it moved to ``utils.sexpr.find_block_end``. Kept so
# callers/tests referencing ``easyeda_import._match_paren`` keep resolving.
_match_paren = find_block_end

logger = logging.getLogger("kicad_interface")

# Shared cache library: ~/.kicad-mcp/easyeda.kicad_sym  +  ~/.kicad-mcp/easyeda.pretty/
EASYEDA_LIB_NICKNAME = "easyeda"
_CACHE_DIR = Path.home() / ".kicad-mcp"
_LIB_BASE = _CACHE_DIR / EASYEDA_LIB_NICKNAME  # no extension
SYMBOL_LIB_PATH = _LIB_BASE.with_suffix(".kicad_sym")
FOOTPRINT_LIB_DIR = Path(str(_LIB_BASE) + ".pretty")

_LCSC_RE = re.compile(r"^C\d+$")

# Pin-type inference (S7). easyeda2kicad emits every pin as electrical type
# ``unspecified``, so ERC can't check power driving AND floods pin_to_pin
# "Unspecified … connected" warnings — 35 of them on the GD32 E2E board, none
# clearable through the server. We infer a sensible type for every pin from its
# NAME (the EasyEDA source's own ``electric`` attribute is already flattened to
# ``unspecified`` by easyeda2kicad, so all that survives in the .kicad_sym is the
# name and the blanket type). The fallback is ``passive`` — NOT ``unspecified`` —
# because passive connects to anything without ERC noise, exactly how generic
# KiCad parts behave, so no pin is ever left as an ERC landmine.
#
# Power rails: name PREFIXES for the V*/GND* families (VDD_3 etc.) …
_POWER_PIN_PREFIXES = (
    "VDD",  # + VDDA, VDDIO … (startswith)
    "VCC",
    "VBAT",
    "AVDD",  # analog supply (doesn't start with VDD)
    "V3P3",  # explicit 3.3 V rail
    "5V",  # explicit 5 V rail (5V0, 5VSB …)
    "VSS",  # + VSSA (startswith)
    "GND",
    "AGND",  # analog ground
    "PGND",  # power ground
)
# … and EXACT names for the exposed-pad rails (prefix-matching "EP" would eat
# EPWM/EPROM-style signal names, so these must match the whole name).
_POWER_PIN_EXACT = ("EP", "EPAD")

# GPIO / bus lines → bidirectional. Prefixes plus the P<port><n> MCU pattern
# (PA0, PB12, PC13 …). Serial data/clock lines are bidirectional buses.
_GPIO_PIN_PREFIXES = ("RX", "TX", "SDA", "SCL", "MISO", "MOSI", "SCK", "IO")
_GPIO_PORT_RE = re.compile(r"^P[A-G]\d")

# Driver pins. OUT* (incl. DOUT/LOUT/ROUT audio outs) → output; IN*
# (incl. DIN/FMIN/AIN) → input.
_OUTPUT_PIN_PREFIXES = ("DOUT", "LOUT", "ROUT", "OUT")
_INPUT_PIN_PREFIXES = ("DIN", "FMIN", "AIN", "IN")

# Explicit no-connects (exact only — "NCS"/"NC_EN" etc. are real signals).
_NO_CONNECT_NAMES = ("NC", "DNC", "N/C", "NC/DNC")

_PIN_HEADER_RE = re.compile(r"\(pin\s+(\S+)\s+(\S+)")
_PIN_NAME_RE = re.compile(r'\(name\s+"([^"]*)"')


class EasyEdaImportError(RuntimeError):
    """A user-facing failure importing an LCSC part (network/tool/parse)."""


def _is_power_pin_name(name: str) -> bool:
    """True when a pin name unambiguously denotes a power rail.

    Matches the V*/GND* PREFIXES (VDD_3, VSSA, GND1 …) and the exposed-pad
    EXACT names (EP, EPAD). Case-insensitive; empty → False.
    """
    if not name:
        return False
    n = name.strip().upper()
    return n.startswith(_POWER_PIN_PREFIXES) or n in _POWER_PIN_EXACT


def _infer_pin_type(name: str, current_type: str) -> str:
    """Infer a KiCad electrical type for a pin from its NAME.

    ``current_type`` is preserved whenever easyeda2kicad already set something
    other than ``unspecified`` (its rare correct inferences aren't clobbered).
    Otherwise the NAME is classified; the fallback is ``passive`` (never
    ``unspecified``) so every pin becomes ERC-quiet — passive connects to
    anything without a pin_to_pin warning, matching how generic KiCad parts
    behave.
    """
    if current_type and current_type != "unspecified":
        return current_type
    n = (name or "").strip().upper()
    if not n:
        return "passive"
    if n in _NO_CONNECT_NAMES:
        return "no_connect"
    if _is_power_pin_name(n):
        return "power_in"
    if n.startswith(_GPIO_PIN_PREFIXES) or _GPIO_PORT_RE.match(n):
        return "bidirectional"
    if n.startswith(_OUTPUT_PIN_PREFIXES):
        return "output"
    if n.startswith(_INPUT_PIN_PREFIXES):
        return "input"
    return "passive"


def _retype_single_pin(pin_block: str) -> "tuple[str, int]":
    """Return (rewritten_pin_block, changed) for one ``(pin …)`` s-expression.

    Only the electrical-type token is touched, and only when the inferred type
    differs from the one already present.
    """
    header = _PIN_HEADER_RE.match(pin_block)
    if not header:
        return pin_block, 0
    name_match = _PIN_NAME_RE.search(pin_block)
    name = name_match.group(1) if name_match else ""
    current = header.group(1)
    inferred = _infer_pin_type(name, current)
    if inferred != current:
        rewritten = pin_block[: header.start(1)] + inferred + pin_block[header.end(1) :]
        return rewritten, 1
    return pin_block, 0


def _rewrite_inferred_pins(block: str) -> "tuple[str, int]":
    """Retype every pin inside a symbol block to its inferred electrical type.

    Delegates the quote-aware ``(pin …)`` walk to ``sexpr.rewrite_pin_blocks``
    so only real s-expression openings are matched, never a substring inside a
    quoted value.
    """
    changed = 0

    def _transform(pin_block: str) -> str:
        nonlocal changed
        new_pin, ch = _retype_single_pin(pin_block)
        changed += ch
        return new_pin

    return rewrite_pin_blocks(block, _transform), changed


def _symbol_span(content: str, symbol_name: str) -> "tuple[int, int] | None":
    """Char span of the top-level ``(symbol "<name>" …)`` block, or None.

    Matches the exact top-level name (the trailing ``"`` excludes sub-symbols
    like ``<name>_1_1``).
    """
    marker = f'(symbol "{symbol_name}"'
    start = content.find(marker)
    if start == -1:
        return None
    return start, find_block_end(content, start)


def _count_symbol_units(lib_path: Path, symbol_name: str) -> int:
    """Number of numbered units the symbol defines (>=1).

    A multi-unit part draws each unit in a ``<name>_<unit>_<style>`` sub-symbol;
    unit 0 (common graphics) is not counted.
    """
    try:
        content = lib_path.read_text(encoding="utf-8")
    except OSError:
        return 1
    span = _symbol_span(content, symbol_name)
    if span is None:
        return 1
    block = content[span[0] : span[1]]
    units = {
        int(m.group(1))
        for m in re.finditer(r'\(symbol\s+"' + re.escape(symbol_name) + r'_(\d+)_\d+"', block)
    }
    units.discard(0)
    return len(units) or 1


def _apply_pin_type_inference(lib_path: Path, symbol_name: str) -> Dict[str, Any]:
    """Retype every ``unspecified`` pin of ``symbol_name`` to an inferred type.

    Rewrites only within the target symbol's block (leaving other cached parts
    untouched), re-parses the whole file to confirm it is still valid, then
    writes atomically (the cache library is shared). A no-op when nothing
    changes. Returns ``{"changed": n, ...}``.
    """
    try:
        content = lib_path.read_text(encoding="utf-8")
    except OSError:
        return {"changed": 0, "skipped": "unreadable"}

    span = _symbol_span(content, symbol_name)
    if span is None:
        return {"changed": 0, "skipped": "symbol_not_found"}

    start, end = span
    new_block, changed = _rewrite_inferred_pins(content[start:end])
    if changed == 0:
        return {"changed": 0}

    new_content = content[:start] + new_block + content[end:]
    try:
        sexpdata.loads(new_content)  # re-parse to confirm validity before commit
    except Exception as e:  # never write a library we just broke
        logger.warning(
            f"Pin-type inference for {symbol_name} produced invalid s-expression; "
            f"leaving the library unchanged: {e}"
        )
        return {"changed": 0, "skipped": "validation_failed"}

    tmp = lib_path.with_name(lib_path.name + ".pintypes.tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.replace(lib_path)
    logger.info(f"Inferred electrical type for {changed} pin(s) of {symbol_name}")
    return {"changed": changed}


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------
def _normalize_lcsc(lcsc: str) -> str:
    """Normalise an LCSC id to canonical ``C<digits>`` form.

    Accepts a bare number ("7593") or a lower-case id; raises ValueError on
    anything that is not a plausible LCSC part number.
    """
    s = str(lcsc).strip().upper()
    if not s:
        raise ValueError("LCSC part number is required (e.g. C7593)")
    if not s.startswith("C"):
        s = "C" + s
    if not _LCSC_RE.match(s):
        raise ValueError(f"Invalid LCSC part number {lcsc!r} (expected e.g. C7593)")
    return s


# ---------------------------------------------------------------------------
# Library parsing
# ---------------------------------------------------------------------------
def _parse_symbols(lib_path: Path) -> List[Dict[str, Any]]:
    """Return the top-level symbols of a .kicad_sym as ``[{name, properties}]``.

    Sub-unit symbols (``Foo_0_1``) are nested inside their parent and are not
    returned — only the directly-placeable top-level symbols.
    """
    if not lib_path.exists():
        return []
    try:
        tree = sexpdata.loads(lib_path.read_text(encoding="utf-8"))
    except Exception as e:  # malformed/partial cache — treat as empty
        logger.warning(f"Could not parse {lib_path}: {e}")
        return []
    out: List[Dict[str, Any]] = []
    for item in tree[1:] if isinstance(tree, list) else []:
        if not (isinstance(item, list) and len(item) >= 2 and str(item[0]) == "symbol"):
            continue
        props: Dict[str, str] = {}
        for sub in item[2:]:
            if isinstance(sub, list) and len(sub) >= 3 and str(sub[0]) == "property":
                props[str(sub[1])] = str(sub[2])
        out.append({"name": str(item[1]), "properties": props})
    return out


# ---------------------------------------------------------------------------
# Library-table registration (user-global sym-lib-table / fp-lib-table)
# ---------------------------------------------------------------------------
def _global_kicad_config_dirs() -> List[Path]:
    """Candidate user-global KiCad config dirs, newest version first.

    Mirrors ``dynamic_symbol_loader._global_sym_lib_table_paths`` so Flatpak /
    macOS-sandboxed installs resolve the same place the placement pipeline
    reads from.
    """
    home = Path.home()
    versions = ["10.0", "9.0", "8.0"]
    bases: List[Path] = []
    if os.name == "nt":
        bases.append(home / "AppData" / "Roaming" / "kicad")
    else:
        bases.append(home / ".config" / "kicad")  # native Linux
        bases.append(home / ".var" / "app" / "org.kicad.KiCad" / "config" / "kicad")  # Flatpak
        bases.append(home / "Library" / "Preferences" / "kicad")  # macOS native
        bases.append(  # macOS sandboxed
            home
            / "Library"
            / "Containers"
            / "org.kicad.KiCad"
            / "Data"
            / "Library"
            / "Preferences"
            / "kicad"
        )
    return [base / v for base in bases for v in versions]


def _resolve_global_config_dir() -> Path:
    """Pick the config dir to register libraries in.

    Prefers a dir that already has a ``sym-lib-table``; then any existing
    config-version dir; otherwise creates the newest candidate.
    """
    dirs = _global_kicad_config_dirs()
    for d in dirs:
        if (d / "sym-lib-table").exists():
            return d
    for d in dirs:
        if d.exists():
            return d
    dirs[0].mkdir(parents=True, exist_ok=True)
    return dirs[0]


def _ensure_table_entry(
    table_path: Path, root_tag: str, nickname: str, uri: str, descr: str
) -> bool:
    """Ensure ``table_path`` registers ``nickname`` → ``uri``.

    Returns True if a new entry was added, False if the nickname was already
    present.  Creates the table file if missing.
    """
    if table_path.exists():
        content = table_path.read_text(encoding="utf-8")
    else:
        content = f"({root_tag}\n  (version 7)\n)\n"

    if f'(name "{nickname}")' in content:
        return False  # already registered under this nickname

    entry = (
        f'  (lib (name "{nickname}")(type "KiCad")(uri "{uri}")' f'(options "")(descr "{descr}"))\n'
    )
    idx = content.rfind(")")
    if idx == -1:
        raise EasyEdaImportError(f"Malformed library table: {table_path}")
    new_content = content[:idx] + entry + content[idx:]

    table_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = table_path.with_name(table_path.name + ".tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.replace(table_path)
    logger.info(f"Registered '{nickname}' in {table_path}")
    return True


def _register_libraries() -> Dict[str, Any]:
    cfg = _resolve_global_config_dir()
    sym_table = cfg / "sym-lib-table"
    fp_table = cfg / "fp-lib-table"

    sym_added = _ensure_table_entry(
        sym_table,
        "sym_lib_table",
        EASYEDA_LIB_NICKNAME,
        str(SYMBOL_LIB_PATH),
        "LCSC/JLCPCB parts imported via easyeda2kicad",
    )
    fp_added = False
    if FOOTPRINT_LIB_DIR.exists():
        fp_added = _ensure_table_entry(
            fp_table,
            "fp_lib_table",
            EASYEDA_LIB_NICKNAME,
            str(FOOTPRINT_LIB_DIR),
            "LCSC/JLCPCB footprints imported via easyeda2kicad",
        )
    return {
        "sym_lib_table": str(sym_table),
        "fp_lib_table": str(fp_table),
        "sym_added": sym_added,
        "fp_added": fp_added,
    }


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
def _run(cmd: List[str], timeout: float) -> "subprocess.CompletedProcess[str]":
    """Run easyeda2kicad; isolated here so tests can inject a fake runner."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _build_response(lcsc: str, sym: Dict[str, Any], *, fetched: bool) -> Dict[str, Any]:
    name = sym["name"]
    props = sym["properties"]
    registered = _register_libraries()
    lib_id = f"{EASYEDA_LIB_NICKNAME}:{name}"
    units = _count_symbol_units(SYMBOL_LIB_PATH, name)
    # The tool takes symbol="lib:name" — NOT library=/componentName= (F12b).
    next_hint = f'Place it with add_schematic_component(symbol="{lib_id}")'
    if units > 1:
        next_hint += (
            f". NOTE: this is a MULTI-UNIT symbol ({units} units); that call places only "
            f"unit 1. Pass placeAllUnits=true to place every unit at once, or repeat with "
            f"unit=2..{units}. Pins on an unplaced unit have no location and cannot be "
            f"labeled or connected."
        )
    return {
        "success": True,
        "lcsc": lcsc,
        "library": EASYEDA_LIB_NICKNAME,
        "symbol": name,
        "lib_id": lib_id,
        "units": units,
        "footprint": props.get("Footprint"),
        "value": props.get("Value"),
        "mpn": props.get("MPN"),
        "manufacturer": props.get("Manufacturer"),
        "datasheet": props.get("Datasheet"),
        "symbol_lib_path": str(SYMBOL_LIB_PATH),
        "footprint_lib_dir": str(FOOTPRINT_LIB_DIR),
        "fetched": fetched,
        "already_cached": not fetched,
        "registered": registered,
        "next": next_hint,
    }


def _maybe_infer_pin_types(symbol_name: str, infer_pin_types: bool) -> int:
    """Run power-pin inference when enabled; return the number of pins retyped."""
    if not infer_pin_types:
        return 0
    return _apply_pin_type_inference(SYMBOL_LIB_PATH, symbol_name).get("changed", 0)


def import_lcsc_part(
    lcsc_number: str,
    *,
    overwrite: bool = False,
    timeout: float = 90.0,
    infer_pin_types: bool = True,
) -> Dict[str, Any]:
    """Import an LCSC/JLCPCB part as a KiCAD symbol + footprint.

    Idempotent: if the part is already in the cache library and ``overwrite``
    is False, the network/tool call is skipped and the cached symbol is
    returned.  Always (re-)registers the ``easyeda`` nickname so a library
    that exists on disk but is missing from the lib-table is repaired.

    ``infer_pin_types`` (default True) post-processes the imported symbol,
    replacing easyeda2kicad's blanket ``unspecified`` type on every pin with a
    type inferred from the pin NAME: power rails (VDD*/GND*/VBAT/EP…) →
    ``power_in``, GPIO/bus lines (PA0/SDA/SCK…) → ``bidirectional``, OUT*/IN*
    drivers → ``output``/``input``, and everything else → ``passive`` (never
    left ``unspecified``). This clears the pin_to_pin "Unspecified … connected"
    ERC warnings that flood an all-unspecified import (S7). Any type
    easyeda2kicad already set correctly is preserved; the rewrite is
    re-validated before it is written atomically.

    Raises ``EasyEdaImportError`` for user-facing failures and ``ValueError``
    for a malformed LCSC id.
    """
    lcsc = _normalize_lcsc(lcsc_number)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    symbols_before = _parse_symbols(SYMBOL_LIB_PATH)
    cached = next((s for s in symbols_before if s["properties"].get("LCSC Part") == lcsc), None)
    if cached and not overwrite:
        # Heal an already-cached part imported before pin-typing existed.
        changed = _maybe_infer_pin_types(cached["name"], infer_pin_types)
        resp = _build_response(lcsc, cached, fetched=False)
        resp["pin_types_inferred"] = changed
        return resp

    cmd = [
        sys.executable,
        "-m",
        "easyeda2kicad",
        "--lcsc_id",
        lcsc,
        "--symbol",
        "--footprint",
        "--output",
        str(SYMBOL_LIB_PATH),
    ]
    if overwrite:
        cmd.append("--overwrite")

    try:
        proc = _run(cmd, timeout)
    except subprocess.TimeoutExpired:
        raise EasyEdaImportError(
            f"easyeda2kicad timed out after {timeout:.0f}s fetching {lcsc} "
            "(EasyEDA API unreachable or slow)."
        )

    combined = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
    low = combined.lower()

    # Success is decided by the library contents, not the (unreliable) exit code.
    symbols_after = _parse_symbols(SYMBOL_LIB_PATH)
    target = next((s for s in symbols_after if s["properties"].get("LCSC Part") == lcsc), None)
    if target is None:
        if "no module named easyeda2kicad" in low:
            raise EasyEdaImportError(
                "easyeda2kicad is not installed in the KiCAD MCP Python environment. "
                "Install it with:  pip install easyeda2kicad"
            )
        before_names = {s["name"] for s in symbols_before}
        new = [s for s in symbols_after if s["name"] not in before_names]
        if len(new) == 1:
            target = new[0]
    if target is None:
        raise EasyEdaImportError(
            f"easyeda2kicad did not produce a symbol for {lcsc}."
            + (f" Output:\n{combined[:500]}" if combined else "")
        )

    changed = _maybe_infer_pin_types(target["name"], infer_pin_types)
    resp = _build_response(lcsc, target, fetched=True)
    resp["pin_types_inferred"] = changed
    return resp


def import_lcsc_parts(
    lcsc_numbers: List[str],
    *,
    overwrite: bool = False,
    timeout: float = 90.0,
    infer_pin_types: bool = True,
) -> Dict[str, Any]:
    """Batch-import a list of LCSC parts into the shared cache.

    Pre-warms the ``easyeda`` symbol library for a whole BOM in one call.
    Each id is imported independently via :func:`import_lcsc_part`, so one
    bad/discontinued id never aborts the rest, and already-cached parts are
    skipped without a network call.  Duplicate ids (case/whitespace
    insensitive) are processed once.

    Returns an aggregate summary with per-part results.  ``success`` is True
    when **at least one** part was obtained (imported or already cached);
    ``all_succeeded`` is True only when nothing failed.
    """
    # De-duplicate while preserving order (case/whitespace-insensitive).
    seen: set = set()
    ordered: List[str] = []
    for raw in lcsc_numbers:
        key = str(raw).strip().upper()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(raw)

    results: List[Dict[str, Any]] = []
    imported = cached = failed = 0
    for raw in ordered:
        try:
            r = import_lcsc_part(
                raw, overwrite=overwrite, timeout=timeout, infer_pin_types=infer_pin_types
            )
            status = "cached" if r["already_cached"] else "imported"
            if r["already_cached"]:
                cached += 1
            else:
                imported += 1
            results.append(
                {
                    "lcsc": r["lcsc"],
                    "status": status,
                    "symbol": r["symbol"],
                    "lib_id": r["lib_id"],
                    "footprint": r.get("footprint"),
                }
            )
        except (EasyEdaImportError, ValueError) as e:
            failed += 1
            results.append({"lcsc": str(raw).strip(), "status": "failed", "error": str(e)})

    return {
        "success": (imported + cached) > 0,
        "all_succeeded": failed == 0,
        "library": EASYEDA_LIB_NICKNAME,
        "requested": len(ordered),
        "imported": imported,
        "cached": cached,
        "failed": failed,
        "failures": [r for r in results if r["status"] == "failed"],
        "symbol_lib_path": str(SYMBOL_LIB_PATH),
        "footprint_lib_dir": str(FOOTPRINT_LIB_DIR),
        "results": results,
    }
