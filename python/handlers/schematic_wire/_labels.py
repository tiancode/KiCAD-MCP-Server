"""Net-label and hierarchical-label handlers + pin-snapping helpers.

Split out of the former handlers/schematic_wire.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from commands.schematic import SchematicManager

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.schematic_wire")


def handle_add_schematic_hierarchical_label(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Add a hierarchical label to a sub-sheet schematic."""
    logger.info("Adding hierarchical label to schematic")
    try:
        from commands.wire_manager import WireManager

        schematic_path = params.get("schematicPath")
        text = params.get("text")
        position = params.get("position")
        shape = params.get("shape", "bidirectional")
        orientation = params.get("orientation", 0)

        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}
        if not text:
            return {"success": False, "message": "text is required"}
        if not position or len(position) != 2:
            return {"success": False, "message": "position [x, y] is required"}
        if shape not in ("input", "output", "bidirectional"):
            return {
                "success": False,
                "message": "shape must be input, output, or bidirectional",
            }

        sch_file = Path(schematic_path)
        if not sch_file.exists():
            return {
                "success": False,
                "message": f"Schematic not found: {schematic_path}",
            }

        success = WireManager.add_hierarchical_label(
            sch_file, text, position, shape=shape, orientation=orientation
        )

        if success:
            return {
                "success": True,
                "message": (f"Added hierarchical_label '{text}' " f"at {position} shape={shape}"),
            }
        return {"success": False, "message": "Failed to add hierarchical label"}

    except Exception as e:
        logger.error(f"Error adding hierarchical label: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_move_schematic_net_label(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Move a net label to a new position in the schematic."""
    logger.info("Moving schematic net label")
    try:
        import sexpdata as _sexpdata
        from sexpdata import Symbol

        schematic_path = params.get("schematicPath")
        net_name = params.get("netName")
        new_position = params.get("newPosition", {})
        new_x = new_position.get("x")
        new_y = new_position.get("y")
        current_position = params.get("currentPosition")
        label_type = params.get("labelType")

        if not schematic_path or not net_name:
            return {"success": False, "message": "schematicPath and netName are required"}
        if new_x is None or new_y is None:
            return {"success": False, "message": "newPosition with x and y is required"}

        _valid_types = {"label", "global_label", "hierarchical_label"}
        if label_type is not None and label_type not in _valid_types:
            return {
                "success": False,
                "message": f"labelType must be one of: {', '.join(sorted(_valid_types))}",
            }

        _SYM_AT = Symbol("at")
        target_syms = (
            {Symbol(label_type)} if label_type is not None else {Symbol(t) for t in _valid_types}
        )

        TOLERANCE = 0.5

        with open(schematic_path, "r", encoding="utf-8") as f:
            sch_data = _sexpdata.loads(f.read())

        for item in sch_data:
            if not (isinstance(item, list) and len(item) >= 2 and item[0] in target_syms):
                continue
            if item[1] != net_name:
                continue

            at_idx = next(
                (
                    j
                    for j, p in enumerate(item)
                    if isinstance(p, list) and len(p) >= 3 and p[0] == _SYM_AT
                ),
                None,
            )
            if at_idx is None:
                continue

            at_entry = item[at_idx]
            old_x, old_y = float(at_entry[1]), float(at_entry[2])

            if current_position is not None:
                cx = current_position.get("x", 0)
                cy = current_position.get("y", 0)
                if not (abs(old_x - cx) < TOLERANCE and abs(old_y - cy) < TOLERANCE):
                    continue

            rotation = at_entry[3] if len(at_entry) > 3 else 0
            item[at_idx] = [_SYM_AT, float(new_x), float(new_y), rotation]

            with open(schematic_path, "w", encoding="utf-8") as f:
                f.write(_sexpdata.dumps(sch_data))

            return {
                "success": True,
                "oldPosition": {"x": old_x, "y": old_y},
                "newPosition": {"x": float(new_x), "y": float(new_y)},
            }

        return {"success": False, "message": f"Label '{net_name}' not found"}

    except Exception as e:
        logger.error(f"Error moving schematic net label: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_delete_schematic_net_label(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Delete a net label from the schematic"""
    logger.info("Deleting schematic net label")
    try:
        schematic_path = params.get("schematicPath")
        net_name = params.get("netName")
        position = params.get("position")

        if not schematic_path or not net_name:
            return {
                "success": False,
                "message": "schematicPath and netName are required",
            }

        from pathlib import Path

        from commands.wire_manager import WireManager

        pos_list = None
        if position:
            pos_list = [position.get("x", 0), position.get("y", 0)]

        deleted = WireManager.delete_label(Path(schematic_path), net_name, pos_list)
        if deleted:
            return {"success": True}
        else:
            return {"success": False, "message": f"Label '{net_name}' not found"}

    except Exception as e:
        logger.error(f"Error deleting schematic net label: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_edit_schematic_net_label(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Change a net label's type (local/global/hierarchical) and/or its text.

    Converts in place — same uuid and position — so fixing a page-local net
    mistakenly created as a global label needs no wire/junction rework. Pass
    at least one of newLabelType / newName.
    """
    logger.info("Editing schematic net label")
    try:
        from pathlib import Path

        from commands.wire_manager import WireManager

        schematic_path = params.get("schematicPath")
        net_name = params.get("netName")
        new_label_type = params.get("newLabelType")
        new_name = params.get("newName")
        current_position = params.get("currentPosition")
        label_type = params.get("labelType")
        tolerance = float(params.get("tolerance", 0.5))

        if not schematic_path or not net_name:
            return {"success": False, "message": "schematicPath and netName are required"}
        if new_label_type is None and new_name is None:
            return {
                "success": False,
                "message": "Provide at least one of newLabelType or newName",
            }

        pos_list = None
        if current_position:
            pos_list = [current_position.get("x", 0), current_position.get("y", 0)]

        try:
            result = WireManager.edit_label(
                Path(schematic_path),
                net_name,
                new_type=new_label_type,
                new_name=new_name,
                position=pos_list,
                current_type=label_type,
                tolerance=tolerance,
            )
        except ValueError as ve:
            # Unrecognised label type — surface the clear message verbatim.
            return {"success": False, "message": str(ve)}

        if result is None:
            return {"success": False, "message": f"Label '{net_name}' not found"}
        return {"success": True, **result}

    except Exception as e:
        import traceback

        logger.error(f"Error editing schematic net label: {e}")
        return {
            "success": False,
            "message": str(e),
            "errorDetails": traceback.format_exc(),
        }


_LABEL_PIN_CONNECT_TOLERANCE_MM = 0.0001  # KiCad's internal-unit precision (0.1 µm)


def _scan_all_pin_positions(schematic_path: Any) -> List[Dict[str, Any]]:
    """Return every (ref, pin_number, [x_mm, y_mm]) on the schematic."""
    from pathlib import Path

    from commands.pin_locator import PinLocator
    from skip import Schematic as SkipSchematic

    sch_path = Path(schematic_path)
    locator = PinLocator()
    sch = SkipSchematic(str(sch_path))
    pins: List[Dict[str, Any]] = []
    for symbol in getattr(sch, "symbol", []):
        if not hasattr(symbol, "property") or not hasattr(symbol.property, "Reference"):
            continue
        ref = symbol.property.Reference.value
        if ref.startswith("_TEMPLATE"):
            continue
        try:
            pin_locs = locator.get_all_symbol_pins(sch_path, ref)
        except Exception:
            continue
        for pin_num, coords in pin_locs.items():
            pins.append({"ref": ref, "pin": str(pin_num), "coords": list(coords)})
    return pins


def _lookup_symbol_lib_value(schematic_path: Any, ref: str) -> tuple:
    """Return ``(lib_id, value)`` for the placed symbol whose Reference == ref.

    Used to detect power *ports* (lib_id ``power:*`` / ref ``#PWR…``) so a label
    that merely duplicates a power symbol's Value can be skipped (F4). Returns
    ``(None, None)`` when the symbol or file can't be read. Reads fresh (not via
    the PinLocator cache) so a just-written file is always seen.
    """
    from skip import Schematic as SkipSchematic

    try:
        sch = SkipSchematic(str(schematic_path))
    except Exception as e:
        logger.debug(f"_lookup_symbol_lib_value: could not load {schematic_path}: {e}")
        return None, None
    for symbol in getattr(sch, "symbol", []):
        try:
            if not hasattr(symbol, "property") or not hasattr(symbol.property, "Reference"):
                continue
            if symbol.property.Reference.value.rstrip("_") != ref:
                continue
            lib_id = symbol.lib_id.value if hasattr(symbol, "lib_id") else None
            value = symbol.property.Value.value if hasattr(symbol.property, "Value") else None
            return lib_id, value
        except AttributeError:
            continue
    return None, None


def _is_power_port(ref: Optional[str], lib_id: Optional[str]) -> bool:
    """True for a power-PORT symbol (#PWR…, lib_id ``power:*``).

    PWR_FLAG (#FLG, lib ``power:PWR_FLAG``) is NOT a named port — labeling its
    pin IS the correct attachment idiom — so it is explicitly excluded.
    """
    if not ref or not ref.startswith("#PWR") or ref.startswith("#FLG"):
        return False
    return str(lib_id or "").startswith("power:")


def handle_add_schematic_net_label(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Add a net label to schematic using WireManager.

    Three placement modes:

    * ``componentRef`` + ``pinNumber`` (preferred): snap to the exact pin
      endpoint via PinLocator.  The response reports ``snapped_to_pin``.
    * raw ``position``: by default the label is snapped onto the nearest
      pin within ``snapTolerance`` mm (default 0.05 mm) so caller-side
      float imprecision doesn't silently break the electrical
      connection.  When no pin is within tolerance the raw coordinates
      are used unchanged.
    * raw ``position`` with ``snapTolerance: 0``: opt-out of pin
      snapping entirely; useful for labels that intentionally float
      between pins.

    Either way the response carries ``connected_to_pin`` (the pin the
    final coordinates actually land on at KiCad's electrical-grid
    precision, or ``None`` for a free-floating label) so the caller can
    verify the electrical connection without running ERC.
    """
    logger.info("Adding net label to schematic")
    try:
        from pathlib import Path

        from commands.wire_manager import WireManager

        schematic_path = params.get("schematicPath")
        net_name = params.get("netName")
        position = params.get("position")
        label_type = params.get("labelType", "label")
        # Distinguish "orientation omitted" from "explicitly passed 0": an
        # explicit value (0 included) is honored verbatim; when omitted the
        # orientation is derived from the pin the label lands on so its text
        # extends away from the symbol body (left pin → 180/justify right).
        orientation_explicit = "orientation" in params and params["orientation"] is not None
        orientation = params["orientation"] if orientation_explicit else 0
        component_ref = params.get("componentRef")
        pin_number = params.get("pinNumber")
        snap_tolerance = float(params.get("snapTolerance", 0.05))

        if not all([schematic_path, net_name]):
            return {
                "success": False,
                "message": "Missing required parameters: schematicPath, netName",
            }

        snapped_to_pin: Optional[Dict[str, Any]] = None
        requested_position: Optional[List[float]] = (
            list(position) if isinstance(position, (list, tuple)) else None
        )

        if component_ref and pin_number:
            # Snap position to exact pin endpoint using PinLocator
            from commands.pin_locator import PinLocator

            locator = PinLocator()
            pin_loc = locator.get_pin_location(Path(schematic_path), component_ref, str(pin_number))
            if pin_loc is None:
                # Distinguish a pin on an UNPLACED multi-unit unit (F1) — which
                # must never be "connected" to a phantom coordinate — from a
                # genuinely missing pin. The former gets the exact fix-up call.
                diag = locator.diagnose_missing_pin(
                    Path(schematic_path), component_ref, str(pin_number)
                )
                if diag.get("reason") == "unplaced_unit":
                    return {
                        "success": False,
                        "message": locator.format_unplaced_unit_error(component_ref, diag),
                        "needs_unit_placement": True,
                        "unit": diag.get("pin_unit"),
                        "unplaced_units": diag.get("unplaced_units", []),
                    }
                # S10: distinguish a missing COMPONENT from a missing PIN.
                return {
                    "success": False,
                    "message": locator.format_missing_pin_error(
                        component_ref, str(pin_number), diag
                    ),
                }
            position = pin_loc
            snapped_to_pin = {"component": component_ref, "pin": str(pin_number)}
            logger.info(
                f"Snapped label '{net_name}' to pin {component_ref}/{pin_number} at {position}"
            )
        elif position is None:
            return {
                "success": False,
                "message": (
                    "Missing position. Either provide position [x, y] or "
                    "componentRef + pinNumber to snap to a pin endpoint."
                ),
            }
        elif snap_tolerance > 0:
            # Raw position given; auto-snap to the nearest pin within
            # snapTolerance mm.  This is the safety net for callers that
            # compute pin coords with float imprecision — KiCad treats a
            # 0.01 mm offset as electrically disconnected.
            try:
                all_pins = _scan_all_pin_positions(schematic_path)
            except Exception as e:
                logger.debug(f"Pin scan for label snap failed: {e}")
                all_pins = []
            best = None
            best_dist = snap_tolerance
            for entry in all_pins:
                coords = entry["coords"]
                dx = float(position[0]) - float(coords[0])
                dy = float(position[1]) - float(coords[1])
                dist = (dx * dx + dy * dy) ** 0.5
                if dist <= best_dist:
                    best_dist = dist
                    best = entry
            if best is not None and best_dist > _LABEL_PIN_CONNECT_TOLERANCE_MM:
                # Near-miss: snap onto the actual pin coords so the
                # electrical connection forms.  Skip when already on the
                # endpoint (dist=0 stays unchanged).
                position = list(best["coords"])
                snapped_to_pin = {
                    "component": best["ref"],
                    "pin": best["pin"],
                    "snap_distance_mm": best_dist,
                }
                logger.info(
                    f"Auto-snapped label '{net_name}' from {requested_position} to "
                    f"{best['ref']}/{best['pin']} at {position} (Δ={best_dist:.4f} mm)"
                )

        # Resolve which pin (if any) the FINAL coordinates land on — shared by
        # the power-symbol short-circuit and the outward-orientation derivation.
        landing_pin: Optional[Dict[str, str]] = None
        if snapped_to_pin:
            landing_pin = {"ref": snapped_to_pin["component"], "pin": str(snapped_to_pin["pin"])}
        elif position is not None:
            # Exact-hit raw position: no snap occurred, but the coordinates may
            # still sit on a pin endpoint — resolve it before the write.
            try:
                for entry in _scan_all_pin_positions(schematic_path):
                    cx, cy = entry["coords"]
                    if (
                        abs(float(position[0]) - float(cx)) <= _LABEL_PIN_CONNECT_TOLERANCE_MM
                        and abs(float(position[1]) - float(cy)) <= _LABEL_PIN_CONNECT_TOLERANCE_MM
                    ):
                        landing_pin = {"ref": entry["ref"], "pin": entry["pin"]}
                        break
            except Exception as e:
                logger.debug(f"Landing-pin scan failed: {e}")

        # Power-symbol short-circuit (F4): a power PORT already joins the net
        # named by its Value and self-labels its own pin, so a matching label is
        # a redundant double label — skip the write entirely. A mismatched name
        # is almost certainly a mistake (the pin then carries both names); write
        # it but warn. PWR_FLAG is not a named port, so it never reaches here.
        power_warnings: List[str] = []
        if landing_pin is not None:
            lib_id_lp, value_lp = _lookup_symbol_lib_value(schematic_path, landing_pin["ref"])
            if _is_power_port(landing_pin["ref"], lib_id_lp):
                ref_lp = landing_pin["ref"]
                if value_lp is not None and net_name == value_lp:
                    logger.info(
                        f"Skipping redundant '{net_name}' label on power symbol "
                        f"{ref_lp} (its Value already names the net)"
                    )
                    return {
                        "success": True,
                        "already_connected": True,
                        "skipped_label": True,
                        "connected_to_pin": {"ref": ref_lp, "pin": landing_pin["pin"]},
                        "power_symbol": {"ref": ref_lp, "value": value_lp},
                        "message": (
                            f"{ref_lp} is a power symbol; its pin already joins net "
                            f"'{value_lp}' via its Value, so no label was written. "
                            f"Power symbols self-label their pin — adding a "
                            f"'{net_name}' label would duplicate it."
                        ),
                    }
                power_warnings.append(
                    f"{ref_lp} is a power symbol already driving net '{value_lp}' via "
                    f"its Value. Labeling its pin '{net_name}' does not rename that "
                    f"net: the pin ends up on BOTH '{value_lp}' (from the symbol) and "
                    f"'{net_name}' (from this label), which is almost certainly a "
                    f"mistake. Use a plain net label on a wire instead."
                )

        # Derive the label orientation from the pin the final coordinates land
        # on (unless the caller passed one). PinLocator.get_pin_angle returns the
        # OUTWARD angle (0=right, 90=up, 180=left, 270=down); rounding it to a
        # KiCad label orientation makes the text extend away from the symbol body
        # (WireManager.add_label picks justify right for 180/270). Mirrors the
        # snap connect_to_net applies. Free-floating labels and any failure → 0.
        orientation_source = "explicit" if orientation_explicit else "default"
        if not orientation_explicit and landing_pin is not None:
            try:
                from commands.pin_locator import PinLocator

                pin_angle = PinLocator().get_pin_angle(
                    Path(schematic_path), landing_pin["ref"], str(landing_pin["pin"])
                )
                if pin_angle is not None:
                    orientation = int(round(float(pin_angle) / 90.0) * 90) % 360
                    orientation_source = "pin_outward"
            except Exception as e:
                logger.debug(f"Pin-angle derivation for label orientation failed: {e}")
                orientation = 0

        # Collect existing net names BEFORE adding the new label so we can
        # detect case-mismatch collisions against pre-existing nets only.
        existing_net_names: List[str] = []
        try:
            pre_schematic = SchematicManager.load_schematic(schematic_path)
            if pre_schematic is not None:
                if hasattr(pre_schematic, "label"):
                    for lbl in pre_schematic.label:
                        if hasattr(lbl, "value"):
                            existing_net_names.append(lbl.value)
                if hasattr(pre_schematic, "global_label"):
                    for lbl in pre_schematic.global_label:
                        if hasattr(lbl, "value"):
                            existing_net_names.append(lbl.value)
        except Exception:
            # Non-fatal: if we can't read existing nets, skip the warning
            existing_net_names = []

        # Use WireManager for S-expression manipulation
        success = WireManager.add_label(
            Path(schematic_path),
            net_name,
            position,
            label_type=label_type,
            orientation=orientation,
        )

        if not success:
            return {"success": False, "message": "Failed to add net label"}

        # Compute case-mismatch warnings against pre-existing net names.
        # A collision is: existing name != new name, but lowercases match.
        new_name_lower = net_name.lower()
        case_warnings: List[str] = [
            f"Net '{existing}' already exists — label '{net_name}' may be a case mismatch."
            for existing in existing_net_names
            if existing.lower() == new_name_lower and existing != net_name
        ]

        # Resolve electrical connectivity: which pin (if any) does the
        # final coordinate actually land on at KiCad's IU-precision?  The
        # agent gets this on every call so it can verify the label will
        # connect without having to round-trip through run_erc.
        connected_to_pin: Optional[Dict[str, str]] = None
        try:
            for entry in _scan_all_pin_positions(schematic_path):
                cx, cy = entry["coords"]
                if (
                    abs(float(position[0]) - float(cx)) <= _LABEL_PIN_CONNECT_TOLERANCE_MM
                    and abs(float(position[1]) - float(cy)) <= _LABEL_PIN_CONNECT_TOLERANCE_MM
                ):
                    connected_to_pin = {"ref": entry["ref"], "pin": entry["pin"]}
                    break
        except Exception:
            connected_to_pin = None

        response: Dict[str, Any] = {
            "success": True,
            "message": f"Added net label '{net_name}' at {position}",
            "actual_position": position,
            "connected_to_pin": connected_to_pin,
            "orientation": orientation,
            "orientation_source": orientation_source,
        }
        if requested_position is not None and snapped_to_pin and not (component_ref and pin_number):
            # Auto-snap path — surface what we moved so the caller knows
            # the recorded position differs from what they asked for.
            response["requested_position"] = requested_position
        if snapped_to_pin:
            response["snapped_to_pin"] = snapped_to_pin
            response["message"] = (
                f"Added net label '{net_name}' at pin endpoint "
                f"{snapped_to_pin['component']}/{snapped_to_pin['pin']} "
                f"({position[0]}, {position[1]})"
            )
        if case_warnings:
            response["case_warnings"] = case_warnings
        if power_warnings:
            response["warnings"] = power_warnings
        return response

    except Exception as e:
        logger.error(f"Error adding net label: {str(e)}")
        import traceback

        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": str(e),
            "errorDetails": traceback.format_exc(),
        }
