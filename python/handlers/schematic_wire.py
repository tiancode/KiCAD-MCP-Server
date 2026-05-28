"""
Schematic Wire handlers, extracted from kicad_interface.py.

See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import sexpdata
from commands.connection_schematic import ConnectionManager
from commands.schematic import SchematicManager
from commands.wire_manager import WireManager

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


def handle_add_sheet_pin(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Add a sheet pin to a sheet block on the parent schematic."""
    logger.info("Adding sheet pin to schematic")
    try:
        from commands.wire_manager import WireManager

        schematic_path = params.get("schematicPath")
        sheet_name = params.get("sheetName")
        pin_name = params.get("pinName")
        pin_type = params.get("pinType", "bidirectional")
        position = params.get("position")
        orientation = params.get("orientation", 0)

        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}
        if not sheet_name:
            return {"success": False, "message": "sheetName is required"}
        if not pin_name:
            return {"success": False, "message": "pinName is required"}
        if not position or len(position) != 2:
            return {"success": False, "message": "position [x, y] is required"}
        if pin_type not in ("input", "output", "bidirectional"):
            return {
                "success": False,
                "message": "pinType must be input, output, or bidirectional",
            }

        sch_file = Path(schematic_path)
        if not sch_file.exists():
            return {
                "success": False,
                "message": f"Schematic not found: {schematic_path}",
            }

        with open(sch_file, "r", encoding="utf-8") as f:
            content = f.read()

        modified, success = WireManager.add_sheet_pin(
            content,
            sheet_name,
            pin_name,
            pin_type,
            position,
            orientation=orientation,
        )

        if not success:
            return {
                "success": False,
                "message": f"Sheet '{sheet_name}' not found in {schematic_path}",
            }

        with open(sch_file, "w", encoding="utf-8") as f:
            f.write(modified)

        return {
            "success": True,
            "message": (f"Added sheet pin '{pin_name}' ({pin_type}) " f"to sheet '{sheet_name}'"),
        }

    except Exception as e:
        logger.error(f"Error adding sheet pin: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_add_schematic_sheet(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Create a hierarchical sheet box on the parent schematic referencing a sub-sheet."""
    logger.info("Adding hierarchical sheet box to schematic")
    try:
        from commands.schematic import SchematicManager
        from commands.wire_manager import WireManager

        schematic_path = params.get("schematicPath")
        sheet_name = params.get("sheetName")
        sheet_file = params.get("sheetFile")
        position = params.get("position")
        size = params.get("size")
        page_number = params.get("pageNumber")
        create_sub_sheet = params.get("createSubSheet", True)

        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}
        if not sheet_name:
            return {"success": False, "message": "sheetName is required"}
        if not sheet_file:
            return {"success": False, "message": "sheetFile is required"}
        if not position or len(position) != 2:
            return {"success": False, "message": "position [x, y] is required"}
        if size is not None and len(size) != 2:
            return {"success": False, "message": "size must be [width, height]"}

        parent = Path(schematic_path)
        if not parent.exists():
            return {"success": False, "message": f"Parent schematic not found: {schematic_path}"}

        # Normalize the Sheetfile to a path relative to the parent's directory —
        # that is what _discover_sub_sheets and kicad-cli resolve against.
        parent_dir = parent.parent
        sf = Path(sheet_file)
        if sf.is_absolute():
            try:
                rel_sheet_file = str(sf.relative_to(parent_dir))
            except ValueError:
                rel_sheet_file = sf.name
            sub_sheet_abspath = sf
        else:
            rel_sheet_file = sheet_file
            sub_sheet_abspath = parent_dir / sheet_file

        created_sub_sheet = False
        if create_sub_sheet and not sub_sheet_abspath.exists():
            # Use the genuinely-empty template — template_with_symbols carries
            # offscreen _TEMPLATE_* placeholder instances that would otherwise
            # show up as phantom LED/C/R parts in the sub-sheet's ERC and BOM.
            SchematicManager.create_schematic(
                sub_sheet_abspath.stem,
                path=str(sub_sheet_abspath.parent),
                template="empty.kicad_sch",
            )
            created_sub_sheet = True

        success, info = WireManager.add_sheet(
            parent,
            sheet_name,
            rel_sheet_file,
            position,
            size=size,
            page_number=str(page_number) if page_number is not None else None,
        )

        if not success:
            return {
                "success": False,
                "message": f"Failed to add sheet: {info.get('error', 'unknown error')}",
            }

        return {
            "success": True,
            "message": (
                f"Added sheet '{sheet_name}' -> {rel_sheet_file} (page {info.get('page')})"
            ),
            "uuid": info.get("uuid"),
            "page": info.get("page"),
            "sheetFile": rel_sheet_file,
            "createdSubSheet": created_sub_sheet,
        }

    except Exception as e:
        logger.error(f"Error adding sheet: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


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


def handle_delete_schematic_wire(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Delete a wire from the schematic matching start/end points"""
    logger.info("Deleting schematic wire")
    try:
        schematic_path = params.get("schematicPath")
        start = params.get("start", {})
        end = params.get("end", {})

        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}

        from pathlib import Path

        from commands.wire_manager import WireManager

        start_point = [start.get("x", 0), start.get("y", 0)]
        end_point = [end.get("x", 0), end.get("y", 0)]

        deleted = WireManager.delete_wire(Path(schematic_path), start_point, end_point)
        if deleted:
            return {"success": True}
        else:
            return {"success": False, "message": "No matching wire found"}

    except Exception as e:
        logger.error(f"Error deleting schematic wire: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_connect_passthrough(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Connect all pins of source connector to matching pins of target connector"""
    logger.info("Connecting passthrough between two connectors")
    try:
        from pathlib import Path

        schematic_path = params.get("schematicPath")
        source_ref = params.get("sourceRef")
        target_ref = params.get("targetRef")
        net_prefix = params.get("netPrefix", "PIN")
        pin_offset = int(params.get("pinOffset", 0))

        if not all([schematic_path, source_ref, target_ref]):
            return {
                "success": False,
                "message": "Missing required parameters: schematicPath, sourceRef, targetRef",
            }

        result = ConnectionManager.connect_passthrough(
            Path(schematic_path), source_ref, target_ref, net_prefix, pin_offset
        )

        # Also assign nets to PCB pads for each successfully connected pin
        pcb_assigned = 0
        if iface.board:
            import re as _re

            for conn_info in result.get("connected", []):
                # Expected format: "{src_ref}/{pin} <-> {tgt_ref}/{pin} [{net}]"
                try:
                    parts = conn_info.split(" <-> ")
                    if len(parts) != 2:
                        continue
                    src_part = parts[0]
                    rest = parts[1]
                    bracket_match = _re.search(r"\[(.+)\]", rest)
                    tgt_part = rest.split(" [")[0] if " [" in rest else rest
                    net_name = bracket_match.group(1) if bracket_match else None
                    if not net_name:
                        continue

                    src_ref_pin = src_part.split("/")
                    tgt_ref_pin = tgt_part.split("/")
                    if len(src_ref_pin) == 2 and iface._assign_net_to_pad(
                        src_ref_pin[0], src_ref_pin[1], net_name
                    ):
                        pcb_assigned += 1
                    if len(tgt_ref_pin) == 2 and iface._assign_net_to_pad(
                        tgt_ref_pin[0], tgt_ref_pin[1], net_name
                    ):
                        pcb_assigned += 1
                except Exception as parse_err:
                    logger.debug(
                        f"Could not parse passthrough result for PCB assignment: {parse_err}"
                    )

        n_ok = len(result["connected"])
        n_fail = len(result["failed"])
        msg = f"Passthrough complete: {n_ok} connected, {n_fail} failed"
        if pcb_assigned:
            msg += f" ({pcb_assigned} PCB pads updated)"
        return {
            "success": n_fail == 0,
            "message": msg,
            "connected": result["connected"],
            "failed": result["failed"],
        }
    except Exception as e:
        logger.error(f"Error in connect_passthrough: {str(e)}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_connect_to_net(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Connect a component pin to a named net using wire stub and label,
    and also assign the net to the corresponding pad on the PCB board so
    that save_project persists the net (pcbnew.SaveBoard only writes nets
    that are referenced by at least one board element).
    """
    logger.info("Connecting component pin to net")
    try:
        from pathlib import Path

        schematic_path = params.get("schematicPath")
        component_ref = params.get("componentRef")
        pin_name = params.get("pinName")
        net_name = params.get("netName")

        if not all([schematic_path, component_ref, pin_name, net_name]):
            return {"success": False, "message": "Missing required parameters"}

        # Use ConnectionManager with new WireManager integration
        result = ConnectionManager.connect_to_net(
            Path(schematic_path), component_ref, pin_name, net_name
        )

        # Also assign the net to the pad on the PCB board
        if iface.board and isinstance(result, dict) and result.get("success"):
            try:
                if iface._assign_net_to_pad(component_ref, pin_name, net_name):
                    msg = result.get("message", "")
                    result["message"] = (msg + " (PCB pad also updated)").strip()
            except Exception as pcb_err:
                logger.warning(f"Could not assign net to PCB pad: {pcb_err}")

        return result
    except Exception as e:
        logger.error(f"Error connecting to net: {str(e)}")
        import traceback

        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": str(e),
            "errorDetails": traceback.format_exc(),
        }


def handle_add_no_connect(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Add a no-connect flag (X marker) to an unconnected pin in the schematic."""
    logger.info("Adding no-connect flag to schematic")
    try:
        from pathlib import Path

        from commands.pin_locator import PinLocator
        from commands.wire_manager import WireManager

        schematic_path = params.get("schematicPath")
        position = params.get("position")
        component_ref = params.get("componentRef")
        pin_number = params.get("pinNumber")

        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}

        # Snap to pin endpoint when componentRef + pinNumber are provided
        snapped_to_pin = None
        if component_ref and pin_number is not None:
            locator = PinLocator()
            pin_loc = locator.get_pin_location(Path(schematic_path), component_ref, str(pin_number))
            if pin_loc is None:
                return {
                    "success": False,
                    "message": f"Could not locate pin {pin_number} on {component_ref}",
                }
            position = pin_loc
            snapped_to_pin = {"component": component_ref, "pin": str(pin_number)}
        elif position is None:
            return {
                "success": False,
                "message": "Provide either position [x, y] or componentRef + pinNumber",
            }

        success = WireManager.add_no_connect(Path(schematic_path), position)
        if success:
            result = {
                "success": True,
                "message": f"Added no-connect flag at {position}",
                "actual_position": position,
            }
            if snapped_to_pin:
                result["snapped_to_pin"] = snapped_to_pin
            return result
        else:
            return {"success": False, "message": "Failed to add no-connect flag"}

    except Exception as e:
        import traceback

        logger.error(f"Error adding no-connect: {e}")
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
        orientation = params.get("orientation", 0)
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
                return {
                    "success": False,
                    "message": (
                        f"Could not locate pin {pin_number} on {component_ref}. "
                        "Check the reference and pin number."
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


def handle_add_schematic_wire(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Add a wire to a schematic using WireManager, with optional pin snapping"""
    logger.info("Adding wire to schematic")
    try:
        from pathlib import Path

        from commands.wire_manager import WireManager

        schematic_path = params.get("schematicPath")
        points = params.get("waypoints")
        properties = params.get("properties", {})
        snap_to_pins = params.get("snapToPins", True)
        snap_tolerance = params.get("snapTolerance", 1.0)

        if not schematic_path:
            return {"success": False, "message": "Schematic path is required"}
        if not points or len(points) < 2:
            return {
                "success": False,
                "message": "At least 2 waypoints are required",
            }

        # Make a mutable copy of points
        points = [list(p) for p in points]

        # Pin snapping: adjust first and last endpoints to nearest pin
        snapped_info = []
        if snap_to_pins:
            from commands.pin_locator import PinLocator

            locator = PinLocator()
            sch_path = Path(schematic_path)

            # Load schematic to iterate all symbols
            from skip import Schematic as SkipSchematic

            sch = SkipSchematic(str(sch_path))

            # Collect all pin locations: list of (ref, pin_num, [x, y])
            all_pins = []
            for symbol in sch.symbol:
                if not hasattr(symbol.property, "Reference"):
                    continue
                ref = symbol.property.Reference.value
                if ref.startswith("_TEMPLATE"):
                    continue
                pin_locs = locator.get_all_symbol_pins(sch_path, ref)
                for pin_num, coords in pin_locs.items():
                    all_pins.append((ref, pin_num, coords))

            def find_nearest_pin(point: Any, tolerance: Any) -> Any:
                """Find the nearest pin within tolerance of a point."""
                best = None
                best_dist = tolerance
                for ref, pin_num, coords in all_pins:
                    dx = point[0] - coords[0]
                    dy = point[1] - coords[1]
                    dist = (dx * dx + dy * dy) ** 0.5
                    if dist <= best_dist:
                        best_dist = dist
                        best = (ref, pin_num, coords)
                return best

            # Snap first endpoint
            match = find_nearest_pin(points[0], snap_tolerance)
            if match:
                ref, pin_num, coords = match
                logger.info(f"Snapped start point {points[0]} -> {coords} (pin {ref}/{pin_num})")
                snapped_info.append(
                    f"start snapped to {ref}/{pin_num} at [{coords[0]}, {coords[1]}]"
                )
                points[0] = list(coords)

            # Snap last endpoint
            match = find_nearest_pin(points[-1], snap_tolerance)
            if match:
                ref, pin_num, coords = match
                logger.info(f"Snapped end point {points[-1]} -> {coords} (pin {ref}/{pin_num})")
                snapped_info.append(f"end snapped to {ref}/{pin_num} at [{coords[0]}, {coords[1]}]")
                points[-1] = list(coords)

        # Extract wire properties
        stroke_width = properties.get("stroke_width", 0)
        stroke_type = properties.get("stroke_type", "default")

        # Use WireManager for S-expression manipulation
        if len(points) == 2:
            success = WireManager.add_wire(
                Path(schematic_path),
                points[0],
                points[1],
                stroke_width=stroke_width,
                stroke_type=stroke_type,
            )
        else:
            success = WireManager.add_polyline_wire(
                Path(schematic_path),
                points,
                stroke_width=stroke_width,
                stroke_type=stroke_type,
            )

        if success:
            message = "Wire added successfully"
            if snapped_info:
                message += "; " + "; ".join(snapped_info)
            return {"success": True, "message": message}
        else:
            return {"success": False, "message": "Failed to add wire"}
    except Exception as e:
        logger.error(f"Error adding wire to schematic: {str(e)}")
        import traceback

        logger.error(traceback.format_exc())
        return {
            "success": False,
            "message": str(e),
            "errorDetails": traceback.format_exc(),
        }
