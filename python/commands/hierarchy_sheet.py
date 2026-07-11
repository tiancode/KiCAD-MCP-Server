"""Hierarchical-sheet authoring for ``.kicad_sch`` files (KiCad 9/10 format).

Two public entry points:

* :func:`create_hierarchical_sheet` — insert a ``(sheet ...)`` box into a
  parent schematic (optionally creating the referenced child file).
* :func:`add_sheet_pin` — add a ``(pin ...)`` on a named sheet's border and,
  optionally, the matching ``(hierarchical_label ...)`` in the child sheet.

This module is a thin *composition* layer: the S-expression emission is owned
by :class:`commands.wire_manager.WireManager` (``add_sheet`` / ``add_sheet_pin``
/ ``add_hierarchical_label``), child-file creation by
:meth:`commands.schematic.SchematicManager.create_schematic`, and atomic writes
by :func:`commands.schematic_locks.atomic_write_text`.  What lives here is the
value the callers layer on top of those primitives: auto-stacked sheet-pin
positions per border side, on-demand child-file creation, and the matching
vertically-stacked hierarchical label written into the child schematic.

Sheet pins use the KiCad angle convention: a pin on the *left* edge has angle
180, *right* edge angle 0, *top* edge angle 90, *bottom* edge angle 270 (see
:data:`_SIDE_ANGLE`).
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from sexpdata import Symbol

from commands.schematic_locks import atomic_write_text
from commands.wire_manager import WireManager

__all__ = ["create_hierarchical_sheet", "add_sheet_pin"]

_VALID_PIN_SHAPES = ("input", "output", "bidirectional", "tri_state", "passive")
_VALID_SIDES = ("left", "right", "top", "bottom")

# KiCad sheet-pin angle convention per border side (see module docstring).
_SIDE_ANGLE: Dict[str, int] = {"left": 180, "right": 0, "top": 90, "bottom": 270}

_PIN_STEP_MM = 2.54
_CHILD_LABEL_X_MM = 25.4
_CHILD_LABEL_START_Y_MM = 25.4

_SYM_KICAD_SCH = Symbol("kicad_sch")
_SYM_SHEET = Symbol("sheet")
_SYM_PROPERTY = Symbol("property")
_SYM_PIN = Symbol("pin")
_SYM_AT = Symbol("at")
_SYM_SIZE = Symbol("size")
_SYM_UUID = Symbol("uuid")
_SYM_HIER_LABEL = Symbol("hierarchical_label")


# ---------------------------------------------------------------------------
# Low-level helpers (inspection only — all emission is delegated)
# ---------------------------------------------------------------------------


def _fail(message: str) -> Dict[str, Any]:
    return {"success": False, "message": message}


def _load_root(content: str) -> Optional[list]:
    """Parse ``content``; return the tree iff it is a ``(kicad_sch ...)`` root."""
    try:
        tree = sexpdata.loads(content)
    except Exception:
        return None
    if isinstance(tree, list) and tree and tree[0] == _SYM_KICAD_SCH:
        return tree
    return None


def _child_lists(node: list, head: Symbol) -> List[list]:
    """Direct child S-expressions of ``node`` whose head symbol is ``head``."""
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == head]


def _sub(node: list, head: Symbol) -> Optional[list]:
    """First direct child of ``node`` whose head symbol is ``head``."""
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == head:
            return c
    return None


def _property_value(sheet: list, prop_name: str) -> Optional[str]:
    """Value of the ``(property "<prop_name>" "<value>" ...)`` in a sheet block."""
    for prop in _child_lists(sheet, _SYM_PROPERTY):
        if len(prop) >= 3 and str(prop[1]) == prop_name:
            return str(prop[2])
    return None


def _sheet_name(sheet: list) -> Optional[str]:
    return _property_value(sheet, "Sheetname")


def _find_sheet(tree: list, sheet_name: str) -> Optional[list]:
    """Top-level ``(sheet ...)`` block whose Sheetname matches, or None."""
    for sheet in _child_lists(tree, _SYM_SHEET):
        if _sheet_name(sheet) == sheet_name:
            return sheet
    return None


def _sheet_geometry(sheet: list) -> Optional[Tuple[float, float, float, float]]:
    """Return (x, y, w, h) from a sheet block's (at ...) / (size ...)."""
    at = _sub(sheet, _SYM_AT)
    size = _sub(sheet, _SYM_SIZE)
    if not (at and len(at) >= 3 and size and len(size) >= 3):
        return None
    return float(at[1]), float(at[2]), float(size[1]), float(size[2])


def _existing_pins(sheet: list) -> List[Tuple[str, float, float, int]]:
    """All (name, x, y, angle) sheet pins declared inside a sheet block."""
    pins: List[Tuple[str, float, float, int]] = []
    for pin in _child_lists(sheet, _SYM_PIN):
        if len(pin) < 2:
            continue
        name = str(pin[1])
        at = _sub(pin, _SYM_AT)
        if at and len(at) >= 4:
            pins.append((name, float(at[1]), float(at[2]), int(float(at[3]))))
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

    tree = _load_root(content)
    if tree is None:
        return _fail(f"Parent schematic is not a parseable (kicad_sch ...) file: {parent_sch_path}")

    if not WireManager._root_schematic_uuid(content):
        return _fail(
            "Parent schematic has no top-level (uuid ...) — cannot key the sheet's "
            "(instances ...) page number"
        )

    if _find_sheet(tree, sheet_name) is not None:
        return _fail(f"A sheet named '{sheet_name}' already exists in {parent.name}")

    # Create the child file first (mirrors handle_add_schematic_sheet) so a
    # write failure leaves the parent untouched.
    child_created = False
    child_path = parent.parent / child_filename
    if create_child and not child_path.exists():
        from commands.schematic import SchematicManager

        child_path.parent.mkdir(parents=True, exist_ok=True)
        SchematicManager.create_schematic(
            child_path.stem,
            path=str(child_path.parent),
            template="empty.kicad_sch",
        )
        child_created = True

    success, info = WireManager.add_sheet(
        parent,
        sheet_name,
        child_filename,
        [float(position[0]), float(position[1])],
        size=[float(size[0]), float(size[1])],
    )
    if not success:
        return _fail(f"Failed to insert sheet: {info.get('error', 'unknown error')}")

    return {
        "success": True,
        "sheetName": sheet_name,
        "sheetFile": child_filename,
        "uuid": info.get("uuid"),
        "page": info.get("page"),
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

    tree = _load_root(content)
    if tree is None:
        return _fail(f"Parent schematic is not a parseable (kicad_sch ...) file: {parent_sch_path}")

    sheet = _find_sheet(tree, sheet_name)
    if sheet is None:
        return _fail(f"Sheet '{sheet_name}' not found in {parent.name}")

    geometry = _sheet_geometry(sheet)
    if geometry is None:
        return _fail(f"Sheet '{sheet_name}' has no parseable (at ...) / (size ...) geometry")

    existing = _existing_pins(sheet)
    if any(p[0] == pin_name for p in existing):
        return _fail(f"Sheet '{sheet_name}' already has a pin named '{pin_name}'")

    px, py = _pin_position(side, offset_mm, geometry, existing)
    px, py = round(px, 4), round(py, 4)
    x, y, w, h = geometry
    if not (x <= px <= x + w and y <= py <= y + h):
        return _fail(
            f"No room left on the {side} side of sheet '{sheet_name}' — "
            f"computed pin position ({px:g}, {py:g}) falls outside the sheet"
        )

    angle = _SIDE_ANGLE[side]
    new_content, ok = WireManager.add_sheet_pin(
        content, sheet_name, pin_name, shape, [px, py], orientation=angle
    )
    if not ok or _load_root(new_content) is None:
        return _fail("Internal error: pin insertion would corrupt the parent schematic")
    atomic_write_text(parent, new_content)

    child_label_added = False
    note: Optional[str] = None
    if add_child_label:
        sheetfile = _property_value(sheet, "Sheetfile")
        if not sheetfile:
            note = "sheet has no Sheetfile property; child label skipped"
        else:
            child_label_added, note = _append_child_label(
                parent.parent / sheetfile, pin_name, shape
            )

    result: Dict[str, Any] = {
        "success": True,
        "pin": {
            "name": pin_name,
            "shape": shape,
            "side": side,
            "position": [px, py],
            "angle": angle,
            "uuid": _last_pin_uuid(new_content, pin_name),
        },
        "childLabelAdded": child_label_added,
    }
    if note:
        result["message"] = note
    return result


def _last_pin_uuid(content: str, pin_name: str) -> Optional[str]:
    """Best-effort: the uuid of the just-inserted named sheet pin."""
    tree = _load_root(content)
    if tree is None:
        return None
    for sheet in _child_lists(tree, _SYM_SHEET):
        for pin in _child_lists(sheet, _SYM_PIN):
            if len(pin) >= 2 and str(pin[1]) == pin_name:
                uid = _sub(pin, _SYM_UUID)
                if uid and len(uid) >= 2:
                    return str(uid[1])
    return None


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

    tree = _load_root(child_content)
    if tree is None:
        return False, f"child schematic {child_path.name} is not parseable; child label skipped"

    labels = _child_lists(tree, _SYM_HIER_LABEL)
    if any(len(lbl) >= 2 and str(lbl[1]) == pin_name for lbl in labels):
        return False, f"child already has a hierarchical label '{pin_name}'"

    label_y = _CHILD_LABEL_START_Y_MM + _PIN_STEP_MM * len(labels)
    ok = WireManager.add_hierarchical_label(
        child_path, pin_name, [_CHILD_LABEL_X_MM, label_y], shape=shape, orientation=0
    )
    if not ok:
        return False, "child label insertion failed; skipped"
    return True, None
