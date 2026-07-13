"""Wire-adjacency graph traversal and multi-sheet walking.

Split out of the former monolithic commands/wire_connectivity.py.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import sexpdata
from commands.pin_locator import PinLocator
from sexpdata import Symbol

logger = logging.getLogger("kicad_interface")


from ._parsing import (
    _IU_PER_MM,
    _load_sexp,
    _parse_junctions_sexp,
    _parse_symbol_instances_sexp,
    _parse_virtual_connections,
    _parse_wires_sexp,
    _point_on_segment,
    _to_iu,
)


def _build_adjacency(
    all_wires: List[List[Tuple[int, int]]],
    junctions: Optional[Set[Tuple[int, int]]] = None,
) -> Tuple[List[Set[int]], Dict[Tuple[int, int], Set[int]]]:
    """Build wire adjacency using exact IU coordinate matching.

    Wires that share an endpoint are adjacent — this naturally handles
    junctions since all wires meeting at the same point get connected.

    Also detects T-junctions where a wire endpoint falls on the interior of
    another wire segment.  In KiCad such a mid-span touch is only an electrical
    connection when a junction dot is placed at that point; a bare touch leaves
    the two wires on separate nets (verified against kicad-cli's netlister).
    Pass ``junctions`` (the set of junction-dot IU positions, e.g. from
    :func:`_parse_junctions_sexp`) to honour that rule — a T is then bridged
    only when a junction sits on it.  When ``junctions`` is ``None`` the
    historical permissive behaviour is kept (every mid-span touch bridges), so
    callers that cannot read junction data (mock schematics, direct unit tests)
    are unaffected.

    Returns a tuple of:
      - adjacency: list of sets, one per wire, containing adjacent wire indices
      - iu_to_wires: dict mapping each IU endpoint to the set of wire indices
        that have an endpoint at that exact coordinate (used for seed queries)
    """
    # Map each IU endpoint to all wire indices that touch it
    iu_to_wires: Dict[Tuple[int, int], Set[int]] = {}
    for i, pts in enumerate(all_wires):
        for pt in pts:
            iu_to_wires.setdefault(pt, set()).add(i)

    # Detect T-junctions: a wire endpoint landing on the interior of another
    # wire segment.  When found, register the endpoint against that segment's
    # wire index so adjacency is established through the shared point — but only
    # when a junction dot actually sits there (KiCad does not auto-connect a
    # bare mid-span touch). Without junction info, keep the old permissive rule.
    all_endpoints = list(iu_to_wires.keys())
    for i, pts in enumerate(all_wires):
        if len(pts) < 2:
            continue
        ax, ay = pts[0]
        bx, by = pts[-1]
        for ep in all_endpoints:
            if ep == (ax, ay) or ep == (bx, by):
                continue
            if _point_on_segment(ep[0], ep[1], ax, ay, bx, by):
                if junctions is not None and ep not in junctions:
                    continue
                iu_to_wires[ep].add(i)

    # Wires that share an IU endpoint (including T-junction points) are adjacent
    adjacency: List[Set[int]] = [set() for _ in range(len(all_wires))]
    for wire_set in iu_to_wires.values():
        wire_list = list(wire_set)
        for a in wire_list:
            for b in wire_list:
                if a != b:
                    adjacency[a].add(b)

    return adjacency, iu_to_wires


def _find_connected_wires(
    x_mm: float,
    y_mm: float,
    all_wires: List[List[Tuple[int, int]]],
    iu_to_wires: Dict[Tuple[int, int], Set[int]],
    adjacency: List[Set[int]],
    point_to_label: Optional[Dict[Tuple[int, int], str]] = None,
    label_to_points: Optional[Dict[str, List[Tuple[int, int]]]] = None,
) -> Tuple:
    """BFS from query point. Returns (visited wire indices, net IU points) or (None, None).

    First tries exact IU match on a wire endpoint, then falls back to
    checking if the point lies on the interior of any wire segment
    (handles labels placed mid-wire).
    """
    query_iu = _to_iu(x_mm, y_mm)

    # Find seed wires: exact IU match on the query endpoint
    seed_set = iu_to_wires.get(query_iu)
    if not seed_set:
        # Fallback: check if query point lies on the interior of any wire segment
        px, py = query_iu
        for i, pts in enumerate(all_wires):
            if len(pts) >= 2 and _point_on_segment(
                px, py, pts[0][0], pts[0][1], pts[-1][0], pts[-1][1]
            ):
                seed_set = {i}
                iu_to_wires.setdefault(query_iu, set()).add(i)
                break
    if not seed_set:
        return (None, None)
    seed_indices: Set[int] = set(seed_set)

    # BFS flood-fill using pre-compiled adjacency
    visited: Set[int] = set(seed_indices)
    queue = list(seed_indices)
    net_points: Set[Tuple[int, int]] = set()
    for i in seed_indices:
        net_points.update(all_wires[i])

    seen_labels: Set[str] = set()
    while queue:
        wire_idx = queue.pop()
        for neighbor_idx in adjacency[wire_idx]:
            if neighbor_idx not in visited:
                visited.add(neighbor_idx)
                queue.append(neighbor_idx)
                net_points.update(all_wires[neighbor_idx])

        if point_to_label and label_to_points:
            for pt in all_wires[wire_idx]:
                label_name = point_to_label.get(pt)
                if label_name and label_name not in seen_labels:
                    seen_labels.add(label_name)
                    for other_pt in label_to_points.get(label_name, []):
                        if other_pt == pt:
                            continue
                        for idx in iu_to_wires.get(other_pt, set()):
                            if idx not in visited:
                                visited.add(idx)
                                queue.append(idx)
                                net_points.update(all_wires[idx])

    return (visited, net_points)


def _find_pins_on_net(
    net_points: Set[Tuple[int, int]],
    schematic_path: Any,
    schematic: Any,
    sexp: Optional[list] = None,
    instances: Optional[list] = None,
) -> List[Dict]:
    """Find component pins that land on net points.

    Parses symbol instances directly from sexpdata to avoid kicad-skip's
    collection iteration issues.  Uses exact IU matching first, then falls
    back to a ±1 IU tolerance for floating-point rounding edge cases.

    Returns a list of {"component": ref, "pin": pin_num} dicts.
    """

    def _on_net(px_mm: float, py_mm: float) -> bool:
        pt = _to_iu(px_mm, py_mm)
        if pt in net_points:
            return True
        ix, iy = pt
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if (ix + dx, iy + dy) in net_points:
                    return True
        return False

    if sexp is None:
        sexp = _load_sexp(schematic_path)

    logger.debug(f"Searching {len(net_points)} net points for matching pins")

    locator = PinLocator()
    if instances is None:
        instances = _parse_symbol_instances_sexp(sexp)
    logger.debug(f"Found {len(instances)} symbol instances via sexpdata")

    pins: List[Dict] = []
    seen: Set[Tuple[str, str]] = set()

    for inst in instances:
        ref = inst["ref"]
        try:
            if ref.startswith("_TEMPLATE") or ref.startswith("#"):
                continue

            lib_id = inst["lib_id"]
            pin_defs = locator.get_symbol_pins(Path(schematic_path), lib_id)
            if not pin_defs:
                logger.debug(f"  {ref}: no pin definitions for lib_id={lib_id}")
                continue

            sym_x = inst["x"]
            sym_y = inst["y"]
            sym_rot = inst["rotation"]
            mirror_x = inst["mirror_x"]
            mirror_y = inst["mirror_y"]

            for pin_num, pdata in pin_defs.items():
                px, py = pdata["x"], pdata["y"]
                # y-negate: lib_symbols y-up → schematic y-down
                py = -py
                if mirror_x:
                    py = -py
                if mirror_y:
                    px = -px
                if sym_rot != 0:
                    px, py = locator.rotate_point(px, py, sym_rot)
                abs_x = sym_x + px
                abs_y = sym_y + py
                if _on_net(abs_x, abs_y):
                    key = (ref, pin_num)
                    if key not in seen:
                        seen.add(key)
                        pins.append({"component": ref, "pin": pin_num})
        except Exception as e:
            logger.warning(f"Error checking pins for {ref}: {e}")

    return pins


# ---------------------------------------------------------------------------
# Multi-sheet (hierarchical) connectivity
#
# The functions below extend single-sheet net tracing to hierarchical KiCad
# projects: ``get_connections_for_net`` discovers and recurses into every
# referenced sub-sheet, processing each one with ``_process_single_sheet``
# (which uses the sexp-based parsers above for reliability across all label
# kinds, including ``hierarchical_label``).
# ---------------------------------------------------------------------------


def _discover_sub_sheets(schematic_path: str) -> List[str]:
    """Recursively discover all sub-sheet .kicad_sch files referenced by the schematic.

    Returns a list of absolute paths to sub-sheet files (does NOT include the
    top-level schematic_path itself).
    """
    parent_dir = Path(schematic_path).parent
    result: List[str] = []
    try:
        with open(schematic_path, "r", encoding="utf-8") as f:
            content = f.read()
        sexp = sexpdata.loads(content)
    except Exception as e:
        logger.warning(f"Could not parse {schematic_path} for sub-sheets: {e}")
        return result

    for item in sexp:
        if not isinstance(item, list) or not item or item[0] != Symbol("sheet"):
            continue
        for sub in item:
            if not isinstance(sub, list) or len(sub) < 3:
                continue
            if sub[0] != Symbol("property"):
                continue
            prop_name = str(sub[1]).strip('"')
            if prop_name == "Sheetfile":
                sheet_file = str(sub[2]).strip('"')
                sheet_path = parent_dir / sheet_file
                if sheet_path.exists():
                    abs_path = str(sheet_path.resolve())
                    result.append(abs_path)
                    result.extend(_discover_sub_sheets(abs_path))
                else:
                    logger.warning(f"Sub-sheet not found: {sheet_path}")
    return result


def _build_sheet_context(
    schematic: Any, schematic_path: str, sexp: Optional[list] = None
) -> Optional[Dict[str, Any]]:
    """Parse and index one sheet once so the result can be reused across nets.

    ``_process_single_sheet`` used to re-load the sexp and rebuild the
    wire-adjacency graph (whose T-junction detection is O(wires^2)) for *every*
    net, making ``list_schematic_nets`` O(nets * wires^2).  A caller iterating
    many nets passes a shared cache of these contexts (see
    ``get_connections_for_net``) so each sheet is parsed and indexed only once,
    dropping the cost to O(sheets * wires^2).

    Returns ``None`` when the sheet cannot be read.
    """
    if sexp is None:
        try:
            sexp = _load_sexp(schematic_path)
        except Exception as e:
            logger.warning(f"Could not load sexp for {schematic_path}: {e}")
            return None

    all_wires = _parse_wires_sexp(sexp)
    adjacency: List[Set[int]] = []
    iu_to_wires: Dict[Tuple[int, int], Set[int]] = {}
    if all_wires:
        adjacency, iu_to_wires = _build_adjacency(all_wires, _parse_junctions_sexp(sexp))

    point_to_label, label_to_points = _parse_virtual_connections(
        schematic, schematic_path, sexp=sexp
    )

    return {
        "sexp": sexp,
        "all_wires": all_wires,
        "adjacency": adjacency,
        "iu_to_wires": iu_to_wires,
        "point_to_label": point_to_label,
        "label_to_points": label_to_points,
        "instances": _parse_symbol_instances_sexp(sexp),
    }


def _process_single_sheet(
    schematic: Any,
    schematic_path: str,
    net_name: str,
    context: Optional[Dict[str, Any]] = None,
) -> List[Dict]:
    """Find pins connected to *net_name* on a single schematic sheet.

    Handles label, global_label, hierarchical_label, and power symbols.
    All wire and label data is parsed directly from the raw .kicad_sch file
    via sexpdata for maximum reliability.

    ``context`` is the per-sheet parse/index produced by ``_build_sheet_context``;
    when omitted it is built on the fly (single-net path). Passing a shared one
    across many nets is what avoids the per-net O(wires^2) adjacency rebuild.
    """
    if context is None:
        context = _build_sheet_context(schematic, schematic_path)
        if context is None:
            return []

    sexp = context["sexp"]
    all_wires = context["all_wires"]
    adjacency = context["adjacency"]
    iu_to_wires = context["iu_to_wires"]
    point_to_label = context["point_to_label"]
    label_to_points = context["label_to_points"]

    seed_positions = label_to_points.get(net_name, [])
    if not seed_positions:
        logger.debug(f"No label positions found for net '{net_name}' in {schematic_path}")
        return []

    logger.debug(
        f"Net '{net_name}': {len(seed_positions)} seed position(s) — "
        f"{[f'({p[0]/10000},{p[1]/10000})' for p in seed_positions]}"
    )

    net_points: Set[Tuple[int, int]] = set()

    for seed_pt in seed_positions:
        net_points.add(seed_pt)
        if not all_wires:
            continue
        visited, pts = _find_connected_wires(
            seed_pt[0] / _IU_PER_MM,
            seed_pt[1] / _IU_PER_MM,
            all_wires,
            iu_to_wires,
            adjacency,
            point_to_label=point_to_label,
            label_to_points=label_to_points,
        )
        if pts:
            logger.debug(
                f"BFS from seed ({seed_pt[0]/10000},{seed_pt[1]/10000}) "
                f"found {len(pts)} points via {len(visited) if visited else 0} wires"
            )
            net_points.update(pts)
        else:
            logger.debug(
                f"BFS from seed ({seed_pt[0]/10000},{seed_pt[1]/10000}) "
                f"found NO connected wires"
            )

    logger.debug(f"Net '{net_name}': total {len(net_points)} IU points in net after BFS")

    return _find_pins_on_net(
        net_points, schematic_path, schematic, sexp=sexp, instances=context["instances"]
    )
