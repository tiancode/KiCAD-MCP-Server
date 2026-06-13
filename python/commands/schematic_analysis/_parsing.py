"""S-expression parsing helpers for schematic analysis.

Split out of the former monolithic commands/schematic_analysis.py.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

import sexpdata
from commands.pin_locator import PinLocator
from sexpdata import Symbol

logger = logging.getLogger("kicad_interface")


# ---------------------------------------------------------------------------
# S-expression parsing helpers
# ---------------------------------------------------------------------------


def _load_sexp(schematic_path: Path) -> list:
    """Load schematic file and return parsed S-expression data."""
    with open(schematic_path, "r", encoding="utf-8") as f:
        return sexpdata.loads(f.read())


def _parse_wires(sexp_data: list) -> List[Dict[str, Any]]:
    """
    Parse all wire segments from the schematic S-expression.

    Returns list of dicts: {start: (x_mm, y_mm), end: (x_mm, y_mm)}
    """
    wires = []
    for item in sexp_data:
        if not isinstance(item, list) or len(item) < 2:
            continue
        if item[0] != Symbol("wire"):
            continue
        pts = None
        for sub in item:
            if isinstance(sub, list) and len(sub) > 0 and sub[0] == Symbol("pts"):
                pts = sub
                break
        if not pts:
            continue
        coords = []
        for sub in pts:
            if isinstance(sub, list) and len(sub) >= 3 and sub[0] == Symbol("xy"):
                coords.append((float(sub[1]), float(sub[2])))
        if len(coords) >= 2:
            wires.append({"start": coords[0], "end": coords[1]})
    return wires


def _parse_labels(sexp_data: list) -> List[Dict[str, Any]]:
    """
    Parse all labels (label and global_label) from the schematic S-expression.

    Returns list of dicts: {name, type ('label'|'global_label'), x, y}
    """
    labels = []
    for item in sexp_data:
        if not isinstance(item, list) or len(item) < 2:
            continue
        tag = item[0]
        if tag not in (Symbol("label"), Symbol("global_label")):
            continue
        name = str(item[1]).strip('"')
        label_type = str(tag)
        x, y = 0.0, 0.0
        for sub in item:
            if isinstance(sub, list) and len(sub) >= 3 and sub[0] == Symbol("at"):
                x = float(sub[1])
                y = float(sub[2])
                break
        labels.append({"name": name, "type": label_type, "x": x, "y": y})
    return labels


def _parse_symbols(sexp_data: list) -> List[Dict[str, Any]]:
    """
    Parse all placed symbol instances from the schematic S-expression.

    Returns list of dicts: {reference, lib_id, x, y, rotation, mirror_x, mirror_y, is_power}
    """
    symbols = []
    for item in sexp_data:
        if not isinstance(item, list) or len(item) < 2:
            continue
        if item[0] != Symbol("symbol"):
            continue

        lib_id = ""
        x, y, rotation = 0.0, 0.0, 0.0
        reference = ""
        is_power = False
        mirror_x = False
        mirror_y = False

        for sub in item:
            if isinstance(sub, list) and len(sub) >= 2:
                if sub[0] == Symbol("lib_id"):
                    lib_id = str(sub[1]).strip('"')
                elif sub[0] == Symbol("at") and len(sub) >= 3:
                    x = float(sub[1])
                    y = float(sub[2])
                    if len(sub) >= 4:
                        rotation = float(sub[3])
                elif sub[0] == Symbol("mirror"):
                    m = str(sub[1])
                    if m == "x":
                        mirror_x = True
                    elif m == "y":
                        mirror_y = True
                elif sub[0] == Symbol("property") and len(sub) >= 3:
                    prop_name = str(sub[1]).strip('"')
                    if prop_name == "Reference":
                        reference = str(sub[2]).strip('"')

        is_power = reference.startswith("#PWR") or reference.startswith("#FLG")
        symbols.append(
            {
                "reference": reference,
                "lib_id": lib_id,
                "x": x,
                "y": y,
                "rotation": rotation,
                "mirror_x": mirror_x,
                "mirror_y": mirror_y,
                "is_power": is_power,
            }
        )
    return symbols


def _parse_lib_symbol_graphics(symbol_def: list) -> List[Tuple[float, float]]:
    """
    Parse graphical body elements from a lib_symbol definition and return
    local-coordinate bounding points.

    Extracts points from rectangle, polyline, circle, arc, and bezier
    elements found in sub-symbols (typically the ``_0_1`` layers that
    contain body shapes).

    Returns a list of ``(x, y)`` points in local symbol coordinates.
    """
    points: List[Tuple[float, float]] = []

    def _extract_graphics_recursive(sexp: list) -> None:
        if not isinstance(sexp, list) or len(sexp) == 0:
            return

        tag = sexp[0]

        if tag == Symbol("rectangle"):
            # (rectangle (start x y) (end x y) ...)
            for sub in sexp[1:]:
                if isinstance(sub, list) and len(sub) >= 3:
                    if sub[0] in (Symbol("start"), Symbol("end")):
                        points.append((float(sub[1]), float(sub[2])))

        elif tag == Symbol("polyline"):
            # (polyline (pts (xy x y) (xy x y) ...) ...)
            for sub in sexp[1:]:
                if isinstance(sub, list) and len(sub) > 0 and sub[0] == Symbol("pts"):
                    for pt in sub[1:]:
                        if isinstance(pt, list) and len(pt) >= 3 and pt[0] == Symbol("xy"):
                            points.append((float(pt[1]), float(pt[2])))

        elif tag == Symbol("circle"):
            # (circle (center x y) (radius r) ...)
            cx, cy, r = 0.0, 0.0, 0.0
            for sub in sexp[1:]:
                if isinstance(sub, list) and len(sub) >= 3 and sub[0] == Symbol("center"):
                    cx, cy = float(sub[1]), float(sub[2])
                elif isinstance(sub, list) and len(sub) >= 2 and sub[0] == Symbol("radius"):
                    r = float(sub[1])
            if r > 0:
                points.extend(
                    [
                        (cx - r, cy - r),
                        (cx + r, cy + r),
                    ]
                )

        elif tag == Symbol("arc"):
            # (arc (start x y) (mid x y) (end x y) ...)
            for sub in sexp[1:]:
                if isinstance(sub, list) and len(sub) >= 3:
                    if sub[0] in (Symbol("start"), Symbol("mid"), Symbol("end")):
                        points.append((float(sub[1]), float(sub[2])))

        elif tag == Symbol("bezier"):
            # (bezier (pts (xy x y) ...) ...)
            for sub in sexp[1:]:
                if isinstance(sub, list) and len(sub) > 0 and sub[0] == Symbol("pts"):
                    for pt in sub[1:]:
                        if isinstance(pt, list) and len(pt) >= 3 and pt[0] == Symbol("xy"):
                            points.append((float(pt[1]), float(pt[2])))

        else:
            # Recurse into sub-symbols to find graphics in nested definitions
            for sub in sexp[1:]:
                if isinstance(sub, list):
                    _extract_graphics_recursive(sub)

    # Search the top-level symbol definition and its sub-symbols
    for item in symbol_def[1:]:
        if isinstance(item, list):
            _extract_graphics_recursive(item)

    return points


def _extract_lib_symbols(sexp_data: list) -> Dict[str, Dict]:
    """
    Walk the lib_symbols section of already-parsed sexp_data and return
    pin definitions and graphics points for every symbol definition.

    Returns:
        Dict mapping lib_id → {"pins": pin_defs, "graphics_points": [(x,y), ...]}.
    """
    lib_symbols_section = None
    for item in sexp_data:
        if isinstance(item, list) and len(item) > 0 and item[0] == Symbol("lib_symbols"):
            lib_symbols_section = item
            break

    if not lib_symbols_section:
        return {}

    result: Dict[str, Dict] = {}
    for item in lib_symbols_section[1:]:
        if isinstance(item, list) and len(item) > 1 and item[0] == Symbol("symbol"):
            symbol_name = str(item[1]).strip('"')
            result[symbol_name] = {
                "pins": PinLocator.parse_symbol_definition(item),
                "graphics_points": _parse_lib_symbol_graphics(item),
            }
    return result
