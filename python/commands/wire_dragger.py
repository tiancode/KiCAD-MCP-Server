"""
WireDragger — drag connected wires when a schematic component is moved.

All methods operate on in-memory sexpdata lists (no disk I/O).
"""

import logging
import math
import uuid
from collections import Counter
from typing import Any, Dict, Iterator, List, Optional, Tuple

from sexpdata import Symbol

logger = logging.getLogger("kicad_interface")

# Module-level Symbol constants
_K = {
    name: Symbol(name)
    for name in [
        "symbol",
        "at",
        "lib_id",
        "mirror",
        "lib_symbols",
        "pts",
        "xy",
        "wire",
        "junction",
        "property",
        "stroke",
        "width",
        "type",
        "uuid",
        "unit",
        "label",
        "global_label",
        "hierarchical_label",
    ]
}

EPS = 1e-4  # mm — coordinate match tolerance


def _rotate(x: float, y: float, angle_deg: float) -> Tuple[float, float]:
    """Rotate (x, y) around the origin by angle_deg degrees (CCW)."""
    if angle_deg == 0:
        return x, y
    rad = math.radians(angle_deg)
    c, s = math.cos(rad), math.sin(rad)
    return x * c - y * s, x * s + y * c


def _coords_match(ax: float, ay: float, bx: float, by: float, eps: float = EPS) -> bool:
    return abs(ax - bx) < eps and abs(ay - by) < eps


def _reference_of(item: Any) -> Optional[str]:
    """Return the ``Reference`` property value of a ``(symbol …)`` item, else None."""
    prop_k = _K["property"]
    for sub in item[1:]:
        if isinstance(sub, list) and len(sub) >= 3 and sub[0] == prop_k:
            if str(sub[1]).strip('"') == "Reference":
                return str(sub[2]).strip('"')
    return None


class WireDragger:
    """Pure-logic helpers for wire-endpoint dragging during component moves."""

    @staticmethod
    def _parse_symbol_instance(item: Any, reference: str) -> Optional[Tuple]:
        """
        Parse one ``(symbol …)`` instance if it matches ``reference``.

        Returns (symbol_item, x, y, rotation, lib_id, mirror_x, mirror_y, unit)
        or None when ``item`` is not a placed symbol carrying that reference.

        ``unit`` is the ``(unit N)`` value (defaults to 1 when absent — KiCad's
        default for single-unit symbols). A multi-unit part is placed as one
        ``(symbol …)`` per unit, all sharing the reference but each with its own
        ``(at …)`` and ``(unit N)``; the unit is what tells callers which
        physical instance owns a given pin.
        """
        sym_k = _K["symbol"]
        at_k = _K["at"]
        lib_id_k = _K["lib_id"]
        mirror_k = _K["mirror"]
        unit_k = _K["unit"]

        if not (isinstance(item, list) and item and item[0] == sym_k):
            return None

        # Check Reference property.
        # kicad-skip may write a trailing "_" on references (e.g. "R1_") when
        # cloning symbols; strip it so callers passing the canonical "R1"
        # still find the symbol. Mirrors the rstrip in PinLocator.get_pin_location.
        ref_val = _reference_of(item)
        if ref_val is None or ref_val.rstrip("_") != reference:
            return None

        old_x = old_y = rotation = 0.0
        lib_id = ""
        mirror_x = mirror_y = False
        unit = 1

        for sub in item[1:]:
            if not isinstance(sub, list) or not sub:
                continue
            tag = sub[0]
            if tag == at_k:
                if len(sub) >= 3:
                    old_x = float(sub[1])
                    old_y = float(sub[2])
                if len(sub) >= 4:
                    rotation = float(sub[3])
            elif tag == lib_id_k and len(sub) >= 2:
                lib_id = str(sub[1]).strip('"')
            elif tag == mirror_k and len(sub) >= 2:
                mv = str(sub[1])
                if mv == "x":
                    mirror_x = True
                elif mv == "y":
                    mirror_y = True
            elif tag == unit_k and len(sub) >= 2:
                try:
                    unit = int(sub[1])
                except (TypeError, ValueError):
                    unit = 1

        return item, old_x, old_y, rotation, lib_id, mirror_x, mirror_y, unit

    @staticmethod
    def find_symbol(sch_data: list, reference: str) -> Any:
        """
        Find a placed symbol by reference designator.

        Returns (symbol_item, old_x, old_y, rotation, lib_id, mirror_x, mirror_y)
        for the FIRST matching instance, or None if the reference is not found.

        mirror_x=True means the symbol has (mirror x) — flips the X local axis.
        mirror_y=True means the symbol has (mirror y) — flips the Y local axis.

        Note: a multi-unit part is placed once per unit under the same
        reference. This returns whichever unit appears first in the file; use
        find_symbol_instances to get every unit's transform (needed to locate
        pins that live on units other than the first — see PinLocator).
        """
        for item in sch_data:
            parsed = WireDragger._parse_symbol_instance(item, reference)
            if parsed is not None:
                return parsed[:7]
        return None

    @staticmethod
    def find_symbol_instances(sch_data: list, reference: str) -> List[Tuple]:
        """
        Find every placed instance of ``reference`` (one per unit).

        Returns a list of
        (symbol_item, x, y, rotation, lib_id, mirror_x, mirror_y, unit),
        in file order — one entry per placed unit. For a single-unit part this
        is a one-element list; for a multi-unit part (op-amp, gate array) it
        has one entry per unit instance present on the sheet.
        """
        out: List[Tuple] = []
        for item in sch_data:
            parsed = WireDragger._parse_symbol_instance(item, reference)
            if parsed is not None:
                out.append(parsed)
        return out

    @staticmethod
    def get_pin_defs(sch_data: list, lib_id: str) -> Dict:
        """
        Get pin definitions from lib_symbols for the given lib_id.

        Returns the same dict format as PinLocator.parse_symbol_definition:
        {pin_num: {"x": ..., "y": ..., ...}}.
        """
        from commands.pin_locator import PinLocator

        lib_sym_k = _K["lib_symbols"]
        symbol_k = _K["symbol"]

        for item in sch_data:
            if not (isinstance(item, list) and item and item[0] == lib_sym_k):
                continue
            for sym_def in item[1:]:
                if not (isinstance(sym_def, list) and sym_def and sym_def[0] == symbol_k):
                    continue
                if len(sym_def) < 2:
                    continue
                name = str(sym_def[1]).strip('"')
                if name == lib_id:
                    return PinLocator.parse_symbol_definition(sym_def)
            break  # only one lib_symbols section
        return {}

    @staticmethod
    def pin_world_xy(
        px: float,
        py: float,
        sym_x: float,
        sym_y: float,
        rotation: float,
        mirror_x: bool,
        mirror_y: bool,
    ) -> Tuple[float, float]:
        """
        Compute the world coordinate of a pin given the symbol transform.

        Library pins are stored Y-up; the schematic is Y-down. Order matches
        eeschema: Y-flip to screen → mirror → rotate (screen-CCW) → translate.

        eeschema's TRANSFORM matrix for rotation 90 is (0, 1, -1, 0) —
        i.e. screen-CCW in Y-down: (x, y) → (y, -x). Our `_rotate` helper is
        standard math (Y-up CCW), so we negate the rotation angle to convert.

        Mirror axis semantics match eeschema's symbol.h:
          (mirror x) = SYM_MIRROR_X = TRANSFORM(1, 0, 0, -1) → negates Y.
          (mirror y) = SYM_MIRROR_Y = TRANSFORM(-1, 0, 0, 1) → negates X.
        """
        lx, ly = px, -py  # Y-flip: lib Y-up → screen Y-down
        if mirror_x:
            ly = -ly  # SYM_MIRROR_X negates screen-Y
        if mirror_y:
            lx = -lx  # SYM_MIRROR_Y negates screen-X
        rx, ry = _rotate(lx, ly, -rotation)  # negate angle: math-CCW → screen-CCW
        return sym_x + rx, sym_y + ry

    @staticmethod
    def compute_pin_positions(
        sch_data: list,
        reference: str,
        new_x: float,
        new_y: float,
    ) -> Dict[str, Tuple[Tuple[float, float], Tuple[float, float]]]:
        """
        Compute world pin positions before and after a component move.

        Returns {pin_num: (old_world_xy, new_world_xy)}.
        old_world_xy uses the symbol's current position; new_world_xy uses (new_x, new_y).
        """
        found = WireDragger.find_symbol(sch_data, reference)
        if found is None:
            return {}
        _, old_x, old_y, rotation, lib_id, mirror_x, mirror_y = found

        pins = WireDragger.get_pin_defs(sch_data, lib_id)
        result: Dict[str, Tuple] = {}
        for pin_num, pin in pins.items():
            px, py = pin["x"], pin["y"]
            old_wx, old_wy = WireDragger.pin_world_xy(
                px, py, old_x, old_y, rotation, mirror_x, mirror_y
            )
            new_wx, new_wy = WireDragger.pin_world_xy(
                px, py, new_x, new_y, rotation, mirror_x, mirror_y
            )
            result[pin_num] = (
                (round(old_wx, 6), round(old_wy, 6)),
                (round(new_wx, 6), round(new_wy, 6)),
            )
        return result

    @staticmethod
    def compute_pin_positions_for_rotation(
        sch_data: list,
        reference: str,
        new_rotation: float,
        new_mirror_x: bool,
        new_mirror_y: bool,
    ) -> Dict[str, Tuple[Tuple[float, float], Tuple[float, float]]]:
        """
        Compute world pin positions before and after a rotation/mirror change.

        The symbol stays at the same (x, y); only the rotation and mirror state change.
        Returns {pin_num: (old_world_xy, new_world_xy)}.
        """
        found = WireDragger.find_symbol(sch_data, reference)
        if found is None:
            return {}
        _, sym_x, sym_y, old_rotation, lib_id, old_mirror_x, old_mirror_y = found

        pins = WireDragger.get_pin_defs(sch_data, lib_id)
        result: Dict[str, Tuple] = {}
        for pin_num, pin in pins.items():
            px, py = pin["x"], pin["y"]
            old_wx, old_wy = WireDragger.pin_world_xy(
                px, py, sym_x, sym_y, old_rotation, old_mirror_x, old_mirror_y
            )
            new_wx, new_wy = WireDragger.pin_world_xy(
                px, py, sym_x, sym_y, new_rotation, new_mirror_x, new_mirror_y
            )
            result[pin_num] = (
                (round(old_wx, 6), round(old_wy, 6)),
                (round(new_wx, 6), round(new_wy, 6)),
            )
        return result

    @staticmethod
    def update_symbol_rotation_mirror(
        sch_data: list,
        reference: str,
        new_rotation: float,
        new_mirror: Optional[str],
    ) -> bool:
        """
        Update the rotation in (at x y rot) and the (mirror x/y) token for a symbol.

        new_mirror: "x", "y", or None (removes any existing mirror token).
        Returns True if the symbol was found and updated.
        """
        found = WireDragger.find_symbol(sch_data, reference)
        if found is None:
            return False
        item = found[0]
        at_k = _K["at"]
        mirror_k = _K["mirror"]

        # Update rotation in (at x y rot)
        for sub in item[1:]:
            if isinstance(sub, list) and sub and sub[0] == at_k and len(sub) >= 4:
                sub[3] = new_rotation
                break

        # Remove existing (mirror ...) token(s)
        to_remove = [
            i for i, sub in enumerate(item) if isinstance(sub, list) and sub and sub[0] == mirror_k
        ]
        for i in reversed(to_remove):
            del item[i]

        # Insert new mirror token if requested
        if new_mirror in ("x", "y"):
            item.append([mirror_k, Symbol(new_mirror)])

        return True

    @staticmethod
    def drag_wires(
        sch_data: list,
        old_to_new: Dict[Tuple[float, float], Tuple[float, float]],
        eps: float = EPS,
    ) -> Dict:
        """
        Move wire endpoints and junctions from old positions to new positions.
        Removes zero-length wires that result from the move.
        Modifies sch_data in place.

        old_to_new: {(old_x, old_y): (new_x, new_y)}

        Returns {'endpoints_moved': N, 'wires_removed': M}.
        """
        wire_k = _K["wire"]
        pts_k = _K["pts"]
        xy_k = _K["xy"]
        junction_k = _K["junction"]
        at_k = _K["at"]

        def find_new(x: float, y: float) -> Optional[Tuple[float, float]]:
            for (ox, oy), (nx, ny) in old_to_new.items():
                if _coords_match(x, y, ox, oy, eps):
                    return nx, ny
            return None

        endpoints_moved = 0
        zero_length_indices = []

        # First pass: update wire endpoints
        for idx, item in enumerate(sch_data):
            if not (isinstance(item, list) and item and item[0] == wire_k):
                continue

            pts_sub = None
            for sub in item[1:]:
                if isinstance(sub, list) and sub and sub[0] == pts_k:
                    pts_sub = sub
                    break
            if pts_sub is None:
                continue

            xy_items = [
                p for p in pts_sub[1:] if isinstance(p, list) and len(p) >= 3 and p[0] == xy_k
            ]
            for xy_item in xy_items:
                nc = find_new(float(xy_item[1]), float(xy_item[2]))
                if nc is not None:
                    xy_item[1] = nc[0]
                    xy_item[2] = nc[1]
                    endpoints_moved += 1

            # Check if this wire is now zero-length
            if len(xy_items) >= 2:
                x1, y1 = float(xy_items[0][1]), float(xy_items[0][2])
                x2, y2 = float(xy_items[-1][1]), float(xy_items[-1][2])
                if _coords_match(x1, y1, x2, y2, eps):
                    zero_length_indices.append(idx)

        # Remove zero-length wires (backwards to preserve indices)
        for idx in reversed(zero_length_indices):
            del sch_data[idx]

        # Second pass: update junctions
        for item in sch_data:
            if not (isinstance(item, list) and item and item[0] == junction_k):
                continue
            for sub in item[1:]:
                if isinstance(sub, list) and sub and sub[0] == at_k and len(sub) >= 3:
                    nc = find_new(float(sub[1]), float(sub[2]))
                    if nc is not None:
                        sub[1] = nc[0]
                        sub[2] = nc[1]
                    break

        return {
            "endpoints_moved": endpoints_moved,
            "wires_removed": len(zero_length_indices),
        }

    @staticmethod
    def update_symbol_position(sch_data: list, reference: str, new_x: float, new_y: float) -> bool:
        """
        Update the (at x y rot) of the named symbol in sch_data.
        Returns True if the symbol was found and updated.
        """
        found = WireDragger.find_symbol(sch_data, reference)
        if found is None:
            return False
        item = found[0]
        at_k = _K["at"]
        prop_k = _K["property"]

        # Find current position and compute delta
        old_x = old_y = None
        for sub in item[1:]:
            if isinstance(sub, list) and sub and sub[0] == at_k and len(sub) >= 3:
                old_x, old_y = sub[1], sub[2]
                sub[1] = new_x
                sub[2] = new_y
                break
        if old_x is None or old_y is None:
            return False

        dx = new_x - old_x
        dy = new_y - old_y

        # Shift all property label positions by the same delta
        for sub in item[1:]:
            if isinstance(sub, list) and sub and sub[0] == prop_k:
                for psub in sub[1:]:
                    if isinstance(psub, list) and psub and psub[0] == at_k and len(psub) >= 3:
                        psub[1] += dx
                        psub[2] += dy
                        break
        return True

    @staticmethod
    def _make_wire_sexp(x1: float, y1: float, x2: float, y2: float) -> list:
        """Build a wire s-expression list in KiCAD schematic format."""
        wire_uuid = str(uuid.uuid4())
        return [
            _K["wire"],
            [_K["pts"], [_K["xy"], x1, y1], [_K["xy"], x2, y2]],
            [_K["stroke"], [_K["width"], 0], [_K["type"], Symbol("default")]],
            [_K["uuid"], wire_uuid],
        ]

    @staticmethod
    def _iter_symbol_pin_world_positions(
        sch_data: list, reference: str
    ) -> Iterator[Tuple[Tuple[float, float], Any, Dict]]:
        """Yield ``(world_xy_key, pin_num, pin_def)`` for each pin of the named
        placed symbol. Empty when the reference is not found."""
        found = WireDragger.find_symbol(sch_data, reference)
        if found is None:
            return
        _, sx, sy, rotation, lib_id, mirror_x, mirror_y = found
        pins = WireDragger.get_pin_defs(sch_data, lib_id)
        for pin_num, pin in pins.items():
            wx, wy = WireDragger.pin_world_xy(
                pin["x"], pin["y"], sx, sy, rotation, mirror_x, mirror_y
            )
            yield (round(wx, 6), round(wy, 6)), pin_num, pin

    @staticmethod
    def get_all_stationary_pin_positions(
        sch_data: list,
        moved_reference: str,
    ) -> Dict[Tuple[float, float], str]:
        """
        Return a map of {world_xy: reference} for every pin of every symbol
        in sch_data *except* moved_reference.

        This is used to detect pins of stationary components that coincide
        with pins of the moved component (touching-pin connections).
        """
        sym_k = _K["symbol"]
        result: Dict[Tuple[float, float], str] = {}

        for item in sch_data:
            if not (isinstance(item, list) and item and item[0] == sym_k):
                continue
            ref_val = _reference_of(item)
            if ref_val is None or ref_val == moved_reference:
                continue
            for key, _pin_num, _pin in WireDragger._iter_symbol_pin_world_positions(
                sch_data, ref_val
            ):
                result[key] = ref_val

        return result

    @staticmethod
    def get_stationary_pin_details(
        sch_data: list,
        moved_reference: str,
    ) -> Dict[Tuple[float, float], List[Tuple[str, str, str]]]:
        """Like :meth:`get_all_stationary_pin_positions` but keeps EVERY pin at a
        coordinate together with its owner and identity.

        Returns ``{world_xy: [(reference, pin_number, pin_name), ...]}`` for
        every pin of every symbol except ``moved_reference``.  The plain
        position map collapses to a single reference per point and drops the pin
        number/name; naming the foreign pin a moved pin was coincident with (A4)
        needs the full identity.
        """
        sym_k = _K["symbol"]
        result: Dict[Tuple[float, float], List[Tuple[str, str, str]]] = {}

        seen_refs: set = set()
        for item in sch_data:
            if not (isinstance(item, list) and item and item[0] == sym_k):
                continue
            ref_val = _reference_of(item)
            if ref_val is None or ref_val == moved_reference or ref_val in seen_refs:
                continue
            seen_refs.add(ref_val)
            for key, pin_num, pin in WireDragger._iter_symbol_pin_world_positions(
                sch_data, ref_val
            ):
                result.setdefault(key, []).append((ref_val, str(pin_num), str(pin.get("name", ""))))

        return result

    @staticmethod
    def _label_positions(sch_data: list) -> Dict[Tuple[float, float], List[str]]:
        """Return ``{world_xy: [label_name, ...]}`` for every net label.

        Covers ``label`` / ``global_label`` / ``hierarchical_label``; the name is
        the first token after the tag.  Used to name the net a detached stub was
        carrying (A4).
        """
        label_syms = {_K["label"], _K["global_label"], _K["hierarchical_label"]}
        at_k = _K["at"]
        out: Dict[Tuple[float, float], List[str]] = {}
        for item in sch_data:
            if not (isinstance(item, list) and item and item[0] in label_syms):
                continue
            name = str(item[1]).strip('"') if len(item) >= 2 else ""
            for sub in item[1:]:
                if isinstance(sub, list) and sub and sub[0] == at_k and len(sub) >= 3:
                    key = (round(float(sub[1]), 6), round(float(sub[2]), 6))
                    out.setdefault(key, []).append(name)
                    break
        return out

    @staticmethod
    def find_detached_foreign_pins(
        sch_data: list,
        moved_reference: str,
        pin_positions: Dict[str, Tuple[Tuple[float, float], Tuple[float, float]]],
        eps: float = EPS,
    ) -> Tuple[set, List[Dict[str, Any]]]:
        """Identify moved pins that will DETACH from an accidentally-coincident
        foreign pin (A4).

        A moved pin qualifies when its OLD world position coincides with a
        stationary component's pin AND the moved pin carries its own single-ended
        stub — a ``connect_to_net`` wire whose far end is free (a net label or
        nothing).  That overlap is a symbol collision, not an intentional
        touching-pin connection: the moved pin's real net travels with its stub,
        so the move must detach from the foreign pin rather than synthesize a
        bridge (see :meth:`synthesize_touching_pin_wires`) that would re-short
        them at the new location.

        Returns ``(skip_positions, warnings)``:
          * ``skip_positions`` — set of rounded OLD world positions to exclude
            from :meth:`synthesize_touching_pin_wires`.
          * ``warnings`` — one dict per detached point::

              {"movedPin": str, "coordinate": {"x", "y"},
               "foreign": [{"reference", "pin", "name"}, ...],
               "netLabels": [str, ...]}
        """
        skip: set = set()
        warnings: List[Dict[str, Any]] = []
        if not pin_positions:
            return skip, warnings

        moved_old: Dict[Tuple[float, float], str] = {}
        for pin_num, (old_xy, _new_xy) in pin_positions.items():
            moved_old.setdefault((round(old_xy[0], 6), round(old_xy[1], 6)), str(pin_num))

        foreign = WireDragger.get_stationary_pin_details(sch_data, moved_reference)
        if not foreign:
            return skip, warnings
        stationary_keys = set(foreign.keys())

        endpoint_count, wires = WireDragger._wire_endpoint_index(sch_data)
        junction_keys = WireDragger._junction_keys(sch_data)

        labels = WireDragger._label_positions(sch_data)

        reported: set = set()
        for eps_pts in wires:
            a, b = eps_pts[0], eps_pts[-1]
            a_key = (round(a[0], 6), round(a[1], 6))
            b_key = (round(b[0], 6), round(b[1], 6))
            a_moved = a_key in moved_old
            b_moved = b_key in moved_old
            # Exactly one endpoint on a moved pin => single-ended stub candidate.
            if a_moved == b_moved:
                continue
            near_key = a_key if a_moved else b_key
            far_key = b_key if a_moved else a_key
            foreign_here = foreign.get(near_key)
            if not foreign_here:
                continue  # near end not shared with a foreign pin — nothing to detach
            # Far end must be a genuinely free stub end (label / nothing).
            if far_key in stationary_keys or far_key in moved_old:
                continue
            if endpoint_count.get(far_key, 0) > 1:
                continue
            if far_key in junction_keys:
                continue
            skip.add(near_key)
            if near_key not in reported:
                reported.add(near_key)
                warnings.append(
                    {
                        "movedPin": moved_old.get(near_key),
                        "coordinate": {"x": near_key[0], "y": near_key[1]},
                        "foreign": [
                            {"reference": r, "pin": p, "name": n} for (r, p, n) in foreign_here
                        ],
                        "netLabels": labels.get(far_key, []),
                    }
                )
        return skip, warnings

    @staticmethod
    def synthesize_touching_pin_wires(
        sch_data: list,
        moved_reference: str,
        pin_positions: Dict[str, Tuple[Tuple[float, float], Tuple[float, float]]],
        eps: float = EPS,
        skip_old_positions: Optional[set] = None,
    ) -> int:
        """
        Detect touching-pin connections and synthesize wire segments to bridge gaps
        created by moving a component.

        For each pin of *moved_reference* whose old world position coincides with
        a pin of a stationary component:
          - If the pin moved (old_xy != new_xy), insert a wire from old_xy to new_xy.
          - If the pin now lands on another stationary pin's position, skip (they touch again).
          - If old_xy == new_xy, do nothing (no gap was created).

        ``skip_old_positions`` is a set of rounded OLD world positions that must
        NOT be bridged — used by the move handler to detach a pin that only
        *accidentally* overlapped a foreign pin (A4) instead of re-shorting it at
        the new location.  See :meth:`find_detached_foreign_pins`.

        Modifies sch_data in place.
        Returns the number of wire segments synthesized.
        """
        if not pin_positions:
            return 0

        stationary_pins = WireDragger.get_all_stationary_pin_positions(sch_data, moved_reference)
        if not stationary_pins:
            return 0

        skip = skip_old_positions or set()
        synthesized = 0

        for pin_num, (old_xy, new_xy) in pin_positions.items():
            # A pin whose old position is flagged for clean detachment must not
            # be bridged — bridging would re-short it to the foreign pin it only
            # accidentally overlapped.
            if (round(old_xy[0], 6), round(old_xy[1], 6)) in skip:
                continue
            # Check if a stationary pin touches this pin's old position
            touching = any(
                _coords_match(old_xy[0], old_xy[1], sx, sy, eps) for (sx, sy) in stationary_pins
            )
            if not touching:
                continue

            # The pin has moved — check if it actually separated
            if _coords_match(old_xy[0], old_xy[1], new_xy[0], new_xy[1], eps):
                # Pin didn't actually move; no gap
                continue

            # Check if the pin's new position happens to touch another stationary pin
            # (component moved into a different touching position — no wire needed)
            rejoining = any(
                _coords_match(new_xy[0], new_xy[1], sx, sy, eps) for (sx, sy) in stationary_pins
            )
            if rejoining:
                logger.debug(
                    f"Pin {moved_reference}/{pin_num} moved from {old_xy} to {new_xy} "
                    f"and rejoins another stationary pin; no wire synthesized"
                )
                continue

            logger.info(
                f"Synthesizing wire for touching-pin connection: "
                f"{moved_reference}/{pin_num} moved from {old_xy} to {new_xy}"
            )
            wire = WireDragger._make_wire_sexp(old_xy[0], old_xy[1], new_xy[0], new_xy[1])
            # Insert before the last item (sheet_instances) to keep file tidy,
            # but appending is also valid — just append.
            sch_data.append(wire)
            synthesized += 1

        return synthesized

    @staticmethod
    def _wire_endpoints(item: Any) -> List[Tuple[float, float]]:
        """Return the [(x, y), …] endpoints of a wire s-expr item, else []."""
        if not (isinstance(item, list) and item and item[0] == _K["wire"]):
            return []
        pts_sub = None
        for sub in item[1:]:
            if isinstance(sub, list) and sub and sub[0] == _K["pts"]:
                pts_sub = sub
                break
        if pts_sub is None:
            return []
        xy_items = [
            p for p in pts_sub[1:] if isinstance(p, list) and len(p) >= 3 and p[0] == _K["xy"]
        ]
        return [(float(p[1]), float(p[2])) for p in xy_items]

    @staticmethod
    def _wire_endpoint_index(
        sch_data: list,
    ) -> Tuple[Counter, List[List[Tuple[float, float]]]]:
        """Index every 2+-point wire once.

        Returns ``(endpoint_count, wires)`` where ``endpoint_count`` counts how
        many wire ends land on each rounded point (a point shared by >1 wire is
        anchored) and ``wires`` is each wire's endpoint list.
        """
        endpoint_count: Counter = Counter()
        wires: List[List[Tuple[float, float]]] = []
        for item in sch_data:
            eps_pts = WireDragger._wire_endpoints(item)
            if len(eps_pts) >= 2:
                wires.append(eps_pts)
                for ep in (eps_pts[0], eps_pts[-1]):
                    endpoint_count[(round(ep[0], 6), round(ep[1], 6))] += 1
        return endpoint_count, wires

    @staticmethod
    def _junction_keys(sch_data: list) -> set:
        """Return the set of rounded ``(junction (at x y))`` positions."""
        junction_keys: set = set()
        for item in sch_data:
            if isinstance(item, list) and item and item[0] == _K["junction"]:
                for sub in item[1:]:
                    if isinstance(sub, list) and sub and sub[0] == _K["at"] and len(sub) >= 3:
                        junction_keys.add((round(float(sub[1]), 6), round(float(sub[2]), 6)))
                        break
        return junction_keys

    @staticmethod
    def collect_stub_far_endpoints(
        sch_data: list,
        moved_reference: str,
        pin_positions: Dict[str, Tuple[Tuple[float, float], Tuple[float, float]]],
        eps: float = EPS,
    ) -> Dict[Tuple[float, float], Tuple[float, float]]:
        """Find 'stub' wires and return {far_old_xy: far_new_xy} for a rigid move.

        A *stub* is a wire with exactly ONE endpoint on a moved pin (its OLD
        world position) and the OTHER endpoint *free* — nothing but an optional
        net label sits there.  That is the shape ``connect_to_net`` produces (a
        2.54 mm wire from the pin plus a label at the far end).  When the
        component moves, such a stub must translate *rigidly*: both endpoints
        (and any label at the far end) shift by the same delta as the pin, so the
        drag never leaves a stretched diagonal wire + orphaned label behind.

        'Free' means the far endpoint is NOT another component's pin, NOT shared
        with any other wire endpoint, and NOT a junction — i.e. it carries no
        real connectivity of its own.  A wire whose far endpoint is anchored to
        something real is deliberately omitted here so it keeps stretch behavior
        (the caller still drags its moved endpoint via ``drag_wires``).

        The returned map is meant to be merged into the ``old_to_new`` passed to
        :meth:`drag_wires`; a label at ``far_old_xy`` is moved by
        :meth:`move_labels_at_points` with that same map.
        """
        if not pin_positions:
            return {}

        # OLD moved-pin position (rounded) -> NEW position.
        moved_pins: Dict[Tuple[float, float], Tuple[float, float]] = {}
        for _pin, (old_xy, new_xy) in pin_positions.items():
            moved_pins[(round(old_xy[0], 6), round(old_xy[1], 6))] = (new_xy[0], new_xy[1])

        stationary_keys = set(
            WireDragger.get_all_stationary_pin_positions(sch_data, moved_reference).keys()
        )

        # Count wire endpoints (a far end shared with another wire is anchored)
        # and gather each wire's endpoints once. Junction positions mean real
        # connectivity — not a free end.
        endpoint_count, wires = WireDragger._wire_endpoint_index(sch_data)
        junction_keys = WireDragger._junction_keys(sch_data)

        far_map: Dict[Tuple[float, float], Tuple[float, float]] = {}
        for eps_pts in wires:
            a, b = eps_pts[0], eps_pts[-1]
            a_key = (round(a[0], 6), round(a[1], 6))
            b_key = (round(b[0], 6), round(b[1], 6))
            a_moved = a_key in moved_pins
            b_moved = b_key in moved_pins
            # Exactly one endpoint on a moved pin => single-ended stub candidate.
            if a_moved == b_moved:
                continue
            near_key = a_key if a_moved else b_key
            far_key = b_key if a_moved else a_key
            far_xy = b if a_moved else a
            # Far end must be genuinely free (else keep stretch behavior).
            if far_key in stationary_keys:
                continue
            if far_key in moved_pins:
                continue
            if endpoint_count.get(far_key, 0) > 1:
                continue
            if far_key in junction_keys:
                continue
            new_near = moved_pins[near_key]
            dx = new_near[0] - near_key[0]
            dy = new_near[1] - near_key[1]
            far_map[far_key] = (round(far_xy[0] + dx, 6), round(far_xy[1] + dy, 6))
        return far_map

    @staticmethod
    def move_labels_at_points(
        sch_data: list,
        old_to_new: Dict[Tuple[float, float], Tuple[float, float]],
        eps: float = EPS,
    ) -> int:
        """Move net labels sitting on any moved point, in place. Returns count.

        A label (``label`` / ``global_label`` / ``hierarchical_label``) whose
        anchor coincides (within ``eps``) with a key of ``old_to_new`` is
        relocated to the mapped new position, rotation preserved.  Combined with
        the stub-far-endpoint map, this makes a ``connect_to_net`` label travel
        rigidly with its component; used with the pin map alone it also moves a
        label placed directly on a moved pin.
        """
        if not old_to_new:
            return 0
        label_syms = {_K["label"], _K["global_label"], _K["hierarchical_label"]}
        at_k = _K["at"]
        mapping = list(old_to_new.items())
        moved = 0
        for item in sch_data:
            if not (isinstance(item, list) and item and item[0] in label_syms):
                continue
            for sub in item[1:]:
                if isinstance(sub, list) and sub and sub[0] == at_k and len(sub) >= 3:
                    lx, ly = float(sub[1]), float(sub[2])
                    for (ox, oy), (nx, ny) in mapping:
                        if _coords_match(lx, ly, ox, oy, eps):
                            sub[1] = nx
                            sub[2] = ny
                            moved += 1
                            break
                    break
        return moved
