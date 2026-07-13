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
    _match_paren,
    _symbol_span,
)

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

    Walks the text quote-aware so ``(pin `` only matches real s-expression
    openings, never a substring inside a quoted value. Returns
    (new_block, records, matched_keys).
    """
    out: List[str] = []
    records: List[Dict[str, Any]] = []
    matched_keys: set = set()
    i = 0
    n = len(block)
    in_str = False
    while i < n:
        c = block[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(block[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "(" and block.startswith("(pin ", i):
            end = _match_paren(block, i)
            new_pin, record = _retype_pin(block[i:end], lookup)
            out.append(new_pin)
            if record is not None:
                records.append(record)
                matched_keys.add(record["key"])
            i = end
            continue
        out.append(c)
        i += 1
    return "".join(out), records, matched_keys


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
    applied = [
        {k: r[k] for k in ("number", "name", "old_type", "new_type")} for r in records
    ]
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
    return start, _match_paren(content, start)


def find_reference_lib_id(content: str, reference: str) -> Optional[str]:
    """Resolve a placed component's ``reference`` to its ``lib_id``.

    Scans every placed instance ``(symbol (lib_id "…") … (property "Reference"
    "<ref>" …))`` and returns the lib_id of the one whose Reference matches.
    """
    for m in _INSTANCE_RE.finditer(content):
        end = _match_paren(content, m.start())
        block = content[m.start() : end]
        ref_m = _REF_PROP_RE.search(block)
        if ref_m and ref_m.group(1) == reference:
            return m.group(1)
    return None


def apply_to_schematic(
    sch_path: Path, lib_id: str, lookup: Dict[str, str]
) -> Dict[str, Any]:
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
    new_sym, records, matched_keys = rewrite_pins_in_block(lib_block[s:e], lookup)
    changed = any(r["changed"] for r in records)
    wrote = False
    if changed:
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
