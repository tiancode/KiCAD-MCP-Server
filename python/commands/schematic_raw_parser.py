"""Raw S-expression fallback parser for .kicad_sch files.

`kicad-skip` is the primary loader (see commands/schematic.py), but it
trips over certain stock KiCAD symbols — most often ones that use
`(extends "...")` inheritance or contain empty property names.  When skip
fails, the entire schematic becomes invisible to MCP, which is the worst
possible failure mode for an LLM agent that just wants to know what's on
the page.

This module gives `handle_list_schematic_components` a degraded path: a
plain sexpdata walk that returns the same shape skip would, minus pin
enrichment.  It only reads top-level `(symbol ...)` instances (the ones
under `(kicad_sch ...)` that have a `(lib_id ...)` and `(at ...)`),
skipping the `(lib_symbols ...)` section that's the usual source of
skip's `_base_coords` crash.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import sexpdata
from sexpdata import Symbol

logger = logging.getLogger(__name__)


def _atom(node: Any) -> str:
    """Stringify a sexpdata atom, stripping the Symbol wrapper."""
    if isinstance(node, Symbol):
        return node.value()
    return str(node)


def _head(node: Any) -> str:
    """Return the head atom of an S-expression list, or '' for non-lists."""
    if isinstance(node, list) and node and isinstance(node[0], Symbol):
        return node[0].value()
    return ""


def parse_components(file_path: str) -> List[Dict[str, Any]]:
    """Return a list of component dicts shaped like the skip path produces.

    Each dict has: reference, libId, value, footprint, position {x, y},
    rotation, uuid.  Does NOT include pin information — callers should
    treat the absence of `pins` as "use the dedicated pin tools instead".

    Raises OSError if the file can't be read; raises sexpdata's
    ExpectClosingBracket et al. if the file isn't valid S-expression.
    Per-symbol parse failures are logged and skipped, not raised, so a
    single malformed entry doesn't take out the whole list.
    """
    with open(file_path, "r", encoding="utf-8") as fh:
        data = sexpdata.loads(fh.read())

    if _head(data) != "kicad_sch":
        raise ValueError(f"Not a kicad_sch file: {file_path}")

    components: List[Dict[str, Any]] = []
    for node in data[1:]:
        if _head(node) != "symbol":
            continue
        try:
            comp = _parse_one_symbol(node)
        except (ValueError, IndexError, TypeError) as e:
            logger.debug("Skipping malformed symbol in %s: %s", file_path, e)
            continue
        if comp is None:
            continue
        components.append(comp)
    return components


def _parse_one_symbol(node: list) -> Dict[str, Any] | None:
    """Extract a single (symbol ...) instance's metadata, or None for
    library-definition-style entries (no lib_id / at)."""
    lib_id = ""
    position: List[float] = [0.0, 0.0, 0.0]
    uuid = ""
    properties: Dict[str, str] = {}
    has_at = False

    for child in node[1:]:
        head = _head(child)
        if head == "lib_id":
            lib_id = _atom(child[1])
        elif head == "at":
            has_at = True
            for i, comp in enumerate(child[1:4]):
                try:
                    position[i] = float(_atom(comp))
                except (ValueError, TypeError):
                    pass
        elif head == "uuid":
            uuid = _atom(child[1])
        elif head == "property":
            # (property "Name" "Value" ...)
            if len(child) >= 3:
                key = _atom(child[1])
                value = _atom(child[2])
                if key:
                    properties[key] = value

    if not has_at or not lib_id:
        # `(symbol ...)` blocks inside `lib_symbols` won't have a top-level
        # `(at ...)` or won't be instance entries — skip them.
        return None

    reference = properties.get("Reference", "")
    if reference.startswith("_TEMPLATE"):
        return None

    return {
        "reference": reference,
        "libId": lib_id,
        "value": properties.get("Value", ""),
        "footprint": properties.get("Footprint", ""),
        "position": {"x": position[0], "y": position[1]},
        "rotation": position[2],
        "uuid": uuid,
    }
