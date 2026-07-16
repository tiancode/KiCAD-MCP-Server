"""S-expression parsing helpers for schematic analysis.

Split out of the former monolithic commands/schematic_analysis.py.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

import sexpdata
from commands.pin_locator import _UNIT_SUFFIX_RE, PinLocator
from sexpdata import Symbol

logger = logging.getLogger("kicad_interface")


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

    Returns list of dicts: {reference, lib_id, x, y, rotation, mirror_x,
    mirror_y, is_power, unit}. ``unit`` is the placed instance's ``(unit N)``
    (default 1) — a multi-unit part is placed once per unit under the same
    reference, and each instance's bounding box must use only *its* unit's pins
    (see _geometry), or unit-B pins drawn at the shared library origin inflate
    unit A's box into a phantom overlap (F4).
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
        unit = 1

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
                elif sub[0] == Symbol("unit") and len(sub) >= 2:
                    try:
                        unit = int(sub[1])
                    except (TypeError, ValueError):
                        unit = 1
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
                "unit": unit,
            }
        )
    return symbols


def _parse_lib_symbol_graphics_by_unit(symbol_def: list) -> Dict[int, List[Tuple[float, float]]]:
    """
    Parse graphical body elements from a lib_symbol definition, grouped by the
    symbol unit that owns them.

    Body shapes live in ``<base>_<unit>_<style>`` sub-symbols: the ``_0_1``
    layer holds common graphics drawn on every unit (unit 0), while a
    multi-unit part draws each unit's body in its own ``_<unit>_1`` sub-symbol.
    Grouping by unit lets a placed instance's bounding box use only ITS unit's
    body — otherwise unit B's rectangle, drawn at the shared library origin,
    inflates unit A's box into a phantom overlap (F4).

    Returns ``{unit: [(x, y), ...]}`` in local symbol coordinates.
    """
    by_unit: Dict[int, List[Tuple[float, float]]] = {}

    def _add(unit: int, pt: Tuple[float, float]) -> None:
        by_unit.setdefault(unit, []).append(pt)

    def _add_pts_xy(node: list, unit: int) -> None:
        """Add every ``(xy x y)`` point under this node's ``(pts …)`` list."""
        for sub in node[1:]:
            if isinstance(sub, list) and len(sub) > 0 and sub[0] == Symbol("pts"):
                for pt in sub[1:]:
                    if isinstance(pt, list) and len(pt) >= 3 and pt[0] == Symbol("xy"):
                        _add(unit, (float(pt[1]), float(pt[2])))

    def _recurse(sexp: Any, unit: int) -> None:
        if not isinstance(sexp, list) or len(sexp) == 0:
            return

        tag = sexp[0]

        # Descending into a unit sub-symbol switches the unit context.
        if tag == Symbol("symbol") and len(sexp) > 1:
            match = _UNIT_SUFFIX_RE.search(str(sexp[1]).strip('"'))
            if match:
                unit = int(match.group(1))

        if tag == Symbol("rectangle"):
            for sub in sexp[1:]:
                if isinstance(sub, list) and len(sub) >= 3:
                    if sub[0] in (Symbol("start"), Symbol("end")):
                        _add(unit, (float(sub[1]), float(sub[2])))
        elif tag == Symbol("polyline"):
            _add_pts_xy(sexp, unit)
        elif tag == Symbol("circle"):
            cx, cy, r = 0.0, 0.0, 0.0
            for sub in sexp[1:]:
                if isinstance(sub, list) and len(sub) >= 3 and sub[0] == Symbol("center"):
                    cx, cy = float(sub[1]), float(sub[2])
                elif isinstance(sub, list) and len(sub) >= 2 and sub[0] == Symbol("radius"):
                    r = float(sub[1])
            if r > 0:
                _add(unit, (cx - r, cy - r))
                _add(unit, (cx + r, cy + r))
        elif tag == Symbol("arc"):
            for sub in sexp[1:]:
                if isinstance(sub, list) and len(sub) >= 3:
                    if sub[0] in (Symbol("start"), Symbol("mid"), Symbol("end")):
                        _add(unit, (float(sub[1]), float(sub[2])))
        elif tag == Symbol("bezier"):
            _add_pts_xy(sexp, unit)

        # Recurse into sub-lists, carrying the current unit context.
        for sub in sexp[1:]:
            if isinstance(sub, list):
                _recurse(sub, unit)

    for item in symbol_def[1:]:
        if isinstance(item, list):
            _recurse(item, 0)

    return by_unit


def _parse_lib_symbol_graphics(symbol_def: list) -> List[Tuple[float, float]]:
    """
    Parse graphical body elements from a lib_symbol definition and return the
    flat list of local-coordinate bounding points (all units combined).

    Kept for backward compatibility; unit-aware callers should use
    ``_parse_lib_symbol_graphics_by_unit`` so a placed instance's box uses only
    its own unit's body.
    """
    points: List[Tuple[float, float]] = []
    for pts in _parse_lib_symbol_graphics_by_unit(symbol_def).values():
        points.extend(pts)
    return points


def _extract_lib_symbols(sexp_data: list) -> Dict[str, Dict]:
    """
    Walk the lib_symbols section of already-parsed sexp_data and return
    pin definitions and graphics points for every symbol definition.

    Returns:
        Dict mapping lib_id → {"pins": pin_defs, "graphics_points": [(x,y), ...],
        "graphics_by_unit": {unit: [(x,y), ...]}}. ``graphics_by_unit`` lets a
        multi-unit part's per-instance bounding box use only its unit's body.
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
            graphics_by_unit = _parse_lib_symbol_graphics_by_unit(item)
            flat: List[Tuple[float, float]] = []
            for pts in graphics_by_unit.values():
                flat.extend(pts)
            result[symbol_name] = {
                "pins": PinLocator.parse_symbol_definition(item),
                "graphics_points": flat,
                "graphics_by_unit": graphics_by_unit,
            }
    return result
