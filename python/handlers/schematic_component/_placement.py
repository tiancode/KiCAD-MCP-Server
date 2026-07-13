"""Add / move / rotate / delete / annotate component handlers (+ grid-snap helpers).

Split out of the former handlers/schematic_component.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Tuple

import sexpdata
from commands.schematic import SchematicManager
from commands.schematic_locks import atomic_write_text, serialize_on_param
from commands.wire_manager import WireManager

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


# placeAllUnits stacks each unit of a multi-unit symbol vertically. The gap is
# added between a unit's bottom and the next unit's origin so bodies don't
# overlap; the fallback height is used when a unit's pin span can't be measured.
_UNIT_STACK_GAP_MM = 7.62
_DEFAULT_UNIT_HEIGHT_MM = 25.4


def _snap_to_schematic_grid(value: float, grid_mm: float = _SCHEMATIC_GRID_MM) -> float:
    """Snap a millimeter coordinate to the nearest schematic-grid multiple."""
    if grid_mm <= 0:
        return value
    return round(value / grid_mm) * grid_mm


def _unit_pin_heights(pins: Dict[str, Any]) -> Dict[int, float]:
    """Vertical pin span (mm) of each numbered unit, for stacking placeAllUnits.

    ``pins`` is PinLocator.get_symbol_pins output (pin_num → {..., unit, y}).
    Unit 0 (common/graphic) pins are ignored. A unit with fewer than two pins
    collapses to a zero span; the caller substitutes a sane default.
    """
    by_unit: Dict[int, list] = {}
    for pdata in pins.values():
        u = pdata.get("unit")
        if u in (None, 0):
            continue
        by_unit.setdefault(int(u), []).append(float(pdata.get("y", 0.0)))
    return {u: (max(ys) - min(ys)) for u, ys in by_unit.items() if ys}


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
            sch_data, reference, float(angle), new_mirror_x, new_mirror_y
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
            sch_data, reference, float(angle), effective_mirror
        )

        WireManager.sync_junctions(sch_data)

        atomic_write_text(schematic_path, _sexpdata.dumps(sch_data))

        return {
            "success": True,
            "reference": reference,
            "angle": angle,
            "mirror": effective_mirror,
            "wiresMoved": drag_summary.get("endpoints_moved", 0),
            "wiresRemoved": drag_summary.get("wires_removed", 0),
        }

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

            drag_summary = WireDragger.drag_wires(sch_data, old_to_new)

            # Move any net label sitting on a moved point — a pin the component
            # dragged, or the far end of a rigidly-moved stub — so no label is
            # left orphaned at the old coordinate (which would silently keep the
            # net tied there).
            labels_moved = WireDragger.move_labels_at_points(sch_data, old_to_new)
            drag_summary["labels_moved"] = labels_moved

            # Synthesize wires for touching-pin connections after dragging,
            # so drag_wires doesn't accidentally move and collapse the new wire.
            wires_synthesized = WireDragger.synthesize_touching_pin_wires(
                sch_data, reference, pin_positions
            )
            drag_summary["wires_synthesized"] = wires_synthesized

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
        }
        if snapped:
            response["snap"] = {
                "applied": True,
                "gridMm": snap_params["snapGridMm"] or _SCHEMATIC_GRID_MM,
                "requested": {"x": requested_new_x, "y": requested_new_y},
            }
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
        import re
        from pathlib import Path

        schematic_path = params.get("schematicPath")
        reference = params.get("reference")

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

        with open(sch_file, "r", encoding="utf-8") as f:
            content = f.read()

        # Skip lib_symbols section
        lib_sym_pos = content.find("(lib_symbols")
        lib_sym_end = iface._find_matching_paren(content, lib_sym_pos) if lib_sym_pos >= 0 else -1

        # Find ALL placed symbol blocks matching the reference (handles duplicates).
        # Use content-string search so multi-line KiCAD format is handled correctly:
        # KiCAD writes (symbol\n\t\t(lib_id "...") across two lines, which a
        # line-by-line regex would never match.
        blocks_to_delete = []  # list of (char_start, char_end) into content
        search_start = 0
        # Match the opening of any placed-symbol block. KiCAD may emit the
        # children of (symbol ...) in any order — most commonly
        # `(symbol (lib_id "..."))`, but symbols whose library entry has been
        # rescued / customised carry an additional `(lib_name "...")` first:
        # `(symbol (lib_name "...") (lib_id "...") ...)`. Matching just
        # `(symbol\s+(` covers both, and the lib_symbols range check below
        # still excludes library-definition symbols (which use the
        # `(symbol "name" ...)` form with a quoted string, not a paren).
        pattern = re.compile(r"\(symbol\s+\(")
        while True:
            m = pattern.search(content, search_start)
            if not m:
                break
            pos = m.start()
            # Skip blocks inside lib_symbols
            if lib_sym_pos >= 0 and lib_sym_pos <= pos <= lib_sym_end:
                search_start = lib_sym_end + 1
                continue
            end = iface._find_matching_paren(content, pos)
            if end < 0:
                search_start = pos + 1
                continue
            block_text = content[pos : end + 1]
            if re.search(
                r'\(property\s+"Reference"\s+"' + re.escape(reference) + r'"',
                block_text,
            ):
                blocks_to_delete.append((pos, end))
            search_start = end + 1

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
        return {
            "success": True,
            "reference": reference,
            "deleted_count": deleted_count,
            "schematic": str(sch_file),
        }

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

        if place_all:
            # Place the first unit to inject the definition, then discover the
            # full unit roster and stack the remaining units vertically so every
            # unit is on the sheet (F1) — pins on an unplaced unit have no real
            # location and can't be labeled/connected.
            _place(1, x, y)
            unit_positions[1] = {"x": x, "y": y}
            try:
                pins = PinLocator().get_symbol_pins(schematic_file, lib_id) or {}
            except Exception:  # best-effort: heights only affect stacking spacing
                pins = {}
            heights = _unit_pin_heights(pins)
            defined_units = sorted(
                {int(p["unit"]) for p in pins.values() if p.get("unit") not in (None, 0)}
            ) or [1]
            cursor_y = float(y) + heights.get(1, _DEFAULT_UNIT_HEIGHT_MM) + _UNIT_STACK_GAP_MM
            for u in defined_units:
                if u == 1:
                    continue
                uy = _snap_to_schematic_grid(cursor_y, grid_mm) if snap_on else cursor_y
                _place(u, x, uy)
                unit_positions[u] = {"x": x, "y": round(uy, 4)}
                cursor_y = uy + heights.get(u, _DEFAULT_UNIT_HEIGHT_MM) + _UNIT_STACK_GAP_MM
        else:
            _place(unit, x, y)
            unit_positions[unit] = {"x": x, "y": y}

        response: Dict[str, Any] = {
            "success": True,
            "component_reference": reference,
            "symbol_source": lib_id,
            "position": {"x": x, "y": y},
            "footprint": footprint,
            "footprintSource": footprint_source,
        }
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
