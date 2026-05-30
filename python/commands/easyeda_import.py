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

logger = logging.getLogger("kicad_interface")

# Shared cache library: ~/.kicad-mcp/easyeda.kicad_sym  +  ~/.kicad-mcp/easyeda.pretty/
EASYEDA_LIB_NICKNAME = "easyeda"
_CACHE_DIR = Path.home() / ".kicad-mcp"
_LIB_BASE = _CACHE_DIR / EASYEDA_LIB_NICKNAME  # no extension
SYMBOL_LIB_PATH = _LIB_BASE.with_suffix(".kicad_sym")
FOOTPRINT_LIB_DIR = Path(str(_LIB_BASE) + ".pretty")

_LCSC_RE = re.compile(r"^C\d+$")


class EasyEdaImportError(RuntimeError):
    """A user-facing failure importing an LCSC part (network/tool/parse)."""


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
    return {
        "success": True,
        "lcsc": lcsc,
        "library": EASYEDA_LIB_NICKNAME,
        "symbol": name,
        "lib_id": f"{EASYEDA_LIB_NICKNAME}:{name}",
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
        "next": (
            f'Place it with add_schematic_component(library="{EASYEDA_LIB_NICKNAME}", '
            f'componentName="{name}")'
        ),
    }


def import_lcsc_part(
    lcsc_number: str,
    *,
    overwrite: bool = False,
    timeout: float = 90.0,
) -> Dict[str, Any]:
    """Import an LCSC/JLCPCB part as a KiCAD symbol + footprint.

    Idempotent: if the part is already in the cache library and ``overwrite``
    is False, the network/tool call is skipped and the cached symbol is
    returned.  Always (re-)registers the ``easyeda`` nickname so a library
    that exists on disk but is missing from the lib-table is repaired.

    Raises ``EasyEdaImportError`` for user-facing failures and ``ValueError``
    for a malformed LCSC id.
    """
    lcsc = _normalize_lcsc(lcsc_number)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    symbols_before = _parse_symbols(SYMBOL_LIB_PATH)
    cached = next((s for s in symbols_before if s["properties"].get("LCSC Part") == lcsc), None)
    if cached and not overwrite:
        return _build_response(lcsc, cached, fetched=False)

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

    return _build_response(lcsc, target, fetched=True)


def import_lcsc_parts(
    lcsc_numbers: List[str],
    *,
    overwrite: bool = False,
    timeout: float = 90.0,
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
            r = import_lcsc_part(raw, overwrite=overwrite, timeout=timeout)
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
