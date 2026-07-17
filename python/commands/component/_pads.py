"""Pad commands: get_component_pads, get_pad_position, edit_component_pad.

Split out of the former monolithic commands/component.py.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

import pcbnew
from utils.responses import failed, no_board_loaded
from utils.units import unit_to_nm_scale

logger = logging.getLogger("kicad_interface")


def _parse_size_mm(value: Any) -> Optional[Tuple[float, float]]:
    """Parse a size/drill param into an (x, y) pair in the caller's unit.

    Accepts a bare number (round/square), ``{"x", "y"}`` (the shape
    get_component_pads reports) or ``{"w", "h"}`` (the shape
    edit_footprint_pad accepts) — returns None if unparseable.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (float(value), float(value))
    if isinstance(value, dict):
        x = value.get("x", value.get("w"))
        y = value.get("y", value.get("h"))
        if x is None and y is None:
            return None
        if x is None:
            x = y
        if y is None:
            y = x
        return (float(x), float(y))
    return None


class PadsMixin:
    def get_component_pads(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get all pads for a component with their positions and net connections"""
        try:
            if not self.board:
                return no_board_loaded()

            reference = params.get("reference")
            if not reference:
                return {
                    "success": False,
                    "message": "Missing reference",
                    "errorDetails": "reference parameter is required",
                }

            from utils.units import nm_to_unit, normalize_unit

            unit = normalize_unit(params.get("unit", "mm"))

            module = self.board.FindFootprintByReference(reference)
            if not module:
                return {
                    "success": False,
                    "message": "Component not found",
                    "errorDetails": f"Could not find component: {reference}",
                }

            pads = []
            for pad in module.Pads():
                pos = pad.GetPosition()
                size = pad.GetSize()

                shape_map = {
                    pcbnew.PAD_SHAPE_CIRCLE: "circle",
                    pcbnew.PAD_SHAPE_RECT: "rect",
                    pcbnew.PAD_SHAPE_OVAL: "oval",
                    pcbnew.PAD_SHAPE_TRAPEZOID: "trapezoid",
                    pcbnew.PAD_SHAPE_ROUNDRECT: "roundrect",
                    pcbnew.PAD_SHAPE_CHAMFERED_RECT: "chamfered_rect",
                    pcbnew.PAD_SHAPE_CUSTOM: "custom",
                }
                shape = shape_map.get(pad.GetShape(), "unknown")

                type_map = {
                    pcbnew.PAD_ATTRIB_PTH: "through_hole",
                    pcbnew.PAD_ATTRIB_SMD: "smd",
                    pcbnew.PAD_ATTRIB_CONN: "connector",
                    pcbnew.PAD_ATTRIB_NPTH: "npth",
                }
                pad_type = type_map.get(pad.GetAttribute(), "unknown")

                pads.append(
                    {
                        "name": pad.GetName(),
                        "number": pad.GetNumber(),
                        "position": {
                            "x": nm_to_unit(pos.x, unit),
                            "y": nm_to_unit(pos.y, unit),
                            "unit": unit,
                        },
                        "net": pad.GetNetname(),
                        "netCode": pad.GetNetCode(),
                        "shape": shape,
                        "type": pad_type,
                        "size": {
                            "x": nm_to_unit(size.x, unit),
                            "y": nm_to_unit(size.y, unit),
                            "unit": unit,
                        },
                        "drillSize": (
                            nm_to_unit(pad.GetDrillSize().x, unit)
                            if pad.GetDrillSize().x > 0
                            else None
                        ),
                    }
                )

            comp_pos = module.GetPosition()

            return {
                "success": True,
                "reference": reference,
                "componentPosition": {
                    "x": nm_to_unit(comp_pos.x, unit),
                    "y": nm_to_unit(comp_pos.y, unit),
                    "unit": unit,
                },
                "padCount": len(pads),
                "pads": pads,
            }

        except Exception as e:
            logger.error(f"Error getting component pads: {str(e)}")
            return failed("Failed to get component pads", e)

    def get_pad_position(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get the position of a specific pad on a component"""
        try:
            if not self.board:
                return no_board_loaded()

            reference = params.get("reference")
            # The TS schema names this argument ``pad`` (and that's what
            # MCP clients send); the SWIG handler originally read
            # ``padName`` / ``padNumber``.  Accept all three so the
            # documented name works and legacy callers don't break.
            pad_name = params.get("pad") or params.get("padName") or params.get("padNumber")

            if not reference:
                return {
                    "success": False,
                    "message": "Missing reference",
                    "errorDetails": "reference parameter is required",
                }
            if not pad_name:
                return {
                    "success": False,
                    "message": "Missing pad identifier",
                    "errorDetails": "pad (or padName / padNumber) parameter is required",
                }

            module = self.board.FindFootprintByReference(reference)
            if not module:
                return {
                    "success": False,
                    "message": "Component not found",
                    "errorDetails": f"Could not find component: {reference}",
                }

            pad = module.FindPadByNumber(str(pad_name))
            if not pad:
                available_pads = [p.GetNumber() for p in module.Pads()]
                return {
                    "success": False,
                    "message": "Pad not found",
                    "errorDetails": f"Pad '{pad_name}' not found on {reference}. Available pads: {', '.join(available_pads)}",
                }

            pos = pad.GetPosition()
            size = pad.GetSize()

            return {
                "success": True,
                "reference": reference,
                "padName": pad.GetNumber(),
                "position": {"x": pos.x / 1000000, "y": pos.y / 1000000, "unit": "mm"},
                "net": pad.GetNetname(),
                "netCode": pad.GetNetCode(),
                "size": {"x": size.x / 1000000, "y": size.y / 1000000, "unit": "mm"},
            }

        except Exception as e:
            logger.error(f"Error getting pad position: {str(e)}")
            return failed("Failed to get pad position", e)

    # ------------------------------------------------------------------
    # edit_component_pad — repair pads on a PLACED footprint.
    #
    # Motivation (GD32 E2E): easyeda footprint BAT-SMD_CR1220-2 ships two
    # thru_hole pads with EMPTY pad numbers and copper diameter == drill
    # diameter -> two unfixable annular_width DRC errors.  The lib-file
    # editor (edit_footprint_pad) can't touch a placed instance and can't
    # target pads without numbers; this command edits the board copy via
    # SWIG and targets pads by number OR zero-based index.
    # ------------------------------------------------------------------
    def edit_component_pad(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Edit pads of a placed footprint (size / drill / shape / number / type).

        Targeting: ``padNumber`` (may be "" for unnumbered pads) or zero-based
        ``padIndex`` (iteration order, matches get_component_pads), optionally
        filtered by ``padType``.  Multiple matches are refused with the
        candidate list unless ``all: true``.  A resulting copper size <= drill
        (zero/negative annular ring) on a plated pad is refused unless
        ``force: true``.
        """
        try:
            if not self.board:
                return no_board_loaded()

            reference = params.get("reference")
            if not reference:
                return {
                    "success": False,
                    "message": "Missing reference",
                    "errorDetails": "reference parameter is required",
                }

            module = self.board.FindFootprintByReference(reference)
            if not module:
                return {
                    "success": False,
                    "message": "Component not found",
                    "errorDetails": f"Could not find component: {reference}",
                }

            from utils.units import nm_to_unit, normalize_unit

            unit = normalize_unit(params.get("unit", "mm"))
            unit_scale = unit_to_nm_scale(unit)

            pad_number = params.get("padNumber")
            pad_index = params.get("padIndex")
            pad_type_filter = params.get("padType")
            edit_all = bool(params.get("all", False))
            force = bool(params.get("force", False))

            new_size = _parse_size_mm(params.get("size"))
            new_drill = _parse_size_mm(params.get("drill"))
            new_shape_name = params.get("shape")
            new_number = params.get("newPadNumber")
            new_type_name = params.get("newPadType")

            if (
                new_size is None
                and new_drill is None
                and new_shape_name is None
                and new_number is None
                and new_type_name is None
            ):
                return {
                    "success": False,
                    "message": "Nothing to edit",
                    "errorDetails": (
                        "Pass at least one of size, drill, shape, " "newPadNumber, newPadType"
                    ),
                }

            # ---- name/const maps (built per call: stub-friendly) ----
            shape_to_name = {
                pcbnew.PAD_SHAPE_CIRCLE: "circle",
                pcbnew.PAD_SHAPE_RECT: "rect",
                pcbnew.PAD_SHAPE_OVAL: "oval",
                pcbnew.PAD_SHAPE_TRAPEZOID: "trapezoid",
                pcbnew.PAD_SHAPE_ROUNDRECT: "roundrect",
                pcbnew.PAD_SHAPE_CHAMFERED_RECT: "chamfered_rect",
                pcbnew.PAD_SHAPE_CUSTOM: "custom",
            }
            name_to_shape = {v: k for k, v in shape_to_name.items()}
            attrib_to_name = {
                pcbnew.PAD_ATTRIB_PTH: "through_hole",
                pcbnew.PAD_ATTRIB_SMD: "smd",
                pcbnew.PAD_ATTRIB_CONN: "connector",
                pcbnew.PAD_ATTRIB_NPTH: "npth",
            }
            name_to_attrib = {v: k for k, v in attrib_to_name.items()}
            # Accept the aliases the footprint-file tools use.
            name_to_attrib["thru_hole"] = pcbnew.PAD_ATTRIB_PTH
            name_to_attrib["np_thru_hole"] = pcbnew.PAD_ATTRIB_NPTH

            if new_shape_name is not None and new_shape_name not in name_to_shape:
                return {
                    "success": False,
                    "message": f"Unknown pad shape '{new_shape_name}'",
                    "errorDetails": f"Valid shapes: {sorted(name_to_shape)}",
                }
            if new_type_name is not None and new_type_name not in name_to_attrib:
                return {
                    "success": False,
                    "message": f"Unknown pad type '{new_type_name}'",
                    "errorDetails": f"Valid types: {sorted(name_to_attrib)}",
                }
            _valid_type_filters = set(name_to_attrib)
            if pad_type_filter is not None and pad_type_filter not in _valid_type_filters:
                return {
                    "success": False,
                    "message": f"Unknown padType filter '{pad_type_filter}'",
                    "errorDetails": f"Valid types: {sorted(_valid_type_filters)}",
                }

            def _pad_type_name(pad: Any) -> str:
                return attrib_to_name.get(pad.GetAttribute(), "unknown")

            def _type_matches(pad: Any, wanted: str) -> bool:
                wanted_attrib = name_to_attrib[wanted]
                return pad.GetAttribute() == wanted_attrib

            def _describe(pad: Any, index: int) -> Dict[str, Any]:
                size = pad.GetSize()
                drill = pad.GetDrillSize()
                pos = pad.GetPosition()
                return {
                    "index": index,
                    "number": pad.GetNumber(),
                    "type": _pad_type_name(pad),
                    "shape": shape_to_name.get(pad.GetShape(), "unknown"),
                    "size": {
                        "x": nm_to_unit(size.x, unit),
                        "y": nm_to_unit(size.y, unit),
                        "unit": unit,
                    },
                    "drill": (
                        {
                            "x": nm_to_unit(drill.x, unit),
                            "y": nm_to_unit(drill.y, unit),
                            "unit": unit,
                        }
                        if drill.x > 0 or drill.y > 0
                        else None
                    ),
                    "position": {
                        "x": nm_to_unit(pos.x, unit),
                        "y": nm_to_unit(pos.y, unit),
                        "unit": unit,
                    },
                }

            # ---- select target pads ----
            # NOTE: "" is a legitimate padNumber (the BT1 failure mode), so
            # test for None, not truthiness.
            pads = list(module.Pads())
            if pad_index is not None:
                try:
                    idx = int(pad_index)
                except (TypeError, ValueError):
                    return {
                        "success": False,
                        "message": "Invalid padIndex",
                        "errorDetails": "padIndex must be a zero-based integer",
                    }
                if idx < 0 or idx >= len(pads):
                    return {
                        "success": False,
                        "message": f"padIndex {idx} out of range",
                        "errorDetails": (
                            f"{reference} has {len(pads)} pad(s); "
                            f"valid indices are 0..{len(pads) - 1}"
                        ),
                    }
                targets = [(idx, pads[idx])]
            elif pad_number is not None:
                wanted = str(pad_number)
                targets = [(i, p) for i, p in enumerate(pads) if str(p.GetNumber()) == wanted]
            elif pad_type_filter is not None:
                targets = list(enumerate(pads))
            else:
                return {
                    "success": False,
                    "message": "Missing pad selector",
                    "errorDetails": (
                        "Pass padNumber (may be ''), padIndex, or a padType filter. "
                        f"{reference} pads: "
                        + ", ".join(f"[{i}]={p.GetNumber()!r}" for i, p in enumerate(pads))
                    ),
                }

            if pad_type_filter is not None:
                targets = [(i, p) for i, p in targets if _type_matches(p, pad_type_filter)]

            if not targets:
                return {
                    "success": False,
                    "message": "No matching pads",
                    "errorDetails": (
                        f"No pad on {reference} matches the given selector. Pads: "
                        + ", ".join(
                            f"[{i}]={p.GetNumber()!r}({_pad_type_name(p)})"
                            for i, p in enumerate(pads)
                        )
                    ),
                }

            if len(targets) > 1 and not edit_all:
                return {
                    "success": False,
                    "message": (
                        f"{len(targets)} pads match — pass all=true to edit every "
                        "match, or target one with padIndex"
                    ),
                    "candidates": [_describe(p, i) for i, p in targets],
                }

            # ---- annular-ring guard (compute resulting geometry first) ----
            # Only geometry/type edits are guarded: a number- or shape-only
            # edit must not be blocked by PRE-EXISTING copper==drill (fixing
            # the numbers first is a natural repair order for the BT1 case).
            geometry_edited = (
                new_size is not None or new_drill is not None or new_type_name is not None
            )
            plated_attribs = {pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_CONN}
            annular_violations: List[Dict[str, Any]] = []
            for i, pad in targets if geometry_edited else []:
                cur_size = pad.GetSize()
                cur_drill = pad.GetDrillSize()
                res_size_x = new_size[0] * unit_scale if new_size else cur_size.x
                res_size_y = new_size[1] * unit_scale if new_size else cur_size.y
                res_drill_x = new_drill[0] * unit_scale if new_drill else cur_drill.x
                res_drill_y = new_drill[1] * unit_scale if new_drill else cur_drill.y
                res_attrib = name_to_attrib[new_type_name] if new_type_name else pad.GetAttribute()
                # NPTH has no copper — copper==drill is its normal state.
                if res_attrib not in plated_attribs:
                    continue
                if res_drill_x <= 0 and res_drill_y <= 0:
                    continue  # no hole -> no annular constraint
                annular_nm = min(res_size_x - res_drill_x, res_size_y - res_drill_y) / 2.0
                if annular_nm <= 0:
                    annular_violations.append(
                        {
                            "index": i,
                            "number": pad.GetNumber(),
                            "annular_mm": round(annular_nm / 1_000_000.0, 4),
                        }
                    )
            if annular_violations and not force:
                return {
                    "success": False,
                    "message": (
                        "Refused: resulting annular ring would be zero or negative "
                        "(copper size <= drill) — the exact defect this tool exists "
                        "to repair"
                    ),
                    "needs_force": True,
                    "violations": annular_violations,
                    "errorDetails": (
                        "Increase size or reduce drill so copper > drill, "
                        "or pass force=true to write it anyway"
                    ),
                }

            # ---- apply ----
            edited: List[Dict[str, Any]] = []
            for i, pad in targets:
                before = _describe(pad, i)
                changes: List[str] = []
                if new_size is not None:
                    pad.SetSize(
                        pcbnew.VECTOR2I(
                            int(round(new_size[0] * unit_scale)),
                            int(round(new_size[1] * unit_scale)),
                        )
                    )
                    changes.append("size")
                if new_drill is not None:
                    pad.SetDrillSize(
                        pcbnew.VECTOR2I(
                            int(round(new_drill[0] * unit_scale)),
                            int(round(new_drill[1] * unit_scale)),
                        )
                    )
                    changes.append("drill")
                if new_shape_name is not None:
                    pad.SetShape(name_to_shape[new_shape_name])
                    changes.append("shape")
                if new_number is not None:
                    pad.SetNumber(str(new_number))
                    changes.append("number")
                if new_type_name is not None:
                    pad.SetAttribute(name_to_attrib[new_type_name])
                    changes.append("type")
                after = _describe(pad, i)
                edited.append({"index": i, "changes": changes, "before": before, "after": after})

            logger.info(
                f"edit_component_pad: {reference} — edited {len(edited)} pad(s): "
                + ", ".join(f"[{e['index']}]{e['changes']}" for e in edited)
            )
            result: Dict[str, Any] = {
                "success": True,
                "message": f"Edited {len(edited)} pad(s) on {reference}",
                "reference": reference,
                "matched": len(edited),
                "pads": edited,
            }
            if annular_violations:
                result["warning"] = (
                    "force=true wrote pad(s) with zero/negative annular ring: "
                    + ", ".join(f"[{v['index']}]" for v in annular_violations)
                )
            return result

        except Exception as e:
            logger.error(f"Error editing component pad: {str(e)}")
            return failed("Failed to edit component pad", e)
