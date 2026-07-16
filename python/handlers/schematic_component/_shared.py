"""Shared text-parsing helpers for the schematic_component handlers.

These deduplicate the "read the ``.kicad_sch`` and locate the placed symbol
block(s) for a given reference" boilerplate shared by the get / edit / delete
component handlers.  Extracted verbatim — behavior and response shapes are
unchanged.

See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface


def load_schematic_text(
    schematic_path: Any,
) -> Tuple[Path, Optional[str], Optional[Dict[str, Any]]]:
    """Read a schematic file.

    Returns ``(sch_file, content, None)`` on success, or
    ``(sch_file, None, error)`` when the file does not exist — where ``error``
    is the exact ``Schematic not found`` refusal these handlers returned inline
    before extraction.
    """
    sch_file = Path(schematic_path)
    if not sch_file.exists():
        return (
            sch_file,
            None,
            {
                "success": False,
                "message": f"Schematic not found: {schematic_path}",
            },
        )
    with open(sch_file, "r", encoding="utf-8") as f:
        content = f.read()
    return sch_file, content, None


def find_placed_symbol_blocks(
    iface: "KiCADInterface", content: str, reference: str
) -> List[Tuple[int, int]]:
    """Return ``(start, end)`` char offsets of every placed symbol block whose
    ``Reference`` property equals ``reference``.

    KiCAD may serialise the children of ``(symbol ...)`` in different orders —
    ``(symbol (lib_id "..."))`` is the common case, but symbols whose library
    entry has been rescued / customised carry an extra ``(lib_name "...")``
    first: ``(symbol (lib_name "...") (lib_id "..."))``.  Matching any opening
    paren after ``(symbol`` handles both; library-definition symbols (which use
    the ``(symbol "name" ...)`` form — quoted string, not paren) are excluded by
    the lib_symbols range check.

    Callers that expect a single match take ``blocks[0]``; the delete handler
    collects all matches to remove duplicate placements.
    """
    # Skip lib_symbols section
    lib_sym_pos = content.find("(lib_symbols")
    lib_sym_end = iface._find_matching_paren(content, lib_sym_pos) if lib_sym_pos >= 0 else -1

    blocks: List[Tuple[int, int]] = []
    search_start = 0
    pattern = re.compile(r"\(symbol\s+\(")
    while True:
        m = pattern.search(content, search_start)
        if not m:
            break
        pos = m.start()
        # Skip blocks inside lib_symbols
        if lib_sym_pos >= 0 and lib_sym_pos <= pos <= lib_sym_end:
            search_start = lib_sym_end + 1
            continue
        end = iface._find_matching_paren(content, pos)
        if end < 0:
            search_start = pos + 1
            continue
        block_text = content[pos : end + 1]
        if re.search(
            r'\(property\s+"Reference"\s+"' + re.escape(reference) + r'"',
            block_text,
        ):
            blocks.append((pos, end))
        search_start = end + 1
    return blocks
