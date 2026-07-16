"""set_symbol_pin_types — rewrite pin electrical types after import (S7).

easyeda2kicad imports (and hand-built symbols) can leave pins typed
``unspecified``, which floods kicad-cli ERC with unclearable pin_to_pin
"Unspecified … connected" warnings. ``import_jlcpcb_symbol`` now infers types
at import time (see ``easyeda_import._infer_pin_type``), but there was no way to
FIX a symbol's pin types after the fact. This module powers a tool that does.

Two surfaces, same s-expression pin rewrite:

* **library file** — a ``lib_id`` like ``easyeda:RDA5807M`` (resolved via the
  sym-lib-table) or an explicit ``libraryPath`` + ``symbolName`` — rewrites the
  ``(symbol "Name" …)`` block in the ``.kicad_sym``. This is the source of
  truth; a schematic that already placed the part carries an EMBEDDED snapshot,
  so follow up with ``refresh_schematic_lib_symbols`` to push the change into
  the ``.kicad_sch`` (then ERC sees it).

* **schematic embedded** — ``schematicPath`` + (``reference`` or ``symbolId``)
  — rewrites the ``(symbol "Lib:Name" …)`` copy inside the schematic's
  ``lib_symbols`` block directly, so ERC reflects it immediately without a
  separate refresh.

Pin keys in the mapping match a pin's NUMBER first, then its NAME
(case-insensitive); one key ("NC", say) can retype several pins. The rewrite is
re-parsed before it is written atomically, and only ever touches the
electrical-type token of matched pins.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata

# Low-level s-expression helpers are shared with the importer's pin-type code.
from commands.easyeda_import import (
    _PIN_HEADER_RE,
    _PIN_NAME_RE,
    _symbol_span,
)
from utils.sexpr import find_block_end, rewrite_pin_blocks

logger = logging.getLogger("kicad_interface")

# The complete KiCad electrical-type vocabulary (matches the enum in
# src/tools/symbol-creator.ts PinSchema and the .kicad_sym format).
VALID_PIN_TYPES = frozenset(
    {
        "input",
        "output",
        "bidirectional",
        "tri_state",
        "passive",
        "free",
        "unspecified",
        "power_in",
        "power_out",
        "open_collector",
        "open_emitter",
        "no_connect",
    }
)

_PIN_NUMBER_RE = re.compile(r'\(number\s+"([^"]*)"')
# A PLACED instance: (symbol (lib_id "Lib:Name") …) — the "(lib_id" right after
# "(symbol" distinguishes it from a lib_symbols DEFINITION (symbol "Lib:Name" …).
_INSTANCE_RE = re.compile(r'\(symbol\s+\(lib_id\s+"([^"]+)"')
_REF_PROP_RE = re.compile(r'\(property\s+"Reference"\s+"([^"]*)"')

# ---------------------------------------------------------------------------
# Pin-type override marker (A13)
# ---------------------------------------------------------------------------
# run_erc pre-refreshes the schematic's embedded lib_symbols from the on-disk
# .kicad_sym (refresh_schematic_lib_symbols) to silence lib_symbol_mismatch
# drift.  That wholesale replace REVERTS a deliberate embedded pin-type edit
# (apply_to_schematic) back to the library's original types — and persists the
# revert.  apply_to_schematic therefore stamps a hidden ``ki_pin_type_override``
# property on the embedded definition recording exactly which pin keys were
# retyped and to what.  The refresh reads that marker and re-applies those pins
# onto the fresh disk copy (dynamic_symbol_loader.refresh_embedded_lib_symbols)
# instead of dropping them, so the edit survives while every OTHER library
# change (positions, graphics, other pins, descriptions) still flows through.
#
# The ``ki_`` prefix marks it library-internal: KiCad keeps such fields on the
# symbol definition and never stamps them onto placed instances (see
# dynamic_symbol_loader's ki_* exclusion), so the marker stays invisible on the
# canvas and out of the BOM.
PIN_TYPE_OVERRIDE_PROP = "ki_pin_type_override"

_OVERRIDE_MARKER_FIND = f'(property "{PIN_TYPE_OVERRIDE_PROP}"'
_OVERRIDE_VALUE_RE = re.compile(
    r'\(property\s+"' + re.escape(PIN_TYPE_OVERRIDE_PROP) + r'"\s+"((?:[^"\\]|\\.)*)"'
)
# Match a symbol block header up to (and including) its quoted name.
_SYMBOL_HEADER_RE = re.compile(r'\(symbol\s+"(?:[^"\\]|\\.)*"')
# A pin key persists only if it survives the compact ``key=type;…`` encoding
# unambiguously — i.e. carries no delimiter or quote chars.  Pin numbers and
# ordinary pin names qualify; an exotic key is simply not recorded (the pin is
# still retyped, only its cross-refresh persistence is skipped).
_MARKER_SAFE_KEY_RE = re.compile(r'^[^;="\\]+$')


class SymbolPinTypeError(RuntimeError):
    """A user-facing failure editing a symbol's pin types."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def normalize_mapping(pin_types: Dict[str, Any]) -> Dict[str, str]:
    """Coerce a {pinKey: type} mapping to {UPPER str key: str type}.

    Keys are upper-cased for case-insensitive pin matching; values are left
    verbatim so ``invalid_types`` can report exactly what the caller sent.
    """
    out: Dict[str, str] = {}
    for key, value in pin_types.items():
        out[str(key).strip().upper()] = str(value)
    return out


def invalid_types(lookup: Dict[str, str]) -> Dict[str, str]:
    """Return the {key: type} pairs whose type is not a KiCad electrical type."""
    return {k: v for k, v in lookup.items() if v not in VALID_PIN_TYPES}


# ---------------------------------------------------------------------------
# Pin-block rewrite
# ---------------------------------------------------------------------------
def _retype_pin(pin_block: str, lookup: Dict[str, str]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Rewrite one ``(pin …)`` block if its number/name is in ``lookup``.

    Returns (possibly-rewritten block, record-or-None). The record notes the
    matched key and whether the type actually changed.
    """
    header = _PIN_HEADER_RE.match(pin_block)
    if not header:
        return pin_block, None
    num_m = _PIN_NUMBER_RE.search(pin_block)
    name_m = _PIN_NAME_RE.search(pin_block)
    number = num_m.group(1) if num_m else ""
    name = name_m.group(1) if name_m else ""

    matched_key: Optional[str] = None
    if number and number.upper() in lookup:
        matched_key = number.upper()
    elif name and name.upper() in lookup:
        matched_key = name.upper()
    if matched_key is None:
        return pin_block, None

    new_type = lookup[matched_key]
    old_type = header.group(1)
    record = {
        "number": number,
        "name": name,
        "old_type": old_type,
        "new_type": new_type,
        "key": matched_key,
        "changed": old_type != new_type,
    }
    if old_type == new_type:
        return pin_block, record
    rewritten = pin_block[: header.start(1)] + new_type + pin_block[header.end(1) :]
    return rewritten, record


def rewrite_pins_in_block(
    block: str, lookup: Dict[str, str]
) -> Tuple[str, List[Dict[str, Any]], set]:
    """Retype every matched pin inside a symbol block.

    Delegates the quote-aware ``(pin …)`` walk to ``sexpr.rewrite_pin_blocks``
    so ``(pin `` only matches real s-expression openings, never a substring
    inside a quoted value. Returns (new_block, records, matched_keys).
    """
    records: List[Dict[str, Any]] = []
    matched_keys: set = set()

    def _transform(pin_block: str) -> str:
        new_pin, record = _retype_pin(pin_block, lookup)
        if record is not None:
            records.append(record)
            matched_keys.add(record["key"])
        return new_pin

    return rewrite_pin_blocks(block, _transform), records, matched_keys


def serialize_pin_overrides(overrides: Dict[str, str]) -> str:
    """Encode ``{pinKey: type}`` as a deterministic ``key=type;…`` string.

    Sorted (so apply-time and refresh-time stamps are byte-identical), skipping
    any pair whose type is not a valid electrical type or whose key carries a
    delimiter char that the compact encoding can't represent.
    """
    parts = []
    for k, v in sorted(overrides.items()):
        if v in VALID_PIN_TYPES and _MARKER_SAFE_KEY_RE.match(k):
            parts.append(f"{k}={v}")
    return ";".join(parts)


def deserialize_pin_overrides(text: str) -> Dict[str, str]:
    """Inverse of :func:`serialize_pin_overrides`; ignores malformed pairs."""
    out: Dict[str, str] = {}
    for part in text.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k and v in VALID_PIN_TYPES:
            out[k] = v
    return out


def _find_override_marker_span(block: str) -> Optional[Tuple[int, int]]:
    idx = block.find(_OVERRIDE_MARKER_FIND)
    if idx == -1:
        return None
    return idx, find_block_end(block, idx)


def read_pin_overrides(block: str) -> Dict[str, str]:
    """Return the ``{pinKey: type}`` overrides recorded on a symbol block, if any."""
    m = _OVERRIDE_VALUE_RE.search(block)
    if not m:
        return {}
    return deserialize_pin_overrides(m.group(1))


def stamp_pin_overrides(block: str, overrides: Dict[str, str]) -> str:
    """Return ``block`` carrying exactly one override marker for ``overrides``.

    Any pre-existing marker is removed first, so repeated stamps never
    accumulate duplicates.  With empty (or fully-filtered-out) ``overrides`` the
    block is returned marker-free.  A non-symbol block is returned untouched.
    """
    span = _find_override_marker_span(block)
    if span is not None:
        s, e = span
        line_start = block.rfind("\n", 0, s) + 1
        # If the marker sat on its own line, drop the whole line (incl. trailing
        # newline) so we don't leave a blank line behind.
        if block[line_start:s].strip() == "" and e < len(block) and block[e] == "\n":
            block = block[:line_start] + block[e + 1 :]
        else:
            block = block[:s] + block[e:]

    serialized = serialize_pin_overrides(overrides)
    if not serialized:
        return block
    hm = _SYMBOL_HEADER_RE.match(block.lstrip())
    if not hm:
        return block
    # Offset the header match back into the un-lstripped block.
    insert_at = (len(block) - len(block.lstrip())) + hm.end()
    marker = (
        f'\n    (property "{PIN_TYPE_OVERRIDE_PROP}" "{serialized}" (at 0 0 0)\n'
        f"      (effects (font (size 1.27 1.27)) (hide yes))\n"
        f"    )"
    )
    return block[:insert_at] + marker + block[insert_at:]


def _atomic_write(path: Path, content: str, suffix: str) -> None:
    tmp = path.with_name(path.name + suffix)
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _result(
    *,
    target: str,
    file: Path,
    symbol: str,
    records: List[Dict[str, Any]],
    matched_keys: set,
    lookup: Dict[str, str],
    wrote: bool,
    next_hint: str,
) -> Dict[str, Any]:
    changed = [r for r in records if r["changed"]]
    unmatched = sorted(k for k in lookup if k not in matched_keys)
    applied = [{k: r[k] for k in ("number", "name", "old_type", "new_type")} for r in records]
    return {
        "success": True,
        "target": target,
        "file": str(file),
        "symbol": symbol,
        "matched": len(records),
        "changed": len(changed),
        "wrote": wrote,
        "applied": applied,
        "unmatched_keys": unmatched,
        "message": (
            f"Retyped {len(changed)} pin(s) of {symbol} in {file.name}"
            if changed
            else f"No pin types changed on {symbol} ({len(records)} matched, already set)"
        ),
        "next": next_hint,
    }


# ---------------------------------------------------------------------------
# Library-file surface
# ---------------------------------------------------------------------------
def apply_to_library(lib_path: Path, symbol_name: str, lookup: Dict[str, str]) -> Dict[str, Any]:
    """Rewrite matched pins of ``symbol_name`` in a ``.kicad_sym`` file."""
    if not lib_path.exists():
        raise SymbolPinTypeError(f"Symbol library not found: {lib_path}")
    content = lib_path.read_text(encoding="utf-8")
    span = _symbol_span(content, symbol_name)
    if span is None:
        raise SymbolPinTypeError(f"Symbol {symbol_name!r} not found in {lib_path}")
    start, end = span
    new_block, records, matched_keys = rewrite_pins_in_block(content[start:end], lookup)
    changed = any(r["changed"] for r in records)
    wrote = False
    if changed:
        new_content = content[:start] + new_block + content[end:]
        _validate(new_content, lib_path)
        _atomic_write(lib_path, new_content, ".pintypes.tmp")
        wrote = True
        logger.info(
            f"set_symbol_pin_types: retyped {sum(r['changed'] for r in records)} "
            f"pin(s) of {symbol_name} in {lib_path}"
        )
    next_hint = (
        f"Library updated. If a .kicad_sch already placed {symbol_name}, run "
        "refresh_schematic_lib_symbols on it so the embedded lib_symbols copy "
        "picks up the new pin types, then run_erc."
    )
    return _result(
        target="library",
        file=lib_path,
        symbol=symbol_name,
        records=records,
        matched_keys=matched_keys,
        lookup=lookup,
        wrote=wrote,
        next_hint=next_hint,
    )


# ---------------------------------------------------------------------------
# Schematic-embedded surface
# ---------------------------------------------------------------------------
def _lib_symbols_span(content: str) -> Optional[Tuple[int, int]]:
    start = content.find("(lib_symbols")
    if start == -1:
        return None
    return start, find_block_end(content, start)


def find_reference_lib_id(content: str, reference: str) -> Optional[str]:
    """Resolve a placed component's ``reference`` to its ``lib_id``.

    Scans every placed instance ``(symbol (lib_id "…") … (property "Reference"
    "<ref>" …))`` and returns the lib_id of the one whose Reference matches.
    """
    for m in _INSTANCE_RE.finditer(content):
        end = find_block_end(content, m.start())
        block = content[m.start() : end]
        ref_m = _REF_PROP_RE.search(block)
        if ref_m and ref_m.group(1) == reference:
            return m.group(1)
    return None


def apply_to_schematic(sch_path: Path, lib_id: str, lookup: Dict[str, str]) -> Dict[str, Any]:
    """Rewrite matched pins of the embedded ``(symbol "lib_id" …)`` snapshot."""
    if not sch_path.exists():
        raise SymbolPinTypeError(f"Schematic not found: {sch_path}")
    content = sch_path.read_text(encoding="utf-8")
    ls_span = _lib_symbols_span(content)
    if ls_span is None:
        raise SymbolPinTypeError(
            f"{sch_path.name} has no lib_symbols block — nothing to retype. "
            "Place a component first, or edit the library file instead."
        )
    ls_start, ls_end = ls_span
    lib_block = content[ls_start:ls_end]
    sym_span = _symbol_span(lib_block, lib_id)
    if sym_span is None:
        raise SymbolPinTypeError(
            f"No embedded lib_symbols entry {lib_id!r} in {sch_path.name}. "
            "Pass the full 'Library:Name' id, or a reference that is placed."
        )
    s, e = sym_span
    old_sym = lib_block[s:e]
    new_sym, records, matched_keys = rewrite_pins_in_block(old_sym, lookup)
    changed = any(r["changed"] for r in records)
    # A13: record which pins were deliberately retyped (merged with any override
    # already on the block) so run_erc's pre-refresh re-applies them onto the
    # fresh disk copy instead of reverting the edit.  Every matched pin is
    # recorded — even one whose type was already correct — so the marker is
    # present the moment ERC's refresh could otherwise revert it.
    applied = {r["key"]: r["new_type"] for r in records}
    merged = {**read_pin_overrides(new_sym), **applied}
    if merged:
        new_sym = stamp_pin_overrides(new_sym, merged)
    wrote = False
    if new_sym != old_sym:
        new_lib_block = lib_block[:s] + new_sym + lib_block[e:]
        new_content = content[:ls_start] + new_lib_block + content[ls_end:]
        _validate(new_content, sch_path)
        _atomic_write(sch_path, new_content, ".pintypes.tmp")
        wrote = True
        logger.info(
            f"set_symbol_pin_types: retyped {sum(r['changed'] for r in records)} "
            f"embedded pin(s) of {lib_id} in {sch_path}"
        )
    next_hint = (
        "Embedded snapshot updated — run_erc should no longer report those "
        "pin_to_pin 'Unspecified' warnings. To make it permanent in the source "
        "library too, call set_symbol_pin_types again with the symbolId form."
    )
    return _result(
        target="schematic",
        file=sch_path,
        symbol=lib_id,
        records=records,
        matched_keys=matched_keys,
        lookup=lookup,
        wrote=wrote,
        next_hint=next_hint,
    )


def _validate(new_content: str, path: Path) -> None:
    try:
        sexpdata.loads(new_content)
    except Exception as e:  # never write a file we just broke
        raise SymbolPinTypeError(
            f"Pin-type rewrite of {path.name} produced invalid s-expression; "
            f"left unchanged: {e}"
        )
