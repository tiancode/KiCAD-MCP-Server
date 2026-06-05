"""Module-level s-expression / text builders for the wire-manager package.

Pure helpers split out of the former monolithic commands/wire_manager.py.
"""

import logging
import math
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, List, Optional, Tuple

import sexpdata
from sexpdata import Symbol

from commands.schematic_locks import (
    atomic_write_text,
    schematic_path_lock,
    serialize_on_path,
)

from ._constants import (
    _SYM_WIRE,
    _SYM_PTS,
    _SYM_XY,
    _SYM_AT,
    _SYM_LABEL,
    _SYM_GLOBAL_LABEL,
    _SYM_HIERARCHICAL_LABEL,
    _SYM_STROKE,
    _SYM_WIDTH,
    _SYM_TYPE,
    _SYM_UUID,
    _SYM_SHEET_INSTANCES,
    _SYM_JUNCTION,
    _SYM_LIB_SYMBOLS,
    _SYM_LIB_ID,
    _SYM_MIRROR,
    _SYM_PIN,
    _SYM_SYMBOL,
    _SYM_UNIT,
    _SYM_KICAD_SCH,
    _IU_PER_MM,
    _LABEL_TYPE_ALIASES,
)

logger = logging.getLogger("kicad_interface")


def _normalize_label_type(label_type: str) -> str:
    """Map a caller-supplied label type to a canonical KiCad element name.

    Raises ValueError for anything unrecognised so a typo surfaces as a
    clear error rather than an invalid ``(<typo> ...)`` element that
    makes KiCad refuse to load the schematic.
    """
    key = str(label_type).strip().lower()
    if key not in _LABEL_TYPE_ALIASES:
        raise ValueError(
            f"Unknown label type {label_type!r}. Expected one of "
            "label / global_label / hierarchical_label "
            "(aliases accepted: local, global, hierarchical)."
        )
    return _LABEL_TYPE_ALIASES[key]


def _validate_schematic_sexpr(output: str) -> None:
    """Raise ValueError if ``output`` is not a structurally loadable .kicad_sch.

    Backstop against serializer corruption.  Checks, in order:

    1. Parenthesis balance (string-aware — parens inside ``"..."`` are
       ignored).  An unbalanced result is the failure mode seen when
       concurrent read-modify-write calls interleave.
    2. The text re-parses as a single S-expression tree rooted at
       ``(kicad_sch ...)``.

    This cannot catch a *balanced-but-semantically-wrong* file (an
    invalid element name still parses) — that class of bug is prevented
    at construction time (see ``_normalize_label_type``).  Run this on
    the serialized string BEFORE writing it, so a failed edit never
    truncates the on-disk schematic.
    """
    depth = 0
    in_str = False
    esc = False
    for ch in output:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise ValueError(
                    "schematic serialization produced an unbalanced ')' "
                    "(too many closing parens) — refusing to write a corrupt file"
                )
    if depth != 0:
        raise ValueError(
            f"schematic serialization left {depth} unclosed paren(s) — "
            "refusing to write a corrupt file"
        )
    try:
        tree = sexpdata.loads(output)
    except Exception as e:
        raise ValueError(f"schematic serialization is not valid S-expression: {e}")
    if not (isinstance(tree, list) and tree and tree[0] == _SYM_KICAD_SCH):
        raise ValueError("schematic serialization lost its (kicad_sch ...) root")


def _serialize_validated(sch_data: Any) -> str:
    """Serialize a parsed .kicad_sch tree, asserting the result is loadable.

    Returns the serialized text.  Raises ValueError (via
    ``_validate_schematic_sexpr``) before any file I/O so callers can
    serialize first, then open the file for writing only once the bytes
    are known-good — avoiding the truncate-on-open data loss that a
    raise *inside* ``open(path, "w")`` would cause.
    """
    output = sexpdata.dumps(sch_data)
    _validate_schematic_sexpr(output)
    return output


def _find_insertion_point(content: str) -> int:
    """Find the right place to insert new elements in a .kicad_sch file.

    Looks for (sheet_instances (KiCad 8) first, falls back to inserting
    before the final closing paren (KiCad 9+).
    """
    marker = "(sheet_instances"
    pos = content.rfind(marker)
    if pos != -1:
        return pos
    pos = content.rfind(")")
    if pos == -1:
        raise ValueError("Could not find insertion point in schematic")
    return pos


def _text_insert(file_path: Path, sexp_text: str) -> bool:
    """Insert S-expression text into a .kicad_sch file preserving formatting."""
    with schematic_path_lock(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        insert_at = _find_insertion_point(content)
        content = content[:insert_at] + sexp_text + content[insert_at:]

        atomic_write_text(file_path, content)
    return True


def _make_hierarchical_label_text(
    text: str,
    position: List[float],
    shape: str = "bidirectional",
    orientation: int = 0,
) -> str:
    """Generate a hierarchical_label S-expression as formatted text.

    orientation: 0=right (label points right, justify left),
                 180=left (label points left, justify right),
                 90/270=vertical.
    """
    uid = str(uuid.uuid4())
    justify = "right" if orientation == 180 else "left"
    text_esc = text.replace("\\", "\\\\").replace('"', '\\"')
    return (
        f'\t(hierarchical_label "{text_esc}"\n'
        f"\t\t(shape {shape})\n"
        f"\t\t(at {position[0]} {position[1]} {orientation})\n"
        f"\t\t(effects\n"
        f"\t\t\t(font\n"
        f"\t\t\t\t(size 1.27 1.27)\n"
        f"\t\t\t)\n"
        f"\t\t\t(justify {justify})\n"
        f"\t\t)\n"
        f'\t\t(uuid "{uid}")\n'
        f"\t)\n"
    )


def _make_sheet_pin_text(
    pin_name: str,
    pin_type: str,
    position: List[float],
    orientation: int = 0,
) -> str:
    """Generate a sheet pin S-expression as formatted text (indented for inside sheet block).

    orientation: 0=right side of sheet box, 180=left side.
    """
    uid = str(uuid.uuid4())
    justify = "left" if orientation == 0 else "right"
    pin_name_esc = pin_name.replace("\\", "\\\\").replace('"', '\\"')
    return (
        f'\t\t(pin "{pin_name_esc}" {pin_type}\n'
        f"\t\t\t(at {position[0]} {position[1]} {orientation})\n"
        f'\t\t\t(uuid "{uid}")\n'
        f"\t\t\t(effects\n"
        f"\t\t\t\t(font\n"
        f"\t\t\t\t\t(size 1.27 1.27)\n"
        f"\t\t\t\t)\n"
        f"\t\t\t\t(justify {justify})\n"
        f"\t\t\t)\n"
        f"\t\t)\n"
    )


def _make_sheet_text(
    sheet_name: str,
    sheet_file: str,
    position: List[float],
    size: List[float],
    project_name: str,
    root_uuid: str,
    page_number: str,
) -> str:
    """Generate a hierarchical sheet block S-expression as formatted text.

    Mirrors the KiCad 9/10 format (version 20250114): the box carries
    ``Sheetname`` / ``Sheetfile`` properties plus an ``(instances ...)`` block
    whose path is the PARENT (root) schematic's own top-level uuid — that is
    how real KiCad keys per-instance page numbers, so the root
    ``(sheet_instances)`` block is left untouched (KiCad does not list
    sub-sheets there).
    """
    uid = str(uuid.uuid4())
    x, y = position[0], position[1]
    w, h = size[0], size[1]
    # Property label placement matches KiCad: name just above the top edge,
    # file just below the bottom edge.
    name_y = round(y - 0.7625, 4)
    file_y = round(y + h + 0.61, 4)
    name_esc = sheet_name.replace("\\", "\\\\").replace('"', '\\"')
    file_esc = sheet_file.replace("\\", "\\\\").replace('"', '\\"')
    proj_esc = project_name.replace("\\", "\\\\").replace('"', '\\"')
    return (
        f"\t(sheet\n"
        f"\t\t(at {x} {y})\n"
        f"\t\t(size {w} {h})\n"
        f"\t\t(exclude_from_sim no)\n"
        f"\t\t(in_bom yes)\n"
        f"\t\t(on_board yes)\n"
        f"\t\t(dnp no)\n"
        f"\t\t(stroke\n"
        f"\t\t\t(width 0)\n"
        f"\t\t\t(type solid)\n"
        f"\t\t)\n"
        f"\t\t(fill\n"
        f"\t\t\t(color 0 0 0 0.0000)\n"
        f"\t\t)\n"
        f'\t\t(uuid "{uid}")\n'
        f'\t\t(property "Sheetname" "{name_esc}"\n'
        f"\t\t\t(at {x} {name_y} 0)\n"
        f"\t\t\t(effects\n"
        f"\t\t\t\t(font\n"
        f"\t\t\t\t\t(size 1.27 1.27)\n"
        f"\t\t\t\t)\n"
        f"\t\t\t\t(justify left bottom)\n"
        f"\t\t\t)\n"
        f"\t\t)\n"
        f'\t\t(property "Sheetfile" "{file_esc}"\n'
        f"\t\t\t(at {x} {file_y} 0)\n"
        f"\t\t\t(effects\n"
        f"\t\t\t\t(font\n"
        f"\t\t\t\t\t(size 1.27 1.27)\n"
        f"\t\t\t\t)\n"
        f"\t\t\t\t(justify left top)\n"
        f"\t\t\t)\n"
        f"\t\t)\n"
        f"\t\t(instances\n"
        f'\t\t\t(project "{proj_esc}"\n'
        f'\t\t\t\t(path "/{root_uuid}"\n'
        f'\t\t\t\t\t(page "{page_number}")\n'
        f"\t\t\t\t)\n"
        f"\t\t\t)\n"
        f"\t\t)\n"
        f"\t)\n"
    )
