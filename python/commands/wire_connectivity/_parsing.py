"""S-expression parsing, IU conversion, and PWR_FLAG predicate.

Split out of the former monolithic commands/wire_connectivity.py.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import sexpdata
from sexpdata import Symbol
from commands.pin_locator import PinLocator

logger = logging.getLogger("kicad_interface")


_IU_PER_MM = 10000  # KiCad schematic internal units per millimeter


# Sentinel registered into ``point_to_label`` for #FLG (PWR_FLAG) symbol
# pins.  We want orphan-wire detection to treat the position as an anchor
# (so a wire terminating only on a PWR_FLAG isn't reported as dangling),
# but PWR_FLAG is NOT a net name — kicad uses it only as an ERC marker.
# A non-net-name sentinel keeps net-resolution from accidentally
# surfacing "PWR_FLAG" as the wire's net.  Use ``is_pwrflag_label`` to
# test for it; never compare against the literal string elsewhere.
PWRFLAG_LABEL_SENTINEL = "__pwrflag_anchor__"


def is_pwrflag_label(label: Optional[str]) -> bool:
    """True iff ``label`` is the PWR_FLAG anchor sentinel."""
    return label == PWRFLAG_LABEL_SENTINEL


def _to_iu(x_mm: float, y_mm: float) -> Tuple[int, int]:
    """Convert mm coordinates to KiCad internal units (integer)."""
    return (round(x_mm * _IU_PER_MM), round(y_mm * _IU_PER_MM))


def _load_sexp(schematic_path: str) -> list:
    """Load and cache the raw sexpdata tree for a schematic file."""
    with open(schematic_path, "r", encoding="utf-8") as f:
        return sexpdata.loads(f.read())


def _parse_wires_sexp(sexp: list) -> List[List[Tuple[int, int]]]:
    """Extract wire endpoints from raw sexpdata as IU tuples.

    Parses ``(wire (pts (xy X Y) (xy X Y)))`` directly, bypassing
    kicad-skip which may silently drop elements.
    """
    all_wires: List[List[Tuple[int, int]]] = []
    for item in sexp:
        if not isinstance(item, list) or not item:
            continue
        if item[0] != Symbol("wire"):
            continue
        for sub in item:
            if not isinstance(sub, list) or not sub or sub[0] != Symbol("pts"):
                continue
            pts: List[Tuple[int, int]] = []
            for xy_elem in sub[1:]:
                if isinstance(xy_elem, list) and len(xy_elem) >= 3 and xy_elem[0] == Symbol("xy"):
                    pts.append(_to_iu(float(xy_elem[1]), float(xy_elem[2])))
            if len(pts) >= 2:
                all_wires.append(pts)
    return all_wires


def _parse_wires(schematic: Any) -> List[List[Tuple[int, int]]]:
    """Extract wire endpoints from a kicad-skip schematic object as IU tuples.

    Used by the single-sheet handlers (``get_wire_connections``,
    ``list_floating_labels``, ``get_net_at_point``) which receive a kicad-skip
    schematic. Multi-sheet code paths use :func:`_parse_wires_sexp` instead.
    """
    all_wires: List[List[Tuple[int, int]]] = []
    if not hasattr(schematic, "wire"):
        return all_wires
    for wire in schematic.wire:
        if hasattr(wire, "pts") and hasattr(wire.pts, "xy"):
            pts: List[Tuple[int, int]] = []
            for point in wire.pts.xy:
                if hasattr(point, "value"):
                    pts.append(_to_iu(float(point.value[0]), float(point.value[1])))
            if len(pts) >= 2:
                all_wires.append(pts)
    return all_wires


def _parse_labels_sexp(
    sexp: list,
) -> Tuple[Dict[Tuple[int, int], str], Dict[str, List[Tuple[int, int]]]]:
    """Parse label, global_label, and hierarchical_label from raw sexpdata.

    Returns (point_to_label, label_to_points) in IU coordinates.
    Bypasses kicad-skip which may not iterate all labels correctly.
    """
    point_to_label: Dict[Tuple[int, int], str] = {}
    label_to_points: Dict[str, List[Tuple[int, int]]] = {}

    label_types = {Symbol("label"), Symbol("global_label"), Symbol("hierarchical_label")}

    for item in sexp:
        if not isinstance(item, list) or len(item) < 2:
            continue
        if item[0] not in label_types:
            continue
        name = str(item[1]).strip('"')
        for sub in item[2:]:
            if isinstance(sub, list) and sub and sub[0] == Symbol("at") and len(sub) >= 3:
                pt = _to_iu(float(sub[1]), float(sub[2]))
                point_to_label[pt] = name
                label_to_points.setdefault(name, []).append(pt)
                logger.debug(
                    f"Parsed {item[0]} '{name}' at IU {pt} "
                    f"(mm {float(sub[1])}, {float(sub[2])})"
                )
                break

    return point_to_label, label_to_points


def _point_on_segment(px: int, py: int, ax: int, ay: int, bx: int, by: int) -> bool:
    """Check if point (px,py) lies strictly between endpoints (ax,ay)-(bx,by).

    Only handles axis-aligned (horizontal/vertical) segments, which covers
    virtually all KiCad schematic wires.
    """
    if ay == by == py:
        lo, hi = (ax, bx) if ax < bx else (bx, ax)
        return lo < px < hi
    if ax == bx == px:
        lo, hi = (ay, by) if ay < by else (by, ay)
        return lo < py < hi
    return False


def _parse_virtual_connections(
    schematic: Any, schematic_path: Any, sexp: Optional[list] = None
) -> Tuple[Dict[Tuple[int, int], str], Dict[str, List[Tuple[int, int]]]]:
    """Return virtual connectivity from net labels, global labels, and power symbols.

    Labels (label, global_label, hierarchical_label) are parsed directly from the
    raw sexpdata tree for reliability — kicad-skip's collection iteration can
    silently miss elements. If the sexp tree cannot be loaded (e.g. the path
    does not exist in unit tests), falls back to kicad-skip's ``schematic.label``
    so callers that pass a mock schematic still get the labels they registered.

    Power symbols are still resolved via kicad-skip's symbol collection.

    Returns a tuple of:
      - point_to_label: Dict[Tuple[int,int], str] — IU position → label name
      - label_to_points: Dict[str, List[Tuple[int,int]]] — label name → list of IU positions
    """
    point_to_label: Dict[Tuple[int, int], str] = {}
    label_to_points: Dict[str, List[Tuple[int, int]]] = {}

    if sexp is None:
        try:
            sexp = _load_sexp(schematic_path)
        except Exception as e:
            logger.debug(
                f"Could not load sexp for {schematic_path} ({e}); "
                "falling back to kicad-skip label collection"
            )
            sexp = None

    if sexp is not None:
        point_to_label, label_to_points = _parse_labels_sexp(sexp)
        logger.debug(
            f"Parsed {sum(len(v) for v in label_to_points.values())} label instances "
            f"across {len(label_to_points)} unique net names from {schematic_path}"
        )
    else:
        for attr in ("label", "global_label"):
            if not hasattr(schematic, attr):
                continue
            for label in getattr(schematic, attr):
                try:
                    if not hasattr(label, "value"):
                        continue
                    name = label.value
                    if not hasattr(label, "at") or not hasattr(label.at, "value"):
                        continue
                    coords = label.at.value
                    pt = _to_iu(float(coords[0]), float(coords[1]))
                    point_to_label[pt] = name
                    label_to_points.setdefault(name, []).append(pt)
                except Exception as e:
                    logger.warning(f"Error parsing net label: {e}")

    if hasattr(schematic, "symbol"):
        locator = PinLocator()
        for symbol in schematic.symbol:
            try:
                if not hasattr(symbol, "property") or not hasattr(symbol.property, "Reference"):
                    continue
                ref = symbol.property.Reference.value
                is_pwr_port = ref.startswith("#PWR")
                is_pwr_flag = ref.startswith("#FLG")
                if not (is_pwr_port or is_pwr_flag):
                    continue
                if ref.startswith("_TEMPLATE"):
                    continue
                if not hasattr(symbol.property, "Value"):
                    continue
                name = symbol.property.Value.value
                all_pins = locator.get_all_symbol_pins(Path(schematic_path), ref)
                if not all_pins or "1" not in all_pins:
                    continue
                pin_data = all_pins["1"]
                pt = _to_iu(float(pin_data[0]), float(pin_data[1]))

                if is_pwr_port:
                    # Power-port symbol: Value is the net name (+BATT, GND, ...).
                    # Register in both maps so BFS-via-label-jump can bridge to
                    # other instances of the same named power net.
                    point_to_label[pt] = name
                    label_to_points.setdefault(name, []).append(pt)
                else:
                    # Power-flag symbol (#FLG*): the schematic Value is always
                    # the literal "PWR_FLAG", which is an ERC marker — NOT a
                    # net name.  Register the pin position with a sentinel so
                    # ``find_orphaned_wires`` still treats it as an anchor (a
                    # wire terminating only on a PWR_FLAG isn't dangling),
                    # but net-name resolvers can filter the sentinel out via
                    # ``is_pwrflag_label`` and avoid surfacing "PWR_FLAG" as
                    # the wire's net.
                    # It is NOT added to label_to_points: doing so would let
                    # BFS-via-label-jump virtually bridge every distinct power
                    # rail that has a pwr-flag into one mega-net (every #FLG
                    # shares Value="PWR_FLAG"). The pwr-flag remains
                    # electrically connected to its rail via the wire-graph
                    # BFS through the wire it sits on.
                    # setdefault avoids clobbering an upstream power-port label
                    # in the unlikely case that one sits at the same point.
                    point_to_label.setdefault(pt, PWRFLAG_LABEL_SENTINEL)
            except Exception as e:
                logger.warning(f"Error parsing power symbol: {e}")

    return point_to_label, label_to_points


def _parse_symbol_instances_sexp(
    sexp: list,
) -> List[Dict]:
    """Parse all placed symbol instances from raw sexpdata.

    Returns a list of dicts with keys: ref, lib_id, x, y, rotation, mirror_x, mirror_y.
    Bypasses kicad-skip's symbol collection which may miss elements.
    """
    instances: List[Dict] = []
    for item in sexp:
        if not isinstance(item, list) or not item or item[0] != Symbol("symbol"):
            continue

        inst: Dict = {
            "ref": None,
            "lib_id": None,
            "x": 0.0,
            "y": 0.0,
            "rotation": 0.0,
            "mirror_x": False,
            "mirror_y": False,
        }

        for sub in item[1:]:
            if not isinstance(sub, list) or not sub:
                continue
            tag = sub[0]
            if tag == Symbol("lib_id") and len(sub) >= 2:
                inst["lib_id"] = str(sub[1]).strip('"')
            elif tag == Symbol("at") and len(sub) >= 3:
                inst["x"] = float(sub[1])
                inst["y"] = float(sub[2])
                if len(sub) >= 4:
                    inst["rotation"] = float(sub[3])
            elif tag == Symbol("mirror"):
                if len(sub) >= 2:
                    mv = str(sub[1]).strip('"')
                    if mv == "x":
                        inst["mirror_x"] = True
                    elif mv == "y":
                        inst["mirror_y"] = True
            elif tag == Symbol("property") and len(sub) >= 3:
                prop_name = str(sub[1]).strip('"')
                if prop_name == "Reference":
                    inst["ref"] = str(sub[2]).strip('"')

        if inst["ref"] and inst["lib_id"]:
            instances.append(inst)

    return instances
