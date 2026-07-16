"""Add / move / rotate / delete / annotate component handlers (+ grid-snap helpers).

Split out of the former handlers/schematic_component.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Tuple

import sexpdata
from commands.schematic import SchematicManager
from commands.schematic_locks import atomic_write_text, serialize_on_param
from commands.wire_manager import WireManager

from ._shared import find_placed_symbol_blocks

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.schematic_component")


# KiCad's default schematic grid is 50 mil = 1.27 mm; symbol pin offsets
# are multiples of that, so an off-grid symbol places its pins off-grid
# and ERC fires "wire/pin not aligned" warnings on every pin.  Tools
# that take mm coordinates snap to this grid BY DEFAULT — agents
# typically use round-mm coordinates like (130, 80) which would
# otherwise produce ERC warnings on every pin (the user reported 11
# off-grid warnings from a single off-grid placement).  Pass
# ``snapToGrid: false`` to opt out when sub-grid placement is intentional.
_SCHEMATIC_GRID_MM = 1.27


# placeAllUnits lays each unit of a multi-unit symbol out in a page-aware
# grid (S2): units stack downward until the next one would run off the sheet
# bottom, then wrap into a new column to the right so no unit lands off-page.
# The gap separates adjacent unit bodies; the fallback height/width is used
# when a unit's pin span can't be measured.
_UNIT_STACK_GAP_MM = 7.62
_DEFAULT_UNIT_HEIGHT_MM = 25.4
_DEFAULT_UNIT_WIDTH_MM = 25.4


# KiCad standard paper sizes in mm, landscape (width, height). Used to keep
# placements page-aware (S2/S9): warn when a symbol/unit lands outside the
# sheet, and reject coordinates so absurd they can only be a units mistake.
_PAPER_SIZES_MM: Dict[str, Tuple[float, float]] = {
    "A5": (210.0, 148.0),
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
    "A": (279.4, 215.9),
    "B": (431.8, 279.4),
    "C": (558.8, 431.8),
    "D": (863.6, 558.8),
    "E": (1117.6, 863.6),
    "USLetter": (279.4, 215.9),
    "USLegal": (355.6, 215.9),
    "USLedger": (431.8, 279.4),
}
_DEFAULT_PAPER_MM = (297.0, 210.0)  # A4
# ``(paper "A4")`` | ``(paper "A4" portrait)`` | ``(paper "User" 300.0 200.0)``
_PAPER_RE = re.compile(r'\(paper\s+"([^"]+)"((?:\s+[-0-9.]+)*)\s*(portrait)?\s*\)')
# A coordinate more than this many page-widths/heights away can only be a
# units mistake (e.g. x=99999 mm ≈ 100 m); reject rather than drag wires there.
_OFF_PAGE_ABSURD_FACTOR = 10.0


def _snap_to_schematic_grid(value: float, grid_mm: float = _SCHEMATIC_GRID_MM) -> float:
    """Snap a millimeter coordinate to the nearest schematic-grid multiple."""
    if grid_mm <= 0:
        return value
    return round(value / grid_mm) * grid_mm


def _read_page_size(schematic_path: Any) -> Dict[str, Any]:
    """Return the sheet's paper size as ``{"name", "width", "height"}`` (mm).

    Parses the ``(paper ...)`` token from the .kicad_sch. Falls back to A4 —
    KiCad's own default and what create_schematic writes — when the token is
    absent or unrecognized.
    """
    name = "A4"
    width, height = _DEFAULT_PAPER_MM
    try:
        text = Path(schematic_path).read_text(encoding="utf-8")
    except OSError:
        return {"name": name, "width": width, "height": height}

    m = _PAPER_RE.search(text)
    if m:
        name = m.group(1)
        nums = [float(v) for v in m.group(2).split()] if m.group(2).strip() else []
        portrait = bool(m.group(3))
        if len(nums) >= 2:
            # Custom "User" size carries explicit width/height.
            width, height = nums[0], nums[1]
        else:
            width, height = _PAPER_SIZES_MM.get(name, _DEFAULT_PAPER_MM)
            if portrait:
                width, height = height, width
    return {"name": name, "width": width, "height": height}


def _classify_position(x: float, y: float, page: Dict[str, Any]) -> str:
    """Classify a point against the sheet: ``'absurd'`` | ``'off_page'`` | ``'ok'``.

    ``off_page`` still gets placed (KiCad's canvas extends past the sheet
    border); ``absurd`` (>10× a page dimension) is rejected as a units error.
    """
    w = float(page["width"])
    h = float(page["height"])
    if abs(x) > _OFF_PAGE_ABSURD_FACTOR * w or abs(y) > _OFF_PAGE_ABSURD_FACTOR * h:
        return "absurd"
    if not (0.0 <= x <= w and 0.0 <= y <= h):
        return "off_page"
    return "ok"


def _unit_extents(pins: Dict[str, Any]) -> Dict[int, Tuple[float, float, float, float]]:
    """Per numbered unit: ``(min_x, max_x, min_y, max_y)`` of its pins (library mm).

    ``pins`` is PinLocator.get_symbol_pins output. Unit 0 (common/graphic) pins
    are ignored. Library coordinates are Y-up; the caller applies the Y-flip
    when converting to placed (screen) coordinates.
    """
    boxes: Dict[int, List[float]] = {}
    for pdata in pins.values():
        u = pdata.get("unit")
        if u in (None, 0):
            continue
        u = int(u)
        px = float(pdata.get("x", 0.0))
        py = float(pdata.get("y", 0.0))
        if u not in boxes:
            boxes[u] = [px, px, py, py]
        else:
            b = boxes[u]
            b[0] = min(b[0], px)
            b[1] = max(b[1], px)
            b[2] = min(b[2], py)
            b[3] = max(b[3], py)
    return {u: (b[0], b[1], b[2], b[3]) for u, b in boxes.items()}


def _unit_offpage(
    pos: Dict[str, float], box: Tuple[float, float, float, float], page: Dict[str, Any]
) -> bool:
    """True when a unit placed at ``pos`` has pins outside the sheet.

    ``box`` is the library-coord extent ``(min_x, max_x, min_y, max_y)``.
    Placed (screen) coords: x = ox + lib_x, y = oy − lib_y (Y-flip).
    """
    min_x, max_x, min_y, max_y = box
    left = pos["x"] + min_x
    right = pos["x"] + max_x
    top = pos["y"] - max_y
    bottom = pos["y"] - min_y
    return left < 0.0 or right > float(page["width"]) or top < 0.0 or bottom > float(page["height"])


def _detect_dangling(
    content: str, pin_positions_mm: List[Tuple[float, float]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Find wire stubs and net labels orphaned by a deleted symbol (S6).

    Given the post-deletion .kicad_sch ``content`` and the deleted symbol's
    pin world positions (mm), returns ``(dangling_wires, dangling_labels)``:

    * a wire is dangling when either endpoint coincided with a deleted pin;
    * a label is dangling when it sits on a deleted pin OR on an endpoint of a
      dangling wire (the stub's far end, where connect_to_net drops the label).

    Reuses the wire_connectivity parsers rather than re-implementing geometry;
    coordinates are reported back in mm.
    """
    dangling_wires: List[Dict[str, Any]] = []
    dangling_labels: List[Dict[str, Any]] = []
    if not pin_positions_mm:
        return dangling_wires, dangling_labels

    try:
        from commands.wire_connectivity import (
            _IU_PER_MM,
            _parse_labels_sexp,
            _parse_wires_sexp,
            _to_iu,
        )

        sexp = sexpdata.loads(content)
        wires = _parse_wires_sexp(sexp)
        point_to_label, _ = _parse_labels_sexp(sexp)
    except Exception:  # best-effort — never let detection break the delete
        logger.debug("dangling detection: could not parse schematic", exc_info=True)
        return dangling_wires, dangling_labels

    tol_iu = round(0.05 * _IU_PER_MM)  # 0.05 mm — pins/stubs share exact grid pts
    pin_iu = [_to_iu(px, py) for px, py in pin_positions_mm]

    def _near(pt: Tuple[int, int], targets: List[Tuple[int, int]]) -> bool:
        return any(abs(pt[0] - t[0]) <= tol_iu and abs(pt[1] - t[1]) <= tol_iu for t in targets)

    def _mm(pt: Tuple[int, int]) -> Dict[str, float]:
        return {"x": round(pt[0] / _IU_PER_MM, 4), "y": round(pt[1] / _IU_PER_MM, 4)}

    dangling_endpoints: List[Tuple[int, int]] = []
    for w in wires:
        start_iu, end_iu = w[0], w[-1]
        if _near(start_iu, pin_iu) or _near(end_iu, pin_iu):
            dangling_wires.append({"start": _mm(start_iu), "end": _mm(end_iu)})
            dangling_endpoints.append(start_iu)
            dangling_endpoints.append(end_iu)

    label_targets = pin_iu + dangling_endpoints
    for pt_iu, name in point_to_label.items():
        if _near(pt_iu, label_targets):
            dangling_labels.append({"name": name, "position": _mm(pt_iu)})

    return dangling_wires, dangling_labels


def _apply_grid_snap(x: float, y: float, params: Dict[str, Any]) -> Tuple[float, float, bool]:
    """Return (x, y, snapped) honoring the caller's snapToGrid choice.

    Snap is **default-on** for the 1.27 mm KiCad schematic grid — most
    callers pass round mm and don't realize KiCad's grid means pins
    land off-connection-grid otherwise.  Pass ``snapToGrid: false``
    explicitly to opt out (e.g. when reproducing a pre-existing
    sub-grid coordinate).  ``snapped`` reports whether the coordinates
    actually moved, so an on-grid input + default-on snap returns
    ``False`` and the response omits the ``snap`` field.
    """
    snap_requested = params.get("snapToGrid")
    # Default-on: only ``False`` opts out.  ``None`` (omitted) or any
    # truthy value snaps.
    if snap_requested is False:
        return float(x), float(y), False
    grid_mm = float(params.get("snapGridMm") or _SCHEMATIC_GRID_MM)
    new_x = _snap_to_schematic_grid(float(x), grid_mm)
    new_y = _snap_to_schematic_grid(float(y), grid_mm)
    return new_x, new_y, (new_x != float(x) or new_y != float(y))


@serialize_on_param("schematicPath")
def handle_annotate_schematic(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Annotate unannotated components in schematic (R? -> R1, R2, ...)"""
    logger.info("Annotating schematic")
    try:
        import re

        schematic_path = params.get("schematicPath")
        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}

        schematic = SchematicManager.load_schematic(schematic_path)
        if not schematic:
            return {"success": False, "message": "Failed to load schematic"}

        # Collect existing references by prefix
        existing_refs = {}  # prefix -> set of numbers
        unannotated = []  # (symbol, prefix)

        for symbol in schematic.symbol:
            if not hasattr(symbol.property, "Reference"):
                continue
            ref = symbol.property.Reference.value
            if ref.startswith("_TEMPLATE"):
                continue

            # Split reference into prefix and number
            match = re.match(r"^([A-Za-z_]+)(\d+)$", ref)
            if match:
                prefix = match.group(1)
                num = int(match.group(2))
                if prefix not in existing_refs:
                    existing_refs[prefix] = set()
                existing_refs[prefix].add(num)
            elif ref.endswith("?"):
                prefix = ref[:-1]
                unannotated.append((symbol, prefix))

        if not unannotated:
            # No '?' placeholders means add_schematic_component was called
            # with concrete references at creation — annotate_schematic
            # has nothing to assign.  Flag this as a no-op so callers can
            # detect it programmatically and skip the call in future
            # runs of the same flow.
            return {
                "success": True,
                "noop": True,
                "annotated": [],
                "message": (
                    "No components needed annotation — every symbol already "
                    "has a concrete reference (no '?' placeholders). This "
                    "tool only matters when add_schematic_component was "
                    "called with placeholder refs like 'R?'."
                ),
            }

        annotated = []
        for symbol, prefix in unannotated:
            if prefix not in existing_refs:
                existing_refs[prefix] = set()

            # Find next available number
            next_num = 1
            while next_num in existing_refs[prefix]:
                next_num += 1

            old_ref = symbol.property.Reference.value
            new_ref = f"{prefix}{next_num}"
            symbol.setAllReferences(new_ref)
            existing_refs[prefix].add(next_num)

            uuid_val = str(symbol.uuid.value) if hasattr(symbol, "uuid") else ""
            annotated.append(
                {
                    "uuid": uuid_val,
                    "oldReference": old_ref,
                    "newReference": new_ref,
                }
            )

        SchematicManager.save_schematic(schematic, schematic_path)
        return {"success": True, "annotated": annotated}

    except Exception as e:
        logger.error(f"Error annotating schematic: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


@serialize_on_param("schematicPath")
def handle_rotate_schematic_component(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Rotate and/or mirror a schematic component, dragging connected wires."""
    logger.info("Rotating schematic component")
    try:
        import sexpdata as _sexpdata
        from commands.wire_dragger import WireDragger

        schematic_path = params.get("schematicPath")
        reference = params.get("reference")
        angle = params.get("angle", 0)
        mirror = params.get("mirror")  # "x", "y", or None

        if not schematic_path or not reference:
            return {
                "success": False,
                "message": "schematicPath and reference are required",
            }

        # KiCad schematic symbols only support orthogonal rotations (0/90/180/270).
        # Reject anything that isn't a multiple of 90 (S8) — a 45° rotation would
        # persist an invalid schematic — and normalize negatives / >=360 (e.g.
        # -90 -> 270) so the stored and reported angle is canonical.
        try:
            requested_angle = float(angle)
        except (TypeError, ValueError):
            return {
                "success": False,
                "message": (
                    f"angle must be a number; got {angle!r}. Valid values are 0, 90, 180, or 270."
                ),
                "errorCode": "INVALID_ROTATION",
            }
        if requested_angle % 90 != 0:
            return {
                "success": False,
                "message": (
                    f"Invalid rotation {requested_angle}°: KiCad schematic symbols support "
                    f"only 0, 90, 180, or 270 degrees (multiples of 90)."
                ),
                "errorCode": "INVALID_ROTATION",
            }
        normalized_angle = requested_angle % 360.0
        # Report a clean integer when the value is whole (it always is for
        # multiples of 90) so the response reads 270, not 270.0.
        normalized_angle = (
            int(normalized_angle) if normalized_angle.is_integer() else normalized_angle
        )

        with open(schematic_path, "r", encoding="utf-8") as f:
            sch_data = _sexpdata.loads(f.read())

        found = WireDragger.find_symbol(sch_data, reference)
        if found is None:
            return {"success": False, "message": f"Component {reference} not found"}

        # Determine new mirror state: explicit param overrides; None preserves existing
        _, _, _, _, _, old_mirror_x, old_mirror_y = found
        if mirror is None:
            new_mirror_x = old_mirror_x
            new_mirror_y = old_mirror_y
            effective_mirror = "x" if old_mirror_x else ("y" if old_mirror_y else None)
        else:
            new_mirror_x = mirror == "x"
            new_mirror_y = mirror == "y"
            effective_mirror = mirror

        # Compute pin world positions before and after the transform
        pin_positions = WireDragger.compute_pin_positions_for_rotation(
            sch_data, reference, float(normalized_angle), new_mirror_x, new_mirror_y
        )

        # Build old→new map (skip pins that don't move)
        old_to_new = {}
        for _pin, (old_xy, new_xy) in pin_positions.items():
            if old_xy == new_xy:
                continue
            if old_xy in old_to_new:
                logger.warning(
                    f"rotate: pin {_pin!r} of {reference!r} shares old position "
                    f"{old_xy} with another pin; skipping duplicate"
                )
                continue
            old_to_new[old_xy] = new_xy

        # Drag connected wires to follow pins
        drag_summary = WireDragger.drag_wires(sch_data, old_to_new)

        # Update the symbol's rotation and mirror token in sexpdata
        WireDragger.update_symbol_rotation_mirror(
            sch_data, reference, float(normalized_angle), effective_mirror
        )

        WireManager.sync_junctions(sch_data)

        atomic_write_text(schematic_path, _sexpdata.dumps(sch_data))

        result: Dict[str, Any] = {
            "success": True,
            "reference": reference,
            "angle": normalized_angle,
            "mirror": effective_mirror,
            "wiresMoved": drag_summary.get("endpoints_moved", 0),
            "wiresRemoved": drag_summary.get("wires_removed", 0),
        }
        # Surface the normalization so the caller knows -90 landed as 270, etc.
        if requested_angle != normalized_angle:
            result["requestedAngle"] = requested_angle
        return result

    except Exception as e:
        logger.error(f"Error rotating schematic component: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


@serialize_on_param("schematicPath")
def handle_move_schematic_component(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Move a schematic component to a new position, dragging connected wires."""
    logger.info("Moving schematic component")
    try:
        from commands.wire_dragger import WireDragger

        schematic_path = params.get("schematicPath")
        reference = params.get("reference")
        position = params.get("position", {})
        new_x = position.get("x")
        new_y = position.get("y")
        preserve_wires = params.get("preserveWires", True)

        if not schematic_path or not reference:
            return {
                "success": False,
                "message": "schematicPath and reference are required",
            }
        if new_x is None or new_y is None:
            return {
                "success": False,
                "message": "position with x and y is required",
            }

        # Opt-in grid snap (same contract as handle_add_schematic_component).
        # Read from position OR top-level so callers can put it wherever
        # feels natural.
        snap_params = {
            "snapToGrid": (position.get("snapToGrid") or params.get("snapToGrid")),
            "snapGridMm": (position.get("snapGridMm") or params.get("snapGridMm")),
        }
        requested_new_x, requested_new_y = new_x, new_y
        new_x, new_y, snapped = _apply_grid_snap(new_x, new_y, snap_params)

        # Page-awareness (S9): reject a move so far off the sheet it can only be
        # a units mistake (x=99999 mm would drag connected wires ~100 m off the
        # canvas); a merely off-page target still moves but is flagged.
        page = _read_page_size(schematic_path)
        target_class = _classify_position(float(new_x), float(new_y), page)
        if target_class == "absurd":
            return {
                "success": False,
                "message": (
                    f"Target position ({requested_new_x}, {requested_new_y}) mm is far outside "
                    f"the {page['name']} sheet ({page['width']}×{page['height']} mm) — more than "
                    f"{int(_OFF_PAGE_ABSURD_FACTOR)}× a page dimension away. This is almost "
                    f"certainly a units error; use millimeters within (or near) the sheet."
                ),
                "errorCode": "POSITION_OFF_SHEET",
                "pageSize": page,
            }

        with open(schematic_path, "r", encoding="utf-8") as f:
            sch_data = sexpdata.loads(f.read())

        # Find symbol and record old position
        found = WireDragger.find_symbol(sch_data, reference)
        if found is None:
            return {"success": False, "message": f"Component {reference} not found"}
        _, old_x, old_y = found[0], found[1], found[2]
        old_position = {"x": old_x, "y": old_y}

        drag_summary = {}
        if preserve_wires:
            # Compute pin world positions before and after the move
            pin_positions = WireDragger.compute_pin_positions(
                sch_data, reference, float(new_x), float(new_y)
            )
            # Build old→new coordinate map (deduplicate coincident pins)
            old_to_new = {}
            for _pin, (old_xy, new_xy) in pin_positions.items():
                if old_xy in old_to_new:
                    logger.warning(
                        f"move_schematic_component: pin {_pin!r} of {reference!r} "
                        f"shares old position {old_xy} with another pin; "
                        f"keeping first entry, skipping duplicate"
                    )
                    continue
                old_to_new[old_xy] = new_xy

            # A connect_to_net "stub" (short wire from the pin + a net label at
            # its far end) must move RIGIDLY with the component: translate the
            # far endpoint by the same delta as the pin, not just the pin side —
            # otherwise the far end stays put and the wire stretches into a long
            # diagonal that keeps the moved pin electrically tied to the OLD
            # spot. Only genuinely-free far ends are folded into the drag map;
            # a far end anchored to real connectivity keeps stretch behavior.
            stub_far = WireDragger.collect_stub_far_endpoints(sch_data, reference, pin_positions)
            for far_old, far_new in stub_far.items():
                old_to_new.setdefault(far_old, far_new)

            # A4: a moved pin whose OLD position coincided with a FOREIGN
            # component's pin — while carrying its own connect_to_net stub — was
            # only *accidentally* overlapping it (a symbol collision), not
            # intentionally connected. Detach cleanly: don't synthesize a bridge
            # that would keep the foreign pin shorted to the moved pin's new
            # location, and warn naming the pin(s) that dropped off the net.
            # Must run BEFORE drag_wires, while wires still sit at old coords.
            detach_skip, detach_warnings = WireDragger.find_detached_foreign_pins(
                sch_data, reference, pin_positions
            )

            drag_summary = WireDragger.drag_wires(sch_data, old_to_new)

            # Move any net label sitting on a moved point — a pin the component
            # dragged, or the far end of a rigidly-moved stub — so no label is
            # left orphaned at the old coordinate (which would silently keep the
            # net tied there).
            labels_moved = WireDragger.move_labels_at_points(sch_data, old_to_new)
            drag_summary["labels_moved"] = labels_moved

            # Synthesize wires for touching-pin connections after dragging,
            # so drag_wires doesn't accidentally move and collapse the new wire.
            # Pins flagged for clean detachment (A4) are excluded so no bridge
            # re-shorts them to the foreign pin they only overlapped.
            wires_synthesized = WireDragger.synthesize_touching_pin_wires(
                sch_data, reference, pin_positions, skip_old_positions=detach_skip
            )
            drag_summary["wires_synthesized"] = wires_synthesized
            drag_summary["foreign_pin_detachments"] = detach_warnings

        # Update symbol position
        WireDragger.update_symbol_position(sch_data, reference, float(new_x), float(new_y))

        WireManager.sync_junctions(sch_data)

        atomic_write_text(schematic_path, sexpdata.dumps(sch_data))

        response: Dict[str, Any] = {
            "success": True,
            "oldPosition": old_position,
            "newPosition": {"x": new_x, "y": new_y},
            "wiresMoved": drag_summary.get("endpoints_moved", 0),
            "wiresRemoved": drag_summary.get("wires_removed", 0),
            "wiresSynthesized": drag_summary.get("wires_synthesized", 0),
            "labelsMoved": drag_summary.get("labels_moved", 0),
            "pageSize": page,
        }
        if snapped:
            response["snap"] = {
                "applied": True,
                "gridMm": snap_params["snapGridMm"] or _SCHEMATIC_GRID_MM,
                "requested": {"x": requested_new_x, "y": requested_new_y},
            }
        # A4: report any foreign pin that a move detached from the moved
        # component. The pin coincided with the moved component's pin only by
        # symbol overlap; moving cleanly separates them and drops the foreign
        # pin off the shared net. Naming it lets the caller re-add the
        # connection if it was actually intended.
        detachments = drag_summary.get("foreign_pin_detachments") or []
        if detachments:
            response["foreignPinDetachments"] = detachments
            parts: List[str] = []
            for d in detachments:
                fpins = ", ".join(
                    f"{f['reference']}/{f['pin']}"
                    + (f" ({f['name']})" if f.get("name") and f["name"] not in ("~", "") else "")
                    for f in d.get("foreign", [])
                )
                nets = ", ".join(n for n in (d.get("netLabels") or []) if n)
                seg = f"pin {d.get('movedPin')} was coincident with {fpins}"
                if nets:
                    seg += f" on net(s) {nets}"
                parts.append(seg)
            response["detachWarning"] = (
                f"{reference}: "
                + "; ".join(parts)
                + ". Moving detached them — the foreign pin(s) dropped off the "
                "shared net. If a connection was intended, add it explicitly."
            )
        # Off-page reporting (S9): the move succeeded (KiCad's canvas extends
        # past the sheet border) but flag it so the caller isn't surprised that
        # the symbol and its dragged wires now sit outside the printable area.
        if target_class == "off_page":
            response["offPageWarning"] = (
                f"{reference} moved to ({new_x}, {new_y}) mm, which is outside the "
                f"{page['name']} sheet ({page['width']}×{page['height']} mm). It (and any "
                f"wires dragged with it) now sit off-page; still saved, but move it onto the "
                f"sheet or enlarge the paper size."
            )
        return response

    except Exception as e:
        logger.error(f"Error moving schematic component: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


@serialize_on_param("schematicPath")
def handle_delete_schematic_component(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Remove a placed symbol from a schematic using text-based manipulation (no skip writes)"""
    logger.info("Deleting schematic component")
    try:
        from pathlib import Path

        schematic_path = params.get("schematicPath")
        reference = params.get("reference")
        remove_dangling = bool(params.get("removeDanglingWires"))

        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}
        if not reference:
            return {"success": False, "message": "reference is required"}

        sch_file = Path(schematic_path)
        if not sch_file.exists():
            return {
                "success": False,
                "message": f"Schematic not found: {schematic_path}",
            }

        # Capture the symbol's pin world positions BEFORE deletion so we can
        # find the wire stubs / net labels that were attached to it (S6). The
        # symbol must still be in the file for the pins to resolve.
        pin_positions_mm: List[Tuple[float, float]] = []
        try:
            from commands.pin_locator import PinLocator

            for xy in (PinLocator().get_all_symbol_pins(sch_file, reference) or {}).values():
                if xy and len(xy) >= 2:
                    pin_positions_mm.append((float(xy[0]), float(xy[1])))
        except Exception:  # best-effort: dangling detection is additive
            pin_positions_mm = []

        with open(sch_file, "r", encoding="utf-8") as f:
            content = f.read()

        # Find ALL placed symbol blocks matching the reference (handles
        # duplicates). Content-string search handles multi-line KiCAD format
        # correctly: KiCAD writes (symbol\n\t\t(lib_id "...") across two lines,
        # which a line-by-line regex would never match.
        blocks_to_delete = find_placed_symbol_blocks(iface, content, reference)

        if not blocks_to_delete:
            return {
                "success": False,
                "message": f"Component '{reference}' not found in schematic (note: this tool removes schematic symbols, use delete_component for PCB footprints)",
            }

        # Delete from back to front to preserve character offsets
        for b_start, b_end in sorted(blocks_to_delete, reverse=True):
            # Include any leading newline/whitespace before the block
            trim_start = b_start
            while trim_start > 0 and content[trim_start - 1] in (" ", "\t"):
                trim_start -= 1
            if trim_start > 0 and content[trim_start - 1] == "\n":
                trim_start -= 1
            content = content[:trim_start] + content[b_end + 1 :]

        atomic_write_text(sch_file, content)

        deleted_count = len(blocks_to_delete)
        logger.info(f"Deleted {deleted_count} instance(s) of {reference} from {sch_file.name}")

        # Detect (and optionally remove) the wire stubs + net labels that were
        # attached to the now-deleted symbol's pins (S6). A GUI delete leaves
        # these behind as orphans; we always REPORT them, and remove them only
        # when removeDanglingWires=true (default false keeps KiCad-GUI parity).
        dangling_wires, dangling_labels = _detect_dangling(content, pin_positions_mm)

        removed_wires = 0
        removed_labels = 0
        if remove_dangling and (dangling_wires or dangling_labels):
            for w in dangling_wires:
                removed_wires += WireManager.delete_wires(
                    sch_file,
                    [w["start"]["x"], w["start"]["y"]],
                    [w["end"]["x"], w["end"]["y"]],
                )
            for lab in dangling_labels:
                pos = lab["position"]
                if WireManager.delete_label(sch_file, lab["name"], [pos["x"], pos["y"]]):
                    removed_labels += 1

        dangling: Dict[str, Any] = {
            "wires": dangling_wires,
            "labels": dangling_labels,
            "wireCount": len(dangling_wires),
            "labelCount": len(dangling_labels),
            "removed": remove_dangling,
        }
        response: Dict[str, Any] = {
            "success": True,
            "reference": reference,
            "deleted_count": deleted_count,
            "schematic": str(sch_file),
            "dangling": dangling,
        }
        if remove_dangling:
            dangling["wiresRemoved"] = removed_wires
            dangling["labelsRemoved"] = removed_labels
            if dangling_wires or dangling_labels:
                response["message"] = (
                    f"Removed {reference} and cleaned up {removed_wires} attached wire "
                    f"stub(s) and {removed_labels} net label(s)."
                )
            else:
                response["message"] = (
                    f"Removed {reference} (no attached wire stubs or labels found)."
                )
        else:
            if dangling_wires or dangling_labels:
                response["message"] = (
                    f"Removed {reference}, but left {len(dangling_wires)} attached wire "
                    f"stub(s) and {len(dangling_labels)} net label(s) behind as orphans "
                    f"(matches a KiCad-GUI delete). Pass removeDanglingWires=true to also "
                    f"remove them."
                )
            else:
                response["message"] = (
                    f"Removed {reference} (no attached wire stubs or labels found)."
                )
        return response

    except Exception as e:
        logger.error(f"Error deleting schematic component: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


@serialize_on_param("schematicPath")
def handle_add_schematic_component(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Add a component to a schematic using text-based injection (no sexpdata)"""
    logger.info("Adding component to schematic")
    try:
        from pathlib import Path

        from commands.dynamic_symbol_loader import DynamicSymbolLoader

        schematic_path = params.get("schematicPath")
        component = params.get("component", {})

        if not schematic_path:
            return {"success": False, "message": "Schematic path is required"}
        if not component:
            return {"success": False, "message": "Component definition is required"}

        comp_type = component.get("type", "R")
        library = component.get("library", "Device")
        reference = component.get("reference", "X?")
        value = component.get("value", comp_type)
        footprint = component.get("footprint", "")
        x = component.get("x", 0)
        y = component.get("y", 0)
        unit = int(component.get("unit", 1) or 1)

        # Opt-in grid snap.  Read from the component dict OR the top-level
        # params so callers can pass it either next to the position or as a
        # request-level flag.  Off by default — caller must ask for it.
        snap_params = {
            "snapToGrid": (component.get("snapToGrid") or params.get("snapToGrid")),
            "snapGridMm": (component.get("snapGridMm") or params.get("snapGridMm")),
        }
        requested_x, requested_y = x, y
        x, y, snapped = _apply_grid_snap(x, y, snap_params)

        # Derive project path from schematic path for project-local library resolution.
        # Walk up from the schematic file to find the directory that owns the project
        # (contains sym-lib-table or a .kicad_pro file).  Schematics stored in a
        # sub-folder (e.g. sheets/) would otherwise resolve to the wrong directory and
        # miss any project-local sym-lib-table entries.
        schematic_file = Path(schematic_path)
        derived_project_path = schematic_file.parent
        for ancestor in schematic_file.parents:
            if (ancestor / "sym-lib-table").exists() or list(ancestor.glob("*.kicad_pro")):
                derived_project_path = ancestor
                break

        from commands.pin_locator import PinLocator

        place_all = bool(component.get("placeAllUnits") or params.get("placeAllUnits"))
        lib_id = f"{library}:{comp_type}"
        grid_mm = float(snap_params.get("snapGridMm") or _SCHEMATIC_GRID_MM)
        snap_on = snap_params.get("snapToGrid") is not False

        # Reference validation (A6/A11): a placed symbol must carry a non-empty,
        # unique reference designator.  An empty or duplicate refdes is an
        # invalid schematic (KiCad ERC flags "duplicate reference"), so refuse
        # it by default with a structured errorCode.  An opt-in ``autoAssign``
        # instead numbers the next free reference of the same prefix (mirroring
        # duplicate_schematic_component).  Placing another UNIT of a multi-unit
        # part already on the sheet legitimately reuses its reference, so that
        # case is allowed through unchanged.
        from ._duplicate import _collect_references, _next_free_reference

        auto_assign = bool(component.get("autoAssign") or params.get("autoAssign"))
        try:
            existing_refs = _collect_references(schematic_file.read_text(encoding="utf-8"))
        except OSError:
            existing_refs = []
        requested_reference = reference
        ref_str = str(reference).strip()

        def _ref_prefix(ref: str) -> str:
            m = re.match(r"^([A-Za-z_]+)", ref)
            if m:
                return m.group(1)
            # Empty/opaque ref: fall back to the component type's leading
            # letters, then a generic "U".
            tm = re.match(r"^([A-Za-z_]+)", str(comp_type))
            return tm.group(1) if tm else "U"

        if not ref_str:
            if auto_assign:
                reference = _next_free_reference(existing_refs, _ref_prefix(""))
            else:
                return {
                    "success": False,
                    "message": (
                        "reference is empty. A placed symbol needs a non-empty "
                        'reference designator (e.g. "R1", "U3"). Pass a '
                        "reference, or set autoAssign=true to number the next "
                        "free one automatically."
                    ),
                    "errorCode": "INVALID_REFERENCE",
                }
        elif ref_str in existing_refs:
            legit_new_unit = False
            if not place_all:
                try:
                    info = PinLocator().get_unit_placement(schematic_file, ref_str)
                except Exception:
                    info = None
                if (
                    info
                    and info.get("lib_id") == lib_id
                    and info.get("is_multi_unit")
                    and unit not in (info.get("placed_units") or [])
                ):
                    legit_new_unit = True
            if not legit_new_unit:
                if auto_assign:
                    reference = _next_free_reference(existing_refs, _ref_prefix(ref_str))
                else:
                    return {
                        "success": False,
                        "message": (
                            f"Reference '{ref_str}' is already used in this "
                            f"schematic. Two symbols sharing a refdes is invalid "
                            f"(KiCad ERC flags 'duplicate reference'). Choose "
                            f"another reference, or set autoAssign=true to place "
                            f"it as the next free '{_ref_prefix(ref_str)}<N>'."
                        ),
                        "errorCode": "REFERENCE_EXISTS",
                    }

        # Page-awareness (S9): reject coordinates so far off the sheet they can
        # only be a units mistake (x=99999 mm ≈ 100 m); a merely off-page point
        # still places but is flagged in the response.
        page = _read_page_size(schematic_file)
        primary_class = _classify_position(float(x), float(y), page)
        if primary_class == "absurd":
            return {
                "success": False,
                "message": (
                    f"Position ({requested_x}, {requested_y}) mm is far outside the "
                    f"{page['name']} sheet ({page['width']}×{page['height']} mm) — more than "
                    f"{int(_OFF_PAGE_ABSURD_FACTOR)}× a page dimension away. This is almost "
                    f"certainly a units error; use millimeters within (or near) the sheet."
                ),
                "errorCode": "POSITION_OFF_SHEET",
                "pageSize": page,
            }

        loader = DynamicSymbolLoader(project_path=derived_project_path)

        # Footprint inheritance (S14): an omitted / empty footprint arg falls
        # back to the library symbol's own Footprint property, so ICs placed
        # from native KiCad libraries (AMS1117-3.3 → SOT-223) or easyeda2kicad
        # imports (which record the footprint in their .kicad_sym) carry a
        # footprint that sync_schematic_to_board can place — instead of the
        # empty field that made it silently skip every IC.  An explicit
        # non-empty arg always wins; when neither exists the field stays "" and
        # the response says so.
        if footprint:
            footprint_source = "explicit"
        else:
            footprint_source = "none"
            try:
                inherited = loader.get_library_footprint(library, comp_type)
            except Exception:  # a library read must never block placement
                inherited = ""
            if inherited:
                footprint = inherited
                footprint_source = "library"

        def _place(unit_n: int, ux: float, uy: float) -> None:
            loader.add_component(
                schematic_file,
                library,
                comp_type,
                reference=reference,
                value=value,
                footprint=footprint,
                x=ux,
                y=uy,
                unit=unit_n,
                project_path=derived_project_path,
            )

        unit_positions: Dict[int, Dict[str, float]] = {}
        unit_boxes: Dict[int, Tuple[float, float, float, float]] = {}

        if place_all:
            # Place the first unit to inject the definition, then discover the
            # full unit roster and lay the remaining units out in a PAGE-AWARE
            # grid so every unit is on the sheet (F1/S2) — pins on an unplaced
            # unit have no real location and can't be labeled/connected, and a
            # tall multi-unit part must not stack off the bottom of the sheet.
            _place(1, x, y)
            unit_positions[1] = {"x": float(x), "y": float(y)}
            try:
                pins = PinLocator().get_symbol_pins(schematic_file, lib_id) or {}
            except Exception:  # best-effort: extents only affect grid spacing
                pins = {}
            unit_boxes = _unit_extents(pins)
            defined_units = sorted(
                {int(p["unit"]) for p in pins.values() if p.get("unit") not in (None, 0)}
            ) or [1]

            page_w = float(page["width"])
            page_h = float(page["height"])
            gap = _UNIT_STACK_GAP_MM

            def _snap(v: float) -> float:
                return _snap_to_schematic_grid(v, grid_mm) if snap_on else v

            box1 = unit_boxes.get(1)
            if box1:
                # Column starts at unit 1's placed footprint (Y-flip: screen
                # bottom = oy − min_lib_y, screen top = oy − max_lib_y).
                col_bottom = float(y) - box1[2]
                col_right = float(x) + box1[1]
                top_ref = float(y) - box1[3]
            else:
                col_bottom = float(y) + _DEFAULT_UNIT_HEIGHT_MM / 2.0
                col_right = float(x) + _DEFAULT_UNIT_WIDTH_MM / 2.0
                top_ref = float(y) - _DEFAULT_UNIT_HEIGHT_MM / 2.0

            col_origin_x = float(x)
            col_has_content = True  # unit 1 seeds the first column

            for u in defined_units:
                if u == 1:
                    continue
                box = unit_boxes.get(u)
                if box:
                    min_x, max_x, min_y, max_y = box
                else:
                    min_x, max_x = 0.0, 0.0
                    min_y, max_y = -_DEFAULT_UNIT_HEIGHT_MM / 2.0, _DEFAULT_UNIT_HEIGHT_MM / 2.0

                # Try stacking under the current column first.
                origin_y = col_bottom + gap + max_y
                origin_x = col_origin_x
                if (origin_y - min_y) > page_h and col_has_content:
                    # Would run off the sheet bottom → wrap to a new column,
                    # clearing the previous column's right-most pin by a gap.
                    col_origin_x = col_right + gap + max(-min_x, 0.0)
                    origin_x = col_origin_x
                    origin_y = top_ref + max_y
                    col_right = origin_x + max_x
                    col_has_content = False

                ox = _snap(origin_x)
                oy = _snap(origin_y)
                _place(u, ox, oy)
                unit_positions[u] = {"x": round(ox, 4), "y": round(oy, 4)}
                col_bottom = oy - min_y
                col_right = max(col_right, ox + max_x)
                col_has_content = True
        else:
            _place(unit, x, y)
            unit_positions[unit] = {"x": float(x), "y": float(y)}

        response: Dict[str, Any] = {
            "success": True,
            "component_reference": reference,
            "symbol_source": lib_id,
            "position": {"x": x, "y": y},
            "footprint": footprint,
            "footprintSource": footprint_source,
            "pageSize": page,
        }
        # A6/A11: surface an auto-assigned reference so the caller learns the
        # refdes it actually got (its requested one was empty or taken).
        if reference != requested_reference:
            response["autoAssignedReference"] = True
            response["requestedReference"] = requested_reference

        # Off-page reporting (S2/S9): the placement still succeeded, but flag
        # any unit that landed outside the sheet so the caller isn't surprised
        # by pins hanging off the border (a tall part at a low y, or a unit the
        # page-aware grid couldn't fully fit).
        off_page_units: List[int] = []
        if place_all:
            for u, pos in unit_positions.items():
                box = unit_boxes.get(u)
                if box is not None:
                    if _unit_offpage(pos, box, page):
                        off_page_units.append(u)
                elif _classify_position(pos["x"], pos["y"], page) == "off_page":
                    off_page_units.append(u)
            if off_page_units:
                off_page_units.sort()
                response["offPageUnits"] = off_page_units
                response["offPageWarning"] = (
                    f"Unit(s) {off_page_units} of {reference} extend outside the "
                    f"{page['name']} sheet ({page['width']}×{page['height']} mm). They are "
                    f"still placed (KiCad's canvas extends past the border) but their pins sit "
                    f"off-page; move them onto the sheet or enlarge the paper size."
                )
        elif primary_class == "off_page":
            response["offPageWarning"] = (
                f"Position ({x}, {y}) mm is outside the {page['name']} sheet "
                f"({page['width']}×{page['height']} mm). {reference} is still placed "
                f"(KiCad's canvas extends past the border) but sits off-page."
            )

        if footprint_source == "none":
            # No footprint anywhere: surface it so the agent knows
            # sync_schematic_to_board will skip this symbol until one is set.
            response["footprintNote"] = (
                f"No footprint set for {reference} ({lib_id}): neither an explicit "
                f"footprint argument nor a library default was available. "
                f"sync_schematic_to_board will skip this symbol until you assign one "
                f"(edit_schematic_component footprint=...)."
            )
        if snapped:
            # Tell the caller their coordinates moved — silent snap would
            # be surprising when an agent tries to land at exactly
            # (150, 100) and gets (149.86, 99.06).
            response["snap"] = {
                "applied": True,
                "gridMm": snap_params["snapGridMm"] or _SCHEMATIC_GRID_MM,
                "requested": {"x": requested_x, "y": requested_y},
            }

        # Multi-unit reporting (F1): tell the caller the unit situation so it
        # never assumes a single add_schematic_component placed the whole part.
        try:
            info = PinLocator().get_unit_placement(schematic_file, reference)
        except Exception:
            info = None
        if info and info["is_multi_unit"]:
            response["units"] = {
                "total": info["total_units"],
                "placed": info["placed_units"],
                "unplaced": info["unplaced_units"],
            }
            if place_all:
                response["unitPositions"] = {
                    str(u): unit_positions[u] for u in sorted(unit_positions)
                }
            unplaced = info["unplaced_units"]
            if unplaced:
                response["warning"] = (
                    f"{reference} ({lib_id}) is a multi-unit symbol with "
                    f"{info['total_units']} units; only unit(s) {info['placed_units']} "
                    f"is/are on the sheet. Unit(s) {unplaced} are NOT placed — their "
                    f"pins (e.g. power/ground on many MCUs) have no location and "
                    f"cannot be labeled or connected until placed."
                )
                response["next"] = (
                    f'Place the remaining unit(s): add_schematic_component(symbol="{lib_id}", '
                    f'reference="{reference}", unit=N) for N in {unplaced}, '
                    f"or re-run with placeAllUnits=true to place every unit at once."
                )
        return response
    except Exception as e:
        logger.error(f"Error adding component to schematic: {str(e)}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}
