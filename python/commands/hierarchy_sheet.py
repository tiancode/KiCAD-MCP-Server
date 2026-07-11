"""Hierarchical-sheet authoring for ``.kicad_sch`` files (KiCad 9/10 format).

Pure text-manipulation functions operating on file paths — no pcbnew, no
kicad-skip.  Two public entry points:

* :func:`create_hierarchical_sheet` — insert a ``(sheet ...)`` box into a
  parent schematic (optionally creating the referenced child file).
* :func:`add_sheet_pin` — add a ``(pin ...)`` on a named sheet's border and,
  optionally, the matching ``(hierarchical_label ...)`` in the child sheet.

The emitted S-expressions mirror what KiCad 9/10 (file version 20250114+)
writes itself:

* the sheet box carries ``Sheetname`` (above the top edge, ``justify left
  bottom``) and ``Sheetfile`` (below the bottom edge, ``justify left top``)
  properties plus an ``(instances (project ... (path "/ROOT_UUID"
  (page "N"))))`` block keyed on the parent schematic's own top-level uuid;
* sheet pins use the KiCad angle convention documented on
  :data:`_SIDE_ANGLE`: a pin on the *left* edge has angle 180 (text justified
  right, inside the box), *right* edge angle 0 (justify left), *top* edge
  angle 90 and *bottom* edge angle 270.

All writes go through an atomic temp-file + ``os.replace`` swap, and every
assembled file is balance-checked before touching disk so a malformed block
can never truncate an existing schematic.
"""

from __future__ import annotations

import os
import re
import tempfile
import uuid as uuid_module
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Iterator, List, Optional, Tuple

from utils import sexpr

__all__ = ["create_hierarchical_sheet", "add_sheet_pin"]

_VALID_PIN_SHAPES = ("input", "output", "bidirectional", "tri_state", "passive")
_VALID_SIDES = ("left", "right", "top", "bottom")

# KiCad sheet-pin angle convention per border side (see module docstring).
_SIDE_ANGLE: Dict[str, int] = {"left": 180, "right": 0, "top": 90, "bottom": 270}
# Text justification that pairs with each angle so the pin label sits inside
# the sheet box (matches what eeschema writes).
_SIDE_JUSTIFY: Dict[str, str] = {
    "left": "right",
    "right": "left",
    "top": "left",
    "bottom": "right",
}

_PIN_STEP_MM = 2.54
_CHILD_LABEL_X_MM = 25.4
_CHILD_LABEL_START_Y_MM = 25.4

_DEFAULT_VERSION = "20250114"
_DEFAULT_GENERATOR = "KiCAD-MCP-Server"


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _fail(message: str) -> Dict[str, Any]:
    return {"success": False, "message": message}


def _fmt(value: float) -> str:
    """Format a coordinate the way KiCad does: no exponent, no trailing zeros."""
    return f"{round(float(value), 4):g}"


def _parens_balanced(content: str) -> bool:
    """String-aware parenthesis balance check (parens inside ``"..."`` ignored)."""
    depth = 0
    in_str = False
    esc = False
    for ch in content:
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
                return False
    return depth == 0 and not in_str


def _is_parseable_root(content: str) -> bool:
    """True when ``content`` is a balanced S-expression rooted at (kicad_sch ...)."""
    if not content.lstrip().startswith("(kicad_sch"):
        return False
    return _parens_balanced(content)


def _iter_top_level_blocks(content: str) -> Iterator[Tuple[int, int, str]]:
    """Yield ``(start, end, keyword)`` for each direct child of the root form.

    ``start``/``end`` are the indices of the child's opening and closing
    parens.  String-aware, so quoted parens are ignored.
    """
    root = content.find("(")
    if root < 0:
        return
    depth = 0
    in_str = False
    esc = False
    child_start = -1
    i = root + 1
    while i < len(content):
        ch = content[i]
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == "(":
                if depth == 0:
                    child_start = i
                depth += 1
            elif ch == ")":
                if depth == 0:
                    return  # closing paren of the root form
                depth -= 1
                if depth == 0 and child_start >= 0:
                    kw_match = re.match(r"\(\s*([A-Za-z_0-9]+)", content[child_start : i + 1])
                    yield child_start, i, kw_match.group(1) if kw_match else ""
                    child_start = -1
        i += 1


def _root_uuid(content: str) -> Optional[str]:
    """Return the schematic's own top-level ``(uuid ...)`` value, if any."""
    for start, end, keyword in _iter_top_level_blocks(content):
        if keyword == "uuid":
            m = re.match(r'\(\s*uuid\s+"?([0-9a-fA-F-]+)"?\s*\)', content[start : end + 1])
            if m:
                return m.group(1)
    return None


def _top_level_sheet_blocks(content: str) -> List[Tuple[int, int]]:
    """Spans of every top-level ``(sheet ...)`` block (not ``sheet_instances``)."""
    return [(s, e) for s, e, kw in _iter_top_level_blocks(content) if kw == "sheet"]


def _header_token(content: str, keyword: str, default: str) -> str:
    """Extract a top-level single-token header value like (version N) / (generator "X")."""
    for start, end, kw in _iter_top_level_blocks(content):
        if kw == keyword:
            m = re.match(
                r"\(\s*" + keyword + r'\s+"?([^"()]+?)"?\s*\)',
                content[start : end + 1],
            )
            if m:
                return m.group(1).strip()
    return default


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via a same-directory temp file + os.replace."""
    directory = path.parent if str(path.parent) else Path(".")
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _insert_top_level(content: str, block_text: str) -> Optional[str]:
    """Insert ``block_text`` as a new top-level child of the root form.

    Prefers the position just before ``(sheet_instances`` (the root sheet's
    trailer); falls back to just before the final closing paren (sub-sheets
    carry no ``sheet_instances``).  Returns None when no insertion point
    exists.
    """
    insert_at = content.rfind("(sheet_instances")
    if insert_at != -1:
        return content[:insert_at] + block_text.lstrip("\n") + "\t" + content[insert_at:]
    stripped = content.rstrip()
    if not stripped.endswith(")"):
        return None
    insert_at = len(stripped) - 1
    return content[:insert_at] + block_text.lstrip("\n") + content[insert_at:]


def _guess_project_name(parent_path: Path) -> str:
    """Project name = sibling ``.kicad_pro`` stem when present, else the sheet stem."""
    pros = sorted(parent_path.parent.glob("*.kicad_pro"))
    if pros:
        return pros[0].stem
    return parent_path.stem


# ---------------------------------------------------------------------------
# S-expression builders
# ---------------------------------------------------------------------------


def _make_sheet_block(
    sheet_name: str,
    sheet_file: str,
    position: Tuple[float, float],
    size: Tuple[float, float],
    project_name: str,
    root_uuid: str,
    page_number: str,
    sheet_uuid: str,
) -> str:
    """Build a KiCad 9/10 ``(sheet ...)`` block as tab-indented text.

    Sheetname is anchored just above the top edge (justify left bottom),
    Sheetfile just below the bottom edge (justify left top) — the placement
    eeschema itself uses.
    """
    x, y = position
    w, h = size
    name_y = round(y - 0.7625, 4)
    file_y = round(y + h + 0.61, 4)
    name_esc = sexpr.escape_sexpr_string(sheet_name)
    file_esc = sexpr.escape_sexpr_string(sheet_file)
    proj_esc = sexpr.escape_sexpr_string(project_name)
    return (
        f"\t(sheet\n"
        f"\t\t(at {_fmt(x)} {_fmt(y)})\n"
        f"\t\t(size {_fmt(w)} {_fmt(h)})\n"
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
        f'\t\t(uuid "{sheet_uuid}")\n'
        f'\t\t(property "Sheetname" "{name_esc}"\n'
        f"\t\t\t(at {_fmt(x)} {_fmt(name_y)} 0)\n"
        f"\t\t\t(effects\n"
        f"\t\t\t\t(font\n"
        f"\t\t\t\t\t(size 1.27 1.27)\n"
        f"\t\t\t\t)\n"
        f"\t\t\t\t(justify left bottom)\n"
        f"\t\t\t)\n"
        f"\t\t)\n"
        f'\t\t(property "Sheetfile" "{file_esc}"\n'
        f"\t\t\t(at {_fmt(x)} {_fmt(file_y)} 0)\n"
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


def _make_pin_text(
    pin_name: str,
    shape: str,
    position: Tuple[float, float],
    angle: int,
    justify: str,
    pin_uuid: str,
) -> str:
    """Build a sheet ``(pin ...)`` block, indented for insertion inside a sheet."""
    name_esc = sexpr.escape_sexpr_string(pin_name)
    return (
        f'\t\t(pin "{name_esc}" {shape}\n'
        f"\t\t\t(at {_fmt(position[0])} {_fmt(position[1])} {angle})\n"
        f"\t\t\t(effects\n"
        f"\t\t\t\t(font\n"
        f"\t\t\t\t\t(size 1.27 1.27)\n"
        f"\t\t\t\t)\n"
        f"\t\t\t\t(justify {justify})\n"
        f"\t\t\t)\n"
        f'\t\t\t(uuid "{pin_uuid}")\n'
        f"\t\t)\n"
    )


def _make_hier_label_text(
    name: str,
    shape: str,
    position: Tuple[float, float],
    label_uuid: str,
) -> str:
    """Build a top-level ``(hierarchical_label ...)`` block (angle 0, justify left)."""
    name_esc = sexpr.escape_sexpr_string(name)
    return (
        f'\t(hierarchical_label "{name_esc}"\n'
        f"\t\t(shape {shape})\n"
        f"\t\t(at {_fmt(position[0])} {_fmt(position[1])} 0)\n"
        f"\t\t(effects\n"
        f"\t\t\t(font\n"
        f"\t\t\t\t(size 1.27 1.27)\n"
        f"\t\t\t)\n"
        f"\t\t\t(justify left)\n"
        f"\t\t)\n"
        f'\t\t(uuid "{label_uuid}")\n'
        f"\t)\n"
    )


def _make_empty_child(version: str, generator: str) -> str:
    """Build a minimal valid empty ``.kicad_sch`` with a fresh uuid."""
    child_uuid = str(uuid_module.uuid4())
    return (
        f"(kicad_sch\n"
        f"\t(version {version})\n"
        f'\t(generator "{sexpr.escape_sexpr_string(generator)}")\n'
        f'\t(uuid "{child_uuid}")\n'
        f'\t(paper "A4")\n'
        f"\t(lib_symbols)\n"
        f")\n"
    )


# ---------------------------------------------------------------------------
# Sheet-block inspection
# ---------------------------------------------------------------------------


def _sheet_has_name(block: str, sheet_name: str) -> bool:
    escaped = re.escape(sexpr.escape_sexpr_string(sheet_name))
    return re.search(r'\(property\s+"Sheetname"\s+"' + escaped + r'"', block) is not None


def _find_sheet_block(content: str, sheet_name: str) -> Optional[Tuple[int, int]]:
    """Span of the top-level (sheet ...) block whose Sheetname matches, or None."""
    for start, end in _top_level_sheet_blocks(content):
        if _sheet_has_name(content[start : end + 1], sheet_name):
            return start, end
    return None


def _sheet_geometry(block: str) -> Optional[Tuple[float, float, float, float]]:
    """Return (x, y, w, h) parsed from the sheet block's (at ...) and (size ...)."""
    at = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)\s*\)", block)
    size = re.search(r"\(size\s+([-\d.]+)\s+([-\d.]+)\s*\)", block)
    if not at or not size:
        return None
    return float(at.group(1)), float(at.group(2)), float(size.group(1)), float(size.group(2))


def _existing_pins(block: str) -> List[Tuple[str, float, float, int]]:
    """All (name, x, y, angle) sheet pins declared inside a sheet block."""
    pins: List[Tuple[str, float, float, int]] = []
    for m in re.finditer(r'\(pin\s+"((?:[^"\\]|\\.)*)"', block):
        end = sexpr.find_matching_paren(block, m.start())
        if end < 0:
            continue
        pin_block = block[m.start() : end + 1]
        at = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\)", pin_block)
        if at:
            pins.append(
                (m.group(1), float(at.group(1)), float(at.group(2)), int(float(at.group(3))))
            )
    return pins


def _pin_position(
    side: str,
    offset_mm: float,
    geometry: Tuple[float, float, float, float],
    existing: List[Tuple[str, float, float, int]],
) -> Tuple[float, float]:
    """Compute the next free pin position on ``side``, stacked in 2.54 mm steps.

    The first pin sits ``offset_mm`` from the side's starting corner (top
    corner for left/right, left corner for top/bottom); subsequent pins on the
    same side land one _PIN_STEP_MM past the furthest existing pin.
    """
    x, y, w, h = geometry
    angle = _SIDE_ANGLE[side]
    same_side = [p for p in existing if p[3] == angle]
    if side in ("left", "right"):
        fixed_x = x if side == "left" else x + w
        stack = y + offset_mm
        if same_side:
            stack = max(p[2] for p in same_side) + _PIN_STEP_MM
        return fixed_x, stack
    fixed_y = y if side == "top" else y + h
    stack = x + offset_mm
    if same_side:
        stack = max(p[1] for p in same_side) + _PIN_STEP_MM
    return stack, fixed_y


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_hierarchical_sheet(
    parent_sch_path: str,
    *,
    sheet_name: str,
    child_filename: str,
    position: Tuple[float, float],
    size: Tuple[float, float] = (50.0, 40.0),
    create_child: bool = True,
) -> Dict[str, Any]:
    """Insert a hierarchical ``(sheet ...)`` box into a parent schematic.

    Args:
        parent_sch_path: Path of the parent ``.kicad_sch`` to modify.
        sheet_name: Value for the sheet's ``Sheetname`` property; must not
            collide with an existing sheet in the parent.
        child_filename: Value for the ``Sheetfile`` property — a path relative
            to the parent's directory (absolute paths are refused).
        position: Sheet top-left corner in mm.
        size: Sheet (width, height) in mm.
        create_child: When True, write a minimal empty child schematic at
            ``child_filename`` if the file does not exist yet.

    Returns:
        On success ``{"success": True, "sheetName", "sheetFile", "uuid",
        "page", "childCreated"}``; on refusal ``{"success": False,
        "message"}``.
    """
    parent = Path(parent_sch_path)
    if not parent.exists():
        return _fail(f"Parent schematic not found: {parent_sch_path}")
    if not sheet_name:
        return _fail("sheet_name must not be empty")
    if not child_filename:
        return _fail("child_filename must not be empty")
    if PurePosixPath(child_filename).is_absolute() or PureWindowsPath(child_filename).is_absolute():
        return _fail(
            f"child_filename must be relative to the parent schematic's directory, "
            f"got absolute path: {child_filename}"
        )

    try:
        content = parent.read_text(encoding="utf-8")
    except OSError as e:
        return _fail(f"Could not read parent schematic: {e}")
    if not _is_parseable_root(content):
        return _fail(f"Parent schematic is not a parseable (kicad_sch ...) file: {parent_sch_path}")

    root_uuid = _root_uuid(content)
    if not root_uuid:
        return _fail(
            "Parent schematic has no top-level (uuid ...) — cannot key the sheet's "
            "(instances ...) page number"
        )

    existing_sheets = _top_level_sheet_blocks(content)
    for start, end in existing_sheets:
        if _sheet_has_name(content[start : end + 1], sheet_name):
            return _fail(f"A sheet named '{sheet_name}' already exists in {parent.name}")

    # Parent is page 1; each existing sheet already occupies a page after it.
    page = str(len(existing_sheets) + 2)
    sheet_uuid = str(uuid_module.uuid4())
    block = _make_sheet_block(
        sheet_name,
        child_filename,
        position,
        size,
        _guess_project_name(parent),
        root_uuid,
        page,
        sheet_uuid,
    )
    new_content = _insert_top_level(content, block)
    if new_content is None or not _is_parseable_root(new_content):
        return _fail("Internal error: sheet insertion would corrupt the parent schematic")

    child_created = False
    child_path = parent.parent / child_filename
    if create_child and not child_path.exists():
        version = _header_token(content, "version", _DEFAULT_VERSION)
        generator = _header_token(content, "generator", _DEFAULT_GENERATOR)
        child_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(child_path, _make_empty_child(version, generator))
        child_created = True

    _atomic_write(parent, new_content)
    return {
        "success": True,
        "sheetName": sheet_name,
        "sheetFile": child_filename,
        "uuid": sheet_uuid,
        "page": page,
        "childCreated": child_created,
    }


def add_sheet_pin(
    parent_sch_path: str,
    *,
    sheet_name: str,
    pin_name: str,
    shape: str = "bidirectional",
    side: str = "left",
    offset_mm: float = 2.54,
    add_child_label: bool = True,
) -> Dict[str, Any]:
    """Add a sheet pin to a named sheet box, optionally with the child-side label.

    The pin lands on the requested border ``side`` of the sheet box,
    ``offset_mm`` from the side's starting corner, auto-stacking 2.54 mm past
    any existing pin on the same side.  Angle follows the KiCad convention
    (left=180, right=0, top=90, bottom=270 — see :data:`_SIDE_ANGLE`).

    When ``add_child_label`` is True and the sheet's ``Sheetfile`` exists, a
    matching ``(hierarchical_label ...)`` (same name and shape) is appended to
    the child schematic, stacked vertically at x=25.4 mm starting at
    y=25.4 mm in 2.54 mm steps.

    Returns:
        On success ``{"success": True, "pin": {...}, "childLabelAdded"}``;
        on refusal ``{"success": False, "message"}``.
    """
    if shape not in _VALID_PIN_SHAPES:
        return _fail(f"Invalid shape '{shape}'. Expected one of: {', '.join(_VALID_PIN_SHAPES)}")
    if side not in _VALID_SIDES:
        return _fail(f"Invalid side '{side}'. Expected one of: {', '.join(_VALID_SIDES)}")
    if not pin_name:
        return _fail("pin_name must not be empty")

    parent = Path(parent_sch_path)
    if not parent.exists():
        return _fail(f"Parent schematic not found: {parent_sch_path}")
    try:
        content = parent.read_text(encoding="utf-8")
    except OSError as e:
        return _fail(f"Could not read parent schematic: {e}")
    if not _is_parseable_root(content):
        return _fail(f"Parent schematic is not a parseable (kicad_sch ...) file: {parent_sch_path}")

    span = _find_sheet_block(content, sheet_name)
    if span is None:
        return _fail(f"Sheet '{sheet_name}' not found in {parent.name}")
    start, end = span
    block = content[start : end + 1]

    geometry = _sheet_geometry(block)
    if geometry is None:
        return _fail(f"Sheet '{sheet_name}' has no parseable (at ...) / (size ...) geometry")

    existing = _existing_pins(block)
    if any(p[0] == sexpr.escape_sexpr_string(pin_name) for p in existing):
        return _fail(f"Sheet '{sheet_name}' already has a pin named '{pin_name}'")

    px, py = _pin_position(side, offset_mm, geometry, existing)
    x, y, w, h = geometry
    if not (x <= px <= x + w and y <= py <= y + h):
        return _fail(
            f"No room left on the {side} side of sheet '{sheet_name}' — "
            f"computed pin position ({_fmt(px)}, {_fmt(py)}) falls outside the sheet"
        )

    angle = _SIDE_ANGLE[side]
    pin_uuid = str(uuid_module.uuid4())
    pin_text = _make_pin_text(pin_name, shape, (px, py), angle, _SIDE_JUSTIFY[side], pin_uuid)

    # Insert the pin just before the line carrying the sheet block's closer.
    line_start = content.rfind("\n", start, end)
    if line_start != -1:
        insert_at = line_start + 1
        new_content = content[:insert_at] + pin_text + content[insert_at:]
    else:  # single-line sheet block — keep it valid anyway
        new_content = content[:end] + "\n" + pin_text + "\t" + content[end:]
    if not _is_parseable_root(new_content):
        return _fail("Internal error: pin insertion would corrupt the parent schematic")
    _atomic_write(parent, new_content)

    child_label_added = False
    note: Optional[str] = None
    if add_child_label:
        sheetfile = re.search(r'\(property\s+"Sheetfile"\s+"((?:[^"\\]|\\.)*)"', block)
        if not sheetfile:
            note = "sheet has no Sheetfile property; child label skipped"
        else:
            child_path = parent.parent / sheetfile.group(1).replace('\\"', '"').replace(
                "\\\\", "\\"
            )
            child_label_added, note = _append_child_label(child_path, pin_name, shape)

    result: Dict[str, Any] = {
        "success": True,
        "pin": {
            "name": pin_name,
            "shape": shape,
            "side": side,
            "position": [round(px, 4), round(py, 4)],
            "angle": angle,
            "uuid": pin_uuid,
        },
        "childLabelAdded": child_label_added,
    }
    if note:
        result["message"] = note
    return result


def _append_child_label(child_path: Path, pin_name: str, shape: str) -> Tuple[bool, Optional[str]]:
    """Append a matching hierarchical label to the child sheet.

    Returns (added, note). Never raises for a missing/unparsable child — the
    parent-side pin write has already succeeded, so this only reports.
    """
    if not child_path.exists():
        return False, f"child schematic not found ({child_path.name}); child label skipped"
    try:
        child_content = child_path.read_text(encoding="utf-8")
    except OSError as e:
        return False, f"could not read child schematic: {e}"
    if not _is_parseable_root(child_content):
        return False, f"child schematic {child_path.name} is not parseable; child label skipped"

    name_esc = re.escape(sexpr.escape_sexpr_string(pin_name))
    if re.search(r'\(hierarchical_label\s+"' + name_esc + r'"', child_content):
        return False, f"child already has a hierarchical label '{pin_name}'"

    label_count = sum(
        1 for _, _, kw in _iter_top_level_blocks(child_content) if kw == "hierarchical_label"
    )
    label_y = _CHILD_LABEL_START_Y_MM + _PIN_STEP_MM * label_count
    label_text = _make_hier_label_text(
        pin_name, shape, (_CHILD_LABEL_X_MM, label_y), str(uuid_module.uuid4())
    )
    new_child = _insert_top_level(child_content, label_text)
    if new_child is None or not _is_parseable_root(new_child):
        return False, "child label insertion would corrupt the child schematic; skipped"
    _atomic_write(child_path, new_child)
    return True, None
