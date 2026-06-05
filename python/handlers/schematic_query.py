"""
Schematic Query handlers, extracted from kicad_interface.py.

See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

from commands.library_schematic import LibraryManager as SchematicLibraryManager
from commands.schematic import SchematicManager
from utils.pagination import paginate

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


def handle_add_schematic_text(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Add a free-form text annotation (SCH_TEXT) to a schematic."""
    logger.info("Adding text annotation to schematic")
    try:
        from commands.wire_manager import WireManager

        schematic_path = params.get("schematicPath")
        text = params.get("text")
        position = params.get("position")
        angle = params.get("angle", 0)
        font_size = params.get("fontSize", 1.27)
        bold = params.get("bold", False)
        italic = params.get("italic", False)
        justify = params.get("justify", "left")

        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}
        if not text:
            return {"success": False, "message": "text is required"}
        if not position or len(position) != 2:
            return {"success": False, "message": "position [x, y] is required"}
        if justify not in ("left", "center", "right"):
            return {"success": False, "message": "justify must be left, center, or right"}
        if font_size <= 0:
            return {"success": False, "message": "fontSize must be positive"}

        sch_file = Path(schematic_path)
        if not sch_file.exists():
            return {
                "success": False,
                "message": f"Schematic not found: {schematic_path}",
            }

        success = WireManager.add_text(
            sch_file,
            text,
            position,
            angle=angle,
            font_size=font_size,
            bold=bold,
            italic=italic,
            justify=justify,
        )

        if success:
            return {
                "success": True,
                "message": f"Added text '{text}' at ({position[0]}, {position[1]})",
                "position": {"x": position[0], "y": position[1]},
                "angle": angle,
            }
        return {"success": False, "message": "Failed to add text annotation"}

    except Exception as e:
        logger.error(f"Error adding schematic text: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_list_schematic_texts(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """List all free-form text annotations (SCH_TEXT) in a schematic."""
    logger.info("Listing schematic text annotations")
    try:
        from commands.wire_manager import WireManager

        schematic_path = params.get("schematicPath")
        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}

        sch_file = Path(schematic_path)
        if not sch_file.exists():
            return {"success": False, "message": f"Schematic not found: {schematic_path}"}

        texts = WireManager.list_texts(sch_file)
        if texts is None:
            return {"success": False, "message": "Failed to parse schematic"}

        # Optional text filter
        filter_text = params.get("text")
        if filter_text is not None:
            texts = [t for t in texts if filter_text.lower() in t["text"].lower()]

        texts, page = paginate(texts, params)
        return {"success": True, "texts": texts, **page}

    except Exception as e:
        logger.error(f"Error listing schematic texts: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_get_net_at_point(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Return the net name at a given (x, y) coordinate, or null if none found."""
    logger.info("Getting net at point")
    try:
        from commands.wire_connectivity import get_net_at_point

        schematic_path = params.get("schematicPath")
        if not schematic_path:
            return {"success": False, "message": "Missing required parameter: schematicPath"}

        x = params.get("x")
        y = params.get("y")
        if x is None or y is None:
            return {"success": False, "message": "Missing required parameters: x and y"}

        try:
            x, y = float(x), float(y)
        except (TypeError, ValueError):
            return {"success": False, "message": "Parameters x and y must be numeric"}

        schematic = SchematicManager.load_schematic(schematic_path)
        if not schematic:
            return {"success": False, "message": "Failed to load schematic"}

        result = get_net_at_point(schematic, schematic_path, x, y)
        return {"success": True, **result}

    except Exception as e:
        logger.error(f"Error getting net at point: {str(e)}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_get_wire_connections(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Find net name and all component pins reachable from a point or component pin."""
    logger.info("Getting wire connections")
    try:
        from pathlib import Path

        from commands.pin_locator import PinLocator
        from commands.wire_connectivity import get_wire_connections

        schematic_path = params.get("schematicPath")
        if not schematic_path:
            return {"success": False, "message": "Missing required parameter: schematicPath"}

        reference = params.get("reference")
        pin = params.get("pin")
        x = params.get("x")
        y = params.get("y")

        has_ref_pin = reference is not None and pin is not None
        has_coords = x is not None and y is not None

        if has_ref_pin and has_coords:
            return {
                "success": False,
                "message": "Supply either {reference, pin} or {x, y}, not both",
            }

        if not has_ref_pin and not has_coords:
            if reference is not None or pin is not None:
                return {
                    "success": False,
                    "message": "Both reference and pin are required together",
                }
            return {
                "success": False,
                "message": "Must supply either {reference, pin} or {x, y}",
            }

        if has_ref_pin:
            location = PinLocator().get_pin_location(Path(schematic_path), reference, str(pin))
            if location is None:
                return {
                    "success": False,
                    "message": f"Pin {pin} not found on {reference}",
                }
            x, y = location[0], location[1]
        else:
            try:
                x, y = float(x), float(y)
            except (TypeError, ValueError):
                return {"success": False, "message": "Parameters x and y must be numeric"}

        schematic = SchematicManager.load_schematic(schematic_path)
        if not schematic:
            return {"success": False, "message": "Failed to load schematic"}

        if not hasattr(schematic, "wire"):
            return {"success": False, "message": "Schematic has no wires"}

        result = get_wire_connections(schematic, schematic_path, x, y)
        if result is None:
            return {
                "success": False,
                "message": f"No wire found at ({x},{y}) — point may not be connected",
            }

        return {"success": True, **result}

    except Exception as e:
        logger.error(f"Error getting wire connections: {str(e)}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_get_net_connections(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Get all connections for a named net"""
    logger.info("Getting net connections")
    try:
        from commands.wire_connectivity import get_connections_for_net

        schematic_path = params.get("schematicPath")
        net_name = params.get("netName")

        if not all([schematic_path, net_name]):
            return {"success": False, "message": "Missing required parameters"}

        schematic = SchematicManager.load_schematic(schematic_path)
        if not schematic:
            return {"success": False, "message": "Failed to load schematic"}

        connections = get_connections_for_net(schematic, schematic_path, net_name)
        return {"success": True, "connections": connections}
    except Exception as e:
        logger.error(f"Error getting net connections: {str(e)}")
        return {"success": False, "message": str(e)}


def handle_list_schematic_labels(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """List all net labels and power flags in a schematic"""
    logger.info("Listing schematic labels")
    try:
        schematic_path = params.get("schematicPath")
        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}

        net_name = params.get("netName")
        label_type = params.get("labelType")

        _valid_label_types = {"net", "global", "power"}
        if label_type is not None and label_type not in _valid_label_types:
            return {"success": False, "message": "labelType must be one of: net, global, power"}

        schematic = SchematicManager.load_schematic(schematic_path)
        if not schematic:
            return {"success": False, "message": "Failed to load schematic"}

        labels = []

        # Regular labels
        if hasattr(schematic, "label"):
            for label in schematic.label:
                if hasattr(label, "value"):
                    pos = (
                        label.at.value
                        if hasattr(label, "at") and hasattr(label.at, "value")
                        else [0, 0]
                    )
                    labels.append(
                        {
                            "name": label.value,
                            "type": "net",
                            "position": {"x": float(pos[0]), "y": float(pos[1])},
                        }
                    )

        # Global labels
        if hasattr(schematic, "global_label"):
            for label in schematic.global_label:
                if hasattr(label, "value"):
                    pos = (
                        label.at.value
                        if hasattr(label, "at") and hasattr(label.at, "value")
                        else [0, 0]
                    )
                    labels.append(
                        {
                            "name": label.value,
                            "type": "global",
                            "position": {"x": float(pos[0]), "y": float(pos[1])},
                        }
                    )

        # Power symbols (components with power flag)
        if hasattr(schematic, "symbol"):
            for symbol in schematic.symbol:
                if not hasattr(symbol.property, "Reference"):
                    continue
                ref = symbol.property.Reference.value
                if ref.startswith("_TEMPLATE"):
                    continue
                if not ref.startswith("#PWR"):
                    continue
                value = symbol.property.Value.value if hasattr(symbol.property, "Value") else ref
                pos = symbol.at.value if hasattr(symbol, "at") else [0, 0, 0]
                labels.append(
                    {
                        "name": value,
                        "type": "power",
                        "position": {"x": float(pos[0]), "y": float(pos[1])},
                    }
                )

        # Apply filters
        if net_name is not None:
            labels = [lbl for lbl in labels if lbl["name"] == net_name]
        if label_type is not None:
            labels = [lbl for lbl in labels if lbl["type"] == label_type]

        labels, page = paginate(labels, params)
        return {"success": True, "labels": labels, **page}

    except Exception as e:
        logger.error(f"Error listing schematic labels: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_list_schematic_wires(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """List all wires in a schematic"""
    logger.info("Listing schematic wires")
    try:
        schematic_path = params.get("schematicPath")
        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}

        schematic = SchematicManager.load_schematic(schematic_path)
        if not schematic:
            return {"success": False, "message": "Failed to load schematic"}

        wires = []
        if hasattr(schematic, "wire"):
            for wire in schematic.wire:
                if hasattr(wire, "pts") and hasattr(wire.pts, "xy"):
                    points = []
                    for point in wire.pts.xy:
                        if hasattr(point, "value"):
                            points.append(
                                {
                                    "x": float(point.value[0]),
                                    "y": float(point.value[1]),
                                }
                            )

                    if len(points) >= 2:
                        wires.append(
                            {
                                "start": points[0],
                                "end": points[-1],
                            }
                        )

        wires, page = paginate(wires, params)
        return {"success": True, "wires": wires, **page}

    except Exception as e:
        logger.error(f"Error listing schematic wires: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_list_schematic_nets(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """List all nets in a schematic with their connections"""
    logger.info("Listing schematic nets")
    try:
        from commands.wire_connectivity import (
            _build_adjacency,
            _discover_sub_sheets,
            _load_sexp,
            _parse_labels_sexp,
            _parse_virtual_connections,
            _parse_wires,
            count_pins_on_net,
            get_connections_for_net,
        )

        schematic_path = params.get("schematicPath")
        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}

        schematic = SchematicManager.load_schematic(schematic_path)
        if not schematic:
            return {"success": False, "message": "Failed to load schematic"}

        # Collect net names from the top-level sheet using sexpdata.
        # Falls back to kicad-skip's label collections when the file
        # cannot be read (e.g. mocked schematics in unit tests).
        net_names: set = set()
        sexp_loaded = False
        try:
            sexp = _load_sexp(schematic_path)
            sexp_loaded = True
            _, label_to_points = _parse_labels_sexp(sexp)
            net_names.update(label_to_points.keys())
        except Exception as e:
            logger.debug(
                f"Could not parse labels from {schematic_path} via sexp ({e}); "
                "falling back to kicad-skip label collections"
            )
            for attr in ("label", "global_label"):
                if not hasattr(schematic, attr):
                    continue
                for label in getattr(schematic, attr):
                    if hasattr(label, "value"):
                        net_names.add(label.value)

        # Collect net names from all sub-sheets (only when the parent
        # sheet was readable; fake/mock paths skip recursion entirely).
        if sexp_loaded:
            sub_sheets = _discover_sub_sheets(schematic_path)
            for sub_path in sub_sheets:
                try:
                    sub_sexp = _load_sexp(sub_path)
                    _, sub_label_to_points = _parse_labels_sexp(sub_sexp)
                    net_names.update(sub_label_to_points.keys())
                except Exception as e:
                    logger.warning(f"Error reading sub-sheet {sub_path}: {e}")

        # Pre-build shared wire graph structures for efficiency
        all_wires = _parse_wires(schematic)
        if all_wires:
            adjacency, iu_to_wires = _build_adjacency(all_wires)
        else:
            adjacency, iu_to_wires = [], {}
        point_to_label, label_to_points = _parse_virtual_connections(schematic, schematic_path)

        # Parse + index each sheet once and reuse across every net, instead of
        # rebuilding the O(wires^2) adjacency graph per net (the old behaviour
        # made a large schematic's overview time out).
        sheet_contexts: Dict[Any, Any] = {}
        nets = []
        for net_name in sorted(net_names):
            connections = get_connections_for_net(
                schematic, schematic_path, net_name, sheet_contexts=sheet_contexts
            )
            pin_count = count_pins_on_net(
                schematic,
                schematic_path,
                net_name,
                all_wires,
                iu_to_wires,
                adjacency,
                point_to_label,
                label_to_points,
            )
            nets.append(
                {
                    "name": net_name,
                    "connections": connections,
                    "connected_pin_count": pin_count,
                }
            )

        nets, page = paginate(nets, params)
        return {"success": True, "nets": nets, **page}

    except Exception as e:
        logger.error(f"Error listing schematic nets: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_list_schematic_components(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """List all components in a schematic"""
    logger.info("Listing schematic components")
    try:
        from pathlib import Path

        from commands.pin_locator import PinLocator

        schematic_path = params.get("schematicPath")
        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}

        sch_file = Path(schematic_path)
        if not sch_file.exists():
            return {
                "success": False,
                "message": f"Schematic not found: {schematic_path}",
            }

        schematic = SchematicManager.load_schematic(schematic_path)
        if not schematic:
            # skip choked on the file (typical when lib_symbols contains a
            # symbol with `(extends ...)` or an empty property name — the
            # _base_coords AttributeError from MCP_FEEDBACK A3).  Fall back
            # to a sexpdata walk so the caller at least sees the
            # component list; pin enrichment is skipped on this path.
            return _list_schematic_components_raw_fallback(sch_file, params)

        # Optional filters
        filter_params = params.get("filter", {})
        lib_id_filter = filter_params.get("libId", "")
        ref_prefix_filter = filter_params.get("referencePrefix", "")

        locator = PinLocator()
        components = []
        skip_failures = 0

        try:
            symbol_iter = list(schematic.symbol)
        except (AttributeError, KeyError, TypeError) as e:
            # skip's iterator itself blew up (e.g. lib_symbols contained an
            # extends-symbol with no parent in scope, producing the
            # `_base_coords` AttributeError reported in MCP_FEEDBACK A3).
            # Fall back to the raw sexpdata parser.
            logger.warning(
                "skip iterator failed for %s (%s); using raw fallback",
                schematic_path,
                e,
            )
            return _list_schematic_components_raw_fallback(sch_file, params)

        for symbol in symbol_iter:
            try:
                if not hasattr(symbol.property, "Reference"):
                    continue
                ref = symbol.property.Reference.value
                # Skip template symbols
                if ref.startswith("_TEMPLATE"):
                    continue

                lib_id = symbol.lib_id.value if hasattr(symbol, "lib_id") else ""

                # Apply filters
                if lib_id_filter and lib_id_filter not in lib_id:
                    continue
                if ref_prefix_filter and not ref.startswith(ref_prefix_filter):
                    continue

                value = symbol.property.Value.value if hasattr(symbol.property, "Value") else ""
                footprint = (
                    symbol.property.Footprint.value if hasattr(symbol.property, "Footprint") else ""
                )
                position = symbol.at.value if hasattr(symbol, "at") else [0, 0, 0]
                uuid_val = symbol.uuid.value if hasattr(symbol, "uuid") else ""

                comp = {
                    "reference": ref,
                    "libId": lib_id,
                    "value": value,
                    "footprint": footprint,
                    "position": {"x": float(position[0]), "y": float(position[1])},
                    "rotation": float(position[2]) if len(position) > 2 else 0,
                    "uuid": str(uuid_val),
                }

                # Get pins if available
                try:
                    all_pins = locator.get_all_symbol_pins(sch_file, ref)
                    if all_pins:
                        pins_def = locator.get_symbol_pins(sch_file, lib_id) or {}
                        pin_list = []
                        for pin_num, coords in all_pins.items():
                            pin_info = {
                                "number": pin_num,
                                "position": {"x": coords[0], "y": coords[1]},
                            }
                            if pin_num in pins_def:
                                pin_info["name"] = pins_def[pin_num].get("name", pin_num)
                            pin_list.append(pin_info)
                        comp["pins"] = pin_list
                except Exception:
                    pass  # Pin lookup is best-effort

                components.append(comp)
            except (AttributeError, KeyError, TypeError) as e:
                # One bad symbol in lib_symbols shouldn't take out the
                # whole list.  Count failures so the caller knows the
                # result is partial.
                skip_failures += 1
                logger.warning("Skipping unparseable symbol in %s: %s", schematic_path, e)
                continue

        components, page = paginate(components, params)
        result: Dict[str, Any] = {
            "success": True,
            "components": components,
            **page,
        }
        if skip_failures:
            result["partial"] = True
            result["skippedSymbols"] = skip_failures
            result["warning"] = (
                f"{skip_failures} symbol(s) could not be parsed by kicad-skip "
                "and were skipped; the list may be incomplete."
            )
        return result

    except Exception as e:
        logger.error(f"Error listing schematic components: {e}")
        import traceback

        logger.error(traceback.format_exc())
        # Last-ditch fallback: even when skip threw something unexpected,
        # try the raw parser before giving up.
        try:
            return _list_schematic_components_raw_fallback(
                Path(params["schematicPath"]),
                params,
            )
        except Exception:  # noqa: BLE001 — the raw parser failed too
            return {"success": False, "message": str(e)}


def _list_schematic_components_raw_fallback(
    sch_file: "Path", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Degraded path used when kicad-skip can't load the schematic.

    Reads top-level (symbol ...) instances directly with sexpdata.
    Returns the same shape as the happy path minus pin enrichment, plus
    a `parser: "raw_fallback"` marker so callers can spot the
    degradation.
    """
    from commands.schematic_raw_parser import parse_components

    filter_params = params.get("filter") or {}

    try:
        components = parse_components(str(sch_file))
    except (OSError, ValueError) as e:
        logger.error("Raw schematic parse failed for %s: %s", sch_file, e)
        return {
            "success": False,
            "message": f"Failed to load schematic (skip + raw parser both failed): {e}",
        }

    lib_id_filter = filter_params.get("libId", "")
    ref_prefix_filter = filter_params.get("referencePrefix", "")
    if lib_id_filter:
        components = [c for c in components if lib_id_filter in c.get("libId", "")]
    if ref_prefix_filter:
        components = [c for c in components if c.get("reference", "").startswith(ref_prefix_filter)]

    components, page = paginate(components, params)
    return {
        "success": True,
        "components": components,
        **page,
        "partial": True,
        "parser": "raw_fallback",
        "warning": (
            "kicad-skip could not load this schematic (often due to a stock "
            "symbol with `(extends ...)` or an empty property name in "
            "lib_symbols); component list was extracted by direct "
            "S-expression parsing and does not include pin coordinates. "
            "Use get_schematic_pin_locations for pin data on a per-component "
            "basis if needed."
        ),
    }


def handle_get_schematic_pin_locations(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Return exact pin endpoint coordinates for a schematic component"""
    logger.info("Getting schematic pin locations")
    try:
        from pathlib import Path

        from commands.pin_locator import PinLocator

        schematic_path = params.get("schematicPath")
        reference = params.get("reference")

        if not all([schematic_path, reference]):
            return {
                "success": False,
                "message": "Missing required parameters: schematicPath, reference",
            }

        locator = PinLocator()
        all_pins = locator.get_all_symbol_pins(Path(schematic_path), reference)

        if not all_pins:
            return {
                "success": False,
                "message": f"No pins found for {reference} — check reference and schematic path",
            }

        # Enrich with pin names and angles from the symbol definition
        pins_def = (
            locator.get_symbol_pins(
                Path(schematic_path),
                locator._get_lib_id(Path(schematic_path), reference),
            )
            if hasattr(locator, "_get_lib_id")
            else {}
        )

        result = {}
        for pin_num, coords in all_pins.items():
            entry = {"x": coords[0], "y": coords[1]}
            if pin_num in pins_def:
                entry["name"] = pins_def[pin_num].get("name", pin_num)
                entry["angle"] = (
                    locator.get_pin_angle(Path(schematic_path), reference, pin_num) or 0
                )
                # Which symbol unit owns this pin. For multi-unit parts (op-amp,
                # gate array) different pins live on different units, each placed
                # at its own location — callers shorting nets by pin number alone
                # need this to tell unit A's pins from unit B's.
                if pins_def[pin_num].get("unit") is not None:
                    entry["unit"] = pins_def[pin_num]["unit"]
            result[pin_num] = entry

        return {"success": True, "reference": reference, "pins": result}

    except Exception as e:
        logger.error(f"Error getting pin locations: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_check_wire_collisions(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Detect wires passing through component bodies without connecting to pins"""
    logger.info("Checking wire collisions")
    try:
        from commands.schematic_analysis import check_wire_collisions

        schematic_path = params.get("schematicPath")
        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}
        result = check_wire_collisions(schematic_path)
        return {"success": True, **result}
    except ImportError:
        return {
            "success": False,
            "message": "schematic_analysis module not available",
        }
    except Exception as e:
        logger.error(f"Error checking wire collisions: {e}")
        return {"success": False, "message": str(e)}


def handle_find_unconnected_pins(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """List component pins with no wire/label/power symbol touching them"""
    logger.info("Finding unconnected pins")
    try:
        from commands.schematic_analysis import find_unconnected_pins

        schematic_path = params.get("schematicPath")
        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}
        result = find_unconnected_pins(schematic_path)
        return {"success": True, **result}
    except ImportError:
        return {
            "success": False,
            "message": "schematic_analysis module not available",
        }
    except Exception as e:
        logger.error(f"Error finding unconnected pins: {e}")
        return {"success": False, "message": str(e)}


def handle_list_schematic_libraries(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """List available symbol libraries"""
    logger.info("Listing schematic libraries")
    try:
        search_paths = params.get("searchPaths")

        libraries = SchematicLibraryManager.list_available_libraries(search_paths)
        return {"success": True, "libraries": libraries}
    except Exception as e:
        logger.error(f"Error listing schematic libraries: {str(e)}")
        return {"success": False, "message": str(e)}
