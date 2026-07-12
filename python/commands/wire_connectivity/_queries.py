"""Public connectivity queries (wire connections, nets, floating labels).

Split out of the former monolithic commands/wire_connectivity.py.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from commands.pin_locator import PinLocator

logger = logging.getLogger("kicad_interface")


from ._parsing import (
    _IU_PER_MM,
    _load_sexp,
    _parse_labels_sexp,
    _parse_virtual_connections,
    _parse_wires,
    _to_iu,
    is_pwrflag_label,
)
from ._traversal import (
    _build_adjacency,
    _build_sheet_context,
    _discover_sub_sheets,
    _find_connected_wires,
    _find_pins_on_net,
    _process_single_sheet,
)


def get_wire_connections(
    schematic: Any, schematic_path: str, x_mm: float, y_mm: float
) -> Optional[Dict]:
    """Find the net name and all component pins reachable from a point via connected wires.

    The query point (x_mm, y_mm) must be exactly on a wire endpoint or junction (exact IU match).
    Interior (mid-segment) points are not matched —
    use wire endpoint coordinates obtained from the schematic data.

    Net labels and power symbols are traversed: wires on the same named net are
    treated as connected even when they are not geometrically adjacent.

    Returns dict with keys:
      - "net": str or None (net label/power name, None if unnamed)
      - "pins": list of {"component": str, "pin": str}
      - "wires": list of {"start": {"x", "y"}, "end": {"x", "y"}} in mm
      - "query_point": {"x": float, "y": float}
    Or None if no wire endpoint found within tolerance of the query point.
    """
    all_wires = _parse_wires(schematic)
    query_point = {"x": x_mm, "y": y_mm}
    if not all_wires:
        return {"net": None, "pins": [], "wires": [], "query_point": query_point}

    adjacency, iu_to_wires = _build_adjacency(all_wires)

    point_to_label, label_to_points = _parse_virtual_connections(schematic, schematic_path)

    visited, net_points = _find_connected_wires(
        x_mm,
        y_mm,
        all_wires,
        iu_to_wires,
        adjacency,
        point_to_label=point_to_label,
        label_to_points=label_to_points,
    )
    if visited is None:
        return None

    # Resolve net name: first label anchor that falls on this net's IU points.
    # Skip the PWR_FLAG sentinel — those positions are anchors for orphan-wire
    # detection but carry no real net name.  The actual net comes from a
    # #PWR symbol or a label elsewhere on the same wire.
    net: Optional[str] = None
    for pt in net_points:
        label = point_to_label.get(pt)
        if label is not None and not is_pwrflag_label(label):
            net = label
            break

    wires_out = [
        {
            "start": {
                "x": all_wires[i][0][0] / _IU_PER_MM,
                "y": all_wires[i][0][1] / _IU_PER_MM,
            },
            "end": {
                "x": all_wires[i][-1][0] / _IU_PER_MM,
                "y": all_wires[i][-1][1] / _IU_PER_MM,
            },
        }
        for i in visited
    ]

    if not hasattr(schematic, "symbol"):
        return {"net": net, "pins": [], "wires": wires_out, "query_point": query_point}

    pins = _find_pins_on_net(net_points, schematic_path, schematic)
    return {"net": net, "pins": pins, "wires": wires_out, "query_point": query_point}


def count_pins_on_net(
    schematic: Any,
    schematic_path: str,
    net_name: str,
    all_wires: List[List[Tuple[int, int]]],
    iu_to_wires: Dict[Tuple[int, int], Set[int]],
    adjacency: List[Set[int]],
    point_to_label: Dict[Tuple[int, int], str],
    label_to_points: Dict[str, List[Tuple[int, int]]],
) -> int:
    """Count the number of component pins connected to the named net.

    A pin is counted if its IU coordinate falls on the wire-network reachable
    from any label anchor for *net_name*, or directly on a label anchor of that
    net (pin directly touching a label with no intervening wire).

    Returns the count of distinct (component, pin_num) pairs on this net.
    """
    label_positions = label_to_points.get(net_name, [])
    if not label_positions:
        return 0

    # Collect the union of all net-points across all label positions for this net
    all_net_points: Set[Tuple[int, int]] = set()
    for lx, ly in label_positions:
        # Include the label anchor itself so pins directly at the label count
        all_net_points.add((lx, ly))
        # Trace from this label position into the wire graph
        x_mm = lx / _IU_PER_MM
        y_mm = ly / _IU_PER_MM
        visited, net_points = _find_connected_wires(
            x_mm,
            y_mm,
            all_wires,
            iu_to_wires,
            adjacency,
            point_to_label=point_to_label,
            label_to_points=label_to_points,
        )
        if net_points:
            all_net_points |= net_points

    if not hasattr(schematic, "symbol"):
        return 0

    locator = PinLocator()
    seen: Set[Tuple[str, str]] = set()
    ref = None
    for symbol in schematic.symbol:
        try:
            if not hasattr(symbol, "property") or not hasattr(symbol.property, "Reference"):
                continue
            ref = symbol.property.Reference.value
            # Same exclusions as _process_single_sheet: template clones and
            # virtual symbols (#PWR / #FLG) are not real pins — otherwise the
            # count disagrees with the connections list, which filters them.
            if ref.startswith("_TEMPLATE") or ref.startswith("#"):
                continue
            all_pins = locator.get_all_symbol_pins(Path(schematic_path), ref)
            if not all_pins:
                continue
            for pin_num, pin_data in all_pins.items():
                pin_iu = _to_iu(float(pin_data[0]), float(pin_data[1]))
                if pin_iu in all_net_points:
                    key = (ref, pin_num)
                    if key not in seen:
                        seen.add(key)
        except Exception as e:
            logger.warning(
                f"Error checking pins for {ref if ref is not None else '<unknown>'}: {e}"
            )

    return len(seen)


def list_floating_labels(schematic: Any, schematic_path: str) -> List[Dict[str, Any]]:
    """Return net labels that are not connected to any component pin.

    A label is "floating" when no component pin's IU coordinate falls on the
    wire-network reachable from the label's anchor position.  These labels are
    likely placed off-grid or incorrectly positioned and will cause ERC errors.

    Returns a list of dicts with keys:
      - "name": str   — the net label text
      - "x": float    — label X position in mm
      - "y": float    — label Y position in mm
      - "type": str   — "label" or "global_label"
    """
    all_wires = _parse_wires(schematic)
    if all_wires:
        adjacency, iu_to_wires = _build_adjacency(all_wires)
    else:
        adjacency = []
        iu_to_wires = {}

    point_to_label, label_to_points = _parse_virtual_connections(schematic, schematic_path)

    # Build a set of all pin IU positions for fast lookup
    pin_iu_set: Set[Tuple[int, int]] = set()
    if hasattr(schematic, "symbol"):
        locator = PinLocator()
        for symbol in schematic.symbol:
            try:
                if not hasattr(symbol, "property") or not hasattr(symbol.property, "Reference"):
                    continue
                ref = symbol.property.Reference.value
                if ref.startswith("_TEMPLATE"):
                    continue
                all_pins = locator.get_all_symbol_pins(Path(schematic_path), ref)
                if not all_pins:
                    continue
                for pin_data in all_pins.values():
                    pin_iu_set.add(_to_iu(float(pin_data[0]), float(pin_data[1])))
            except Exception as e:
                logger.warning(f"Error reading pins for floating-label check: {e}")

    floating: List[Dict[str, Any]] = []

    if not hasattr(schematic, "label"):
        return floating

    for label in schematic.label:
        try:
            if not hasattr(label, "value"):
                continue
            name = label.value
            if not hasattr(label, "at") or not hasattr(label.at, "value"):
                continue
            coords = label.at.value
            lx_mm = float(coords[0])
            ly_mm = float(coords[1])
            label_iu = _to_iu(lx_mm, ly_mm)

            # Check if the label anchor itself is a pin position
            if label_iu in pin_iu_set:
                continue

            # Trace the wire-network from this label and check for pins
            if all_wires:
                _, net_points = _find_connected_wires(
                    lx_mm,
                    ly_mm,
                    all_wires,
                    iu_to_wires,
                    adjacency,
                    point_to_label=point_to_label,
                    label_to_points=label_to_points,
                )
            else:
                net_points = None

            if net_points is not None and net_points & pin_iu_set:
                continue  # at least one pin on this net

            floating.append({"name": name, "x": lx_mm, "y": ly_mm, "type": "label"})

        except Exception as e:
            logger.warning(f"Error checking label for floating status: {e}")

    return floating


def get_net_at_point(
    schematic: Any, schematic_path: str, x_mm: float, y_mm: float
) -> Dict[str, Any]:
    """Return the net name at the given coordinate, or null if none found.

    Checks net label positions first (exact IU match within tolerance), then
    wire endpoints. Returns a dict with keys:
      - "net_name": str or None
      - "position": {"x": float, "y": float}
      - "source": "net_label" | "wire_endpoint" | None
    """
    query_iu = _to_iu(x_mm, y_mm)
    position = {"x": x_mm, "y": y_mm}

    # Build label map from schematic
    point_to_label, _ = _parse_virtual_connections(schematic, schematic_path)

    # Check if query point is exactly on a net label / power symbol position.
    # PWR_FLAG anchors are skipped — they're not net names, so the resolver
    # falls through to the wire-trace branch below.
    label_name = point_to_label.get(query_iu)
    if label_name is not None and not is_pwrflag_label(label_name):
        return {"net_name": label_name, "position": position, "source": "net_label"}

    # Check if query point is on a wire endpoint
    all_wires = _parse_wires(schematic) if hasattr(schematic, "wire") else []
    if all_wires:
        adjacency, iu_to_wires = _build_adjacency(all_wires)
        if query_iu in iu_to_wires:
            # Found a wire endpoint — trace the net to get the name
            visited, net_points = _find_connected_wires(
                x_mm,
                y_mm,
                all_wires,
                iu_to_wires,
                adjacency,
                point_to_label=point_to_label,
                label_to_points=None,
            )
            if visited is not None:
                net: Optional[str] = None
                if net_points:
                    for pt in net_points:
                        candidate = point_to_label.get(pt)
                        if candidate is not None and not is_pwrflag_label(candidate):
                            net = candidate
                            break
                return {"net_name": net, "position": position, "source": "wire_endpoint"}

    return {"net_name": None, "position": position, "source": None}


def get_connections_for_net(
    schematic: Any,
    schematic_path: str,
    net_name: str,
    sheet_contexts: Optional[Dict[Any, Any]] = None,
) -> List[Dict]:
    """Find all component pins connected to a named net across all schematic sheets.

    Recursively discovers sub-sheets, processes each sheet independently, and
    merges results. Handles label, global_label, hierarchical_label, and
    power symbol connections.

    When iterating many nets on the same schematic (e.g. list_schematic_nets),
    pass a shared ``sheet_contexts`` dict: each sheet is then parsed and its
    O(wires^2) adjacency graph built only once across the whole net loop instead
    of once per net. Omitting it (single-net callers) keeps the previous
    behaviour exactly — a fresh, call-local cache parses each sheet once.

    Returns a list of {"component": ref, "pin": pin_num} dicts.
    """
    from skip import Schematic as SkipSchematic

    cache = sheet_contexts if sheet_contexts is not None else {}
    seen: Set[Tuple[str, str]] = set()
    all_pins: List[Dict] = []

    def _collect(pins: List[Dict]) -> None:
        for pin in pins:
            key = (pin["component"], pin["pin"])
            if key not in seen:
                seen.add(key)
                all_pins.append(pin)

    if schematic_path not in cache:
        cache[schematic_path] = _build_sheet_context(schematic, schematic_path)
    top_ctx = cache[schematic_path]
    if top_ctx is not None:
        _collect(_process_single_sheet(schematic, schematic_path, net_name, context=top_ctx))

    subs_key = ("__sub_sheets__", schematic_path)
    if subs_key not in cache:
        cache[subs_key] = _discover_sub_sheets(schematic_path)
    for sub_path in cache[subs_key]:
        try:
            if sub_path not in cache:
                sub_sch = SkipSchematic(sub_path)
                cache[sub_path] = _build_sheet_context(sub_sch, sub_path)
            sub_ctx = cache[sub_path]
            if sub_ctx is not None:
                _collect(_process_single_sheet(None, sub_path, net_name, context=sub_ctx))
        except Exception as e:
            logger.warning(f"Error processing sub-sheet {sub_path}: {e}")

    return all_pins


# ---------------------------------------------------------------------------
# Power-symbol / PWR_FLAG net attachment (verification side-channel)
#
# The default net queries (get_connections_for_net, count_pins_on_net) filter
# #PWR / #FLG pins by design — they are not "real" component pins.  That makes a
# placed PWR_FLAG (or power symbol) impossible to confirm against a net without
# reading the raw file.  The helpers below surface that attachment as an
# additive side-channel, without touching the existing pin arrays or their
# filters.
# ---------------------------------------------------------------------------


def _power_symbols_on_net(schematic: Any, net_name: str) -> List[Dict[str, Any]]:
    """Return power-port (#PWR / ``power:*``) symbols that name ``net_name``.

    A power-port symbol belongs to the net equal to its ``Value`` — that is how
    the schematic joins it (its pin carries an implicit label of that name).
    Returns ``[{"ref", "pin", "value"}]``.
    """
    out: List[Dict[str, Any]] = []
    if not hasattr(schematic, "symbol"):
        return out
    for symbol in schematic.symbol:
        try:
            if not hasattr(symbol, "property") or not hasattr(symbol.property, "Reference"):
                continue
            ref = symbol.property.Reference.value
            # #FLG (PWR_FLAG) is a marker, never a named port — excluded.
            if ref.startswith("_TEMPLATE") or not ref.startswith("#PWR") or ref.startswith("#FLG"):
                continue
            lib_id = symbol.lib_id.value if hasattr(symbol, "lib_id") else ""
            if not str(lib_id or "").startswith("power:"):
                continue
            value = symbol.property.Value.value if hasattr(symbol.property, "Value") else None
            if value is not None and value == net_name:
                out.append({"ref": ref, "pin": "1", "value": value})
        except Exception as e:  # defensive: one odd symbol shouldn't kill the query
            logger.warning(f"Error reading power symbol for net '{net_name}': {e}")
    return out


def _classify_flag_attachment(
    flag_iu: Tuple[int, int],
    coords: List[float],
    labels_only_p2l: Dict[Tuple[int, int], str],
    point_to_label: Dict[Tuple[int, int], str],
    label_to_points: Dict[str, List[Tuple[int, int]]],
    all_wires: List[List[Tuple[int, int]]],
    iu_to_wires: Dict[Tuple[int, int], Set[int]],
    adjacency: List[Set[int]],
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the net a PWR_FLAG pin joins and *how* it attaches.

    Priority mirrors how the schematic joins a flag's pin — the same order the
    task documents: a net label at the pin, then a wire to it, then a coincident
    (power-port) pin.  Returns ``(net_name | None, attachment | None)`` where
    attachment is ``"label"`` | ``"wire"`` | ``"pin_coincident"``.
    """
    # 1. A real net-label element sits exactly on the flag pin (the canonical
    #    "attach a PWR_FLAG by labeling its pin" idiom).
    lbl = labels_only_p2l.get(flag_iu)
    if lbl is not None and not is_pwrflag_label(lbl):
        return lbl, "label"

    # 2. The flag pin sits on the wire network — trace out to the net's name.
    if all_wires:
        visited, net_points = _find_connected_wires(
            coords[0],
            coords[1],
            all_wires,
            iu_to_wires,
            adjacency,
            point_to_label=point_to_label,
            label_to_points=label_to_points,
        )
        if visited is not None and net_points:
            for pt in net_points:
                cand = point_to_label.get(pt)
                if cand is not None and not is_pwrflag_label(cand):
                    return cand, "wire"
            return None, "wire"  # wired, but to an unnamed net

    # 3. The flag pin coincides with a power-port pin (net from that port's
    #    Value) with no intervening label or wire.
    coincident = point_to_label.get(flag_iu)
    if coincident is not None and not is_pwrflag_label(coincident):
        return coincident, "pin_coincident"

    return None, None


def resolve_power_flags(schematic: Any, schematic_path: str) -> List[Dict[str, Any]]:
    """Resolve every PWR_FLAG (#FLG) symbol's net attachment on this sheet.

    Returns ``[{"ref", "pin", "net", "attachment"}]`` where ``net`` is the named
    net the flag's single pin joins (``None`` when it joins no named net) and
    ``attachment`` is ``"label"`` | ``"wire"`` | ``"pin_coincident"``.

    PWR_FLAG is never itself a net name (see PWRFLAG_LABEL_SENTINEL); this
    reports what rail the marker is attached to, which no other net query
    surfaces because they filter #FLG pins.
    """
    results: List[Dict[str, Any]] = []
    if not hasattr(schematic, "symbol"):
        return results

    try:
        sexp = _load_sexp(schematic_path)
    except Exception:
        sexp = None
    labels_only_p2l: Dict[Tuple[int, int], str] = {}
    if sexp is not None:
        labels_only_p2l, _ = _parse_labels_sexp(sexp)

    point_to_label, label_to_points = _parse_virtual_connections(
        schematic, schematic_path, sexp=sexp
    )
    all_wires = _parse_wires(schematic)
    if all_wires:
        adjacency, iu_to_wires = _build_adjacency(all_wires)
    else:
        adjacency, iu_to_wires = [], {}

    locator = PinLocator()
    for symbol in schematic.symbol:
        try:
            if not hasattr(symbol, "property") or not hasattr(symbol.property, "Reference"):
                continue
            ref = symbol.property.Reference.value
            if ref.startswith("_TEMPLATE") or not ref.startswith("#FLG"):
                continue
            all_pins = locator.get_all_symbol_pins(Path(schematic_path), ref)
            if not all_pins:
                continue
            for pin_num, coords in all_pins.items():
                flag_iu = _to_iu(float(coords[0]), float(coords[1]))
                net, attachment = _classify_flag_attachment(
                    flag_iu,
                    list(coords),
                    labels_only_p2l,
                    point_to_label,
                    label_to_points,
                    all_wires,
                    iu_to_wires,
                    adjacency,
                )
                results.append({"ref": ref, "pin": pin_num, "net": net, "attachment": attachment})
        except Exception as e:  # defensive: keep resolving the rest
            logger.warning(f"Error resolving power flag: {e}")
    return results


def get_power_attachments_for_net(
    schematic: Any, schematic_path: str, net_name: str
) -> Dict[str, List[Dict[str, Any]]]:
    """Power-symbol / PWR_FLAG attachment for a single net (verification aid).

    Returns ``{"power_symbols": [{ref, pin, value}],
    "power_flags": [{ref, pin, attachment}]}`` — the power ports whose Value is
    ``net_name`` and the PWR_FLAG markers whose pin joins ``net_name``.
    """
    power_symbols = _power_symbols_on_net(schematic, net_name)
    power_flags = [
        {"ref": f["ref"], "pin": f["pin"], "attachment": f["attachment"]}
        for f in resolve_power_flags(schematic, schematic_path)
        if f["net"] == net_name
    ]
    return {"power_symbols": power_symbols, "power_flags": power_flags}
