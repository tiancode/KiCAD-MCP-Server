"""Place / move / rotate / delete / duplicate / edit single-component commands.

Split out of the former monolithic commands/component.py.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Set

import pcbnew
from utils.responses import failed, no_board_loaded
from utils.units import unit_to_nm_scale

from ._shared import resolve_footprint

logger = logging.getLogger("kicad_interface")


# A coordinate more than this many board-widths/heights away can only be a
# units mistake (e.g. x=5000 mm on a 90 mm board); reject rather than fling the
# footprint 5 m off the board.  Mirrors the schematic-side page guard's
# _OFF_PAGE_ABSURD_FACTOR (POSITION_OFF_SHEET) — here the analog is the board
# outline and POSITION_OFF_BOARD.
_OFF_BOARD_ABSURD_FACTOR = 10.0


def classify_board_position(x_mm: float, y_mm: float, bbox: Optional[tuple]) -> str:
    """Classify a point (mm) against the board outline bbox.

    ``bbox`` is ``(left, top, right, bottom)`` in mm, or ``None`` when the board
    has no Edge.Cuts outline.  Returns:

      * ``"no_outline"`` — no (or degenerate) outline: can't judge, don't warn.
      * ``"absurd"``     — >10× a board dimension away: reject (units error).
      * ``"off_board"``  — outside the outline but plausibly intentional: warn.
      * ``"ok"``         — inside the outline.
    """
    if bbox is None:
        return "no_outline"
    left, top, right, bottom = bbox
    width = abs(right - left)
    height = abs(bottom - top)
    if width <= 0 or height <= 0:
        return "no_outline"
    if (
        abs(x_mm) > _OFF_BOARD_ABSURD_FACTOR * width
        or abs(y_mm) > _OFF_BOARD_ABSURD_FACTOR * height
    ):
        return "absurd"
    if not (left <= x_mm <= right and top <= y_mm <= bottom):
        return "off_board"
    return "ok"


def _outline_bbox_mm(board: Any) -> Optional[tuple]:
    """Return the board's Edge.Cuts bounding box as ``(left, top, right, bottom)``
    mm, or ``None`` when there is no usable outline.

    ``GetBoardEdgesBoundingBox`` returns a BOX2I in nm; an empty/degenerate box
    (no outline) collapses to zero width/height, which the caller treats as
    "can't judge".
    """
    try:
        bb = board.GetBoardEdgesBoundingBox()
        left = bb.GetLeft() / 1000000.0
        top = bb.GetTop() / 1000000.0
        right = bb.GetRight() / 1000000.0
        bottom = bb.GetBottom() / 1000000.0
    except (AttributeError, RuntimeError):
        return None
    if right - left <= 0 or bottom - top <= 0:
        return None
    return (left, top, right, bottom)


def _parse_ref(ref: str) -> tuple:
    """Split a reference into (alpha_prefix, int_suffix). Suffix is None when
    the reference has no trailing digits (e.g. 'REF' → ('REF', None))."""
    m = re.match(r"^(.*?)(\d+)$", ref or "")
    if m:
        return m.group(1), int(m.group(2))
    return (ref or ""), None


def _allocate_duplicate_refs(
    source_ref: str, new_reference: Optional[str], count: int, used: Set[str]
) -> List[str]:
    """Pick ``count`` fresh, unused references for a duplicate.

    - explicit ``new_reference`` given: it is used verbatim for the first copy
      (raises ValueError if it already exists); further copies auto-increment
      its numeric suffix, skipping anything already used.
    - no ``new_reference``: the source reference's alpha prefix is reused and
      the numeric suffix advances past the source to the next free value(s)
      (e.g. R2 → R3, or R98 → R99 as in KiCad's own annotate-next behaviour).
    """
    used = set(used)
    refs: List[str] = []
    if new_reference:
        if new_reference in used:
            raise ValueError(f"A component with reference {new_reference} already exists")
        prefix, num = _parse_ref(new_reference)
        refs.append(new_reference)
        used.add(new_reference)
        if num is None:
            n = 2
            while len(refs) < count:
                cand = f"{new_reference}_{n}"
                n += 1
                if cand in used:
                    continue
                refs.append(cand)
                used.add(cand)
        else:
            next_num = num + 1
            while len(refs) < count:
                cand = f"{prefix}{next_num}"
                next_num += 1
                if cand in used:
                    continue
                refs.append(cand)
                used.add(cand)
    else:
        prefix, num = _parse_ref(source_ref)
        next_num = (num if num is not None else 0) + 1
        while len(refs) < count:
            cand = f"{prefix}{next_num}"
            next_num += 1
            if cand in used:
                continue
            refs.append(cand)
            used.add(cand)
    return refs


class PlacementMixin:
    def place_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Place a component on the PCB"""
        try:
            if not self.board:
                return no_board_loaded()

            component_id = params.get("componentId")
            position = params.get("position")
            reference = params.get("reference")
            value = params.get("value")
            footprint = params.get("footprint")
            rotation = params.get("rotation", 0)
            layer = params.get("layer", "F.Cu")

            if not component_id or not position:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "componentId and position are required",
                }

            # Refuse to silently duplicate an existing reference — the
            # original behaviour was to add a second footprint with the
            # same designator on top of the first, which scrambled DRC
            # output and net assignment.  Callers that meant to move the
            # part should use move_component instead.
            if reference:
                existing = self.board.FindFootprintByReference(reference)
                if existing is not None:
                    return {
                        "success": False,
                        "message": (
                            f"Reference '{reference}' already exists on the board. "
                            f"Use move_component to relocate it, or pass a different "
                            f"reference to add a new footprint."
                        ),
                        "errorDetails": "place_component creates new footprints; it never overwrites.",
                        "errorCode": "DUPLICATE_REFERENCE",
                        "existingReference": reference,
                    }

            # component_id can be "Library:Footprint" or just "Footprint"
            footprint_result = self.library_manager.find_footprint(component_id)

            if not footprint_result:
                suggestions = self.library_manager.search_footprints(f"*{component_id}*", limit=5)
                suggestion_text = ""
                if suggestions:
                    suggestion_text = "\n\nDid you mean one of these?\n" + "\n".join(
                        [f"  - {s['full_name']}" for s in suggestions]
                    )

                return {
                    "success": False,
                    "message": "Footprint not found",
                    "errorDetails": f"Could not find footprint: {component_id}{suggestion_text}",
                }

            library_path, footprint_name = footprint_result

            library_nickname = None
            for nick, path in self.library_manager.libraries.items():
                if path == library_path:
                    library_nickname = nick
                    break

            if not library_nickname:
                return {
                    "success": False,
                    "message": "Internal error",
                    "errorDetails": "Could not determine library nickname",
                }

            module = pcbnew.FootprintLoad(library_path, footprint_name)
            if not module:
                return {
                    "success": False,
                    "message": "Failed to load footprint",
                    "errorDetails": f"Could not load footprint from {library_path}/{footprint_name}",
                }

            # ``unit`` is optional in the tool schema — default to mm (an
            # unrecognised non-None value raises InvalidUnitError -> VALIDATION).
            unit = position.get("unit") or "mm"
            scale = unit_to_nm_scale(unit)
            x_nm = int(position["x"] * scale)
            y_nm = int(position["y"] * scale)
            module.SetPosition(pcbnew.VECTOR2I(x_nm, y_nm))

            if reference:
                module.SetReference(reference)

            if value:
                module.SetValue(value)

            # Set footprint if provided (use existing library_nickname and footprint_name)
            # For KiCAD 9.x compatibility, use SetFPID instead of SetFootprintName
            if footprint:
                # Parse footprint string if it's in "Library:Footprint" format
                if ":" in footprint:
                    lib_name, fp_name = footprint.split(":", 1)
                else:
                    # Use the library_nickname we already have from loading
                    lib_name = library_nickname
                    fp_name = footprint
                fpid = pcbnew.LIB_ID(lib_name, fp_name)
                module.SetFPID(fpid)
            else:
                # Use the footprint we just loaded
                fpid = pcbnew.LIB_ID(library_nickname, footprint_name)
                module.SetFPID(fpid)

            # Set rotation (KiCAD 9.0 uses EDA_ANGLE)
            angle = pcbnew.EDA_ANGLE(rotation, pcbnew.DEGREES_T)
            module.SetOrientation(angle)

            # Set layer for F.Cu (or non-B.Cu) before adding to board
            if layer != "B.Cu":
                layer_id = self.board.GetLayerID(layer)
                if layer_id >= 0:
                    module.SetLayer(layer_id)

            # Add to board first — Flip() requires board context in KiCAD 9
            self.board.Add(module)

            # Flip to B.Cu after add (board context needed, otherwise hangs 30s)
            if layer == "B.Cu":
                if not module.IsFlipped():
                    module.Flip(module.GetPosition(), False)

            return {
                "success": True,
                "message": f"Placed component: {component_id}",
                "component": {
                    "reference": module.GetReference(),
                    "value": module.GetValue(),
                    "position": {"x": position["x"], "y": position["y"], "unit": unit},
                    "rotation": rotation,
                    "layer": layer,
                },
            }

        except Exception as e:
            logger.error(f"Error placing component: {str(e)}")
            return failed("Failed to place component", e)

    def move_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Move an existing component to a new position"""
        try:
            if not self.board:
                return no_board_loaded()

            reference = params.get("reference")
            position = params.get("position")
            rotation = params.get("rotation")
            layer = params.get("layer")
            allow_off_board = bool(params.get("allowOffBoard", False))

            if not reference or not position:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "reference and position are required",
                }

            module = self.board.FindFootprintByReference(reference)
            if not module:
                return {
                    "success": False,
                    "message": "Component not found",
                    "errorDetails": f"Could not find component: {reference}",
                }

            # ``unit`` is optional in the tool schema — default to mm (an
            # unrecognised non-None value raises InvalidUnitError -> VALIDATION).
            unit = position.get("unit") or "mm"
            scale = unit_to_nm_scale(unit)
            x_nm = int(position["x"] * scale)
            y_nm = int(position["y"] * scale)

            # Board-awareness (P11): when the board has an Edge.Cuts outline,
            # reject a target so far outside it that it can only be a units
            # mistake, and flag a merely-off-board (but plausible) target with a
            # warning naming the outline bbox.  No outline → can't judge, no
            # guard.  Classify BEFORE mutating so an absurd move never moves the
            # part.  Mirrors the schematic move's POSITION_OFF_SHEET guard.
            bbox = _outline_bbox_mm(self.board)
            x_mm, y_mm = x_nm / 1000000.0, y_nm / 1000000.0
            target_class = classify_board_position(x_mm, y_mm, bbox)
            board_outline = None
            if bbox is not None:
                board_outline = {
                    "x1": round(bbox[0], 4),
                    "y1": round(bbox[1], 4),
                    "x2": round(bbox[2], 4),
                    "y2": round(bbox[3], 4),
                    "unit": "mm",
                }
            if target_class == "absurd":
                return {
                    "success": False,
                    "message": (
                        f"Target position ({position['x']}, {position['y']}) "
                        f"{unit} is far outside the board outline "
                        f"(x {bbox[0]:.4g}–{bbox[2]:.4g} mm, "
                        f"y {bbox[1]:.4g}–{bbox[3]:.4g} mm) — more than "
                        f"{int(_OFF_BOARD_ABSURD_FACTOR)}× a board dimension away. "
                        f"This is almost certainly a units error; use millimeters "
                        f"within (or near) the board."
                    ),
                    "errorCode": "POSITION_OFF_BOARD",
                    "boardOutline": board_outline,
                }

            # Off-board refusal (B10): a target outside the outline but not
            # absurd is refused by default — silently parking a footprint off the
            # board is almost never intended.  allowOffBoard:true reinstates the
            # apply-with-warning behaviour for the deliberate case.  Classify
            # BEFORE mutating so a refused move never moves the part.
            if target_class == "off_board" and not allow_off_board:
                return {
                    "success": False,
                    "message": (
                        f"Target position ({position['x']}, {position['y']}) "
                        f"{unit} is outside the board outline "
                        f"(x {bbox[0]:.4g}–{bbox[2]:.4g} mm, "
                        f"y {bbox[1]:.4g}–{bbox[3]:.4g} mm). Refusing to move "
                        f"{reference} off the board; pass allowOffBoard:true to "
                        f"place it there intentionally."
                    ),
                    "errorCode": "POSITION_OFF_BOARD",
                    "boardOutline": board_outline,
                }

            module.SetPosition(pcbnew.VECTOR2I(x_nm, y_nm))

            # Flip to the target layer FIRST, then set rotation (B2).  Flip()
            # mirrors the current orientation, so setting the orientation before
            # the flip lets the flip silently rewrite it — a requested 0° lands
            # as 180° on the far side, while the old response still claimed 0°.
            # Flipping first makes the SetOrientation below the last word on the
            # angle, so the requested value is the one that sticks.
            if layer:
                current_layer = self.board.GetLayerName(module.GetLayer())
                if layer == "B.Cu" and current_layer != "B.Cu":
                    module.Flip(module.GetPosition(), False)
                elif layer == "F.Cu" and current_layer != "F.Cu":
                    module.Flip(module.GetPosition(), False)

            if rotation is not None:
                angle = pcbnew.EDA_ANGLE(rotation, pcbnew.DEGREES_T)
                module.SetOrientation(angle)

            # Build the response from read-backs, never the requested values:
            # after a flip the applied rotation/layer/position can differ from
            # what was asked, and the caller must see what actually landed on
            # disk (the B2 success-message-vs-disk mismatch).
            final_pos = module.GetPosition()
            response: Dict[str, Any] = {
                "success": True,
                "message": f"Moved component: {reference}",
                "component": {
                    "reference": reference,
                    "position": {
                        "x": final_pos.x / 1000000.0,
                        "y": final_pos.y / 1000000.0,
                        "unit": "mm",
                    },
                    "rotation": module.GetOrientation().AsDegrees(),
                    "layer": self.board.GetLayerName(module.GetLayer()),
                },
            }
            if board_outline is not None:
                response["boardOutline"] = board_outline
            if target_class == "off_board":
                # Reached only with allowOffBoard:true — the move applied, but
                # flag that the footprint now sits outside the outline.
                response["offBoardWarning"] = (
                    f"{reference} moved to ({position['x']}, {position['y']}) "
                    f"{unit}, which is outside the board outline "
                    f"(x {bbox[0]:.4g}–{bbox[2]:.4g} mm, "
                    f"y {bbox[1]:.4g}–{bbox[3]:.4g} mm). The move still applied, but "
                    f"the footprint now sits off the board; move it back onto the "
                    f"board or extend the outline."
                )
            return response

        except Exception as e:
            logger.error(f"Error moving component: {str(e)}")
            return failed("Failed to move component", e)

    def rotate_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Rotate an existing component"""
        try:
            if not self.board:
                return no_board_loaded()

            reference = params.get("reference")
            angle = params.get("angle")

            if not reference or angle is None:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "reference and angle are required",
                }

            module = self.board.FindFootprintByReference(reference)
            if not module:
                return {
                    "success": False,
                    "message": "Component not found",
                    "errorDetails": f"Could not find component: {reference}",
                }

            rotation_angle = pcbnew.EDA_ANGLE(angle, pcbnew.DEGREES_T)
            module.SetOrientation(rotation_angle)

            return {
                "success": True,
                "message": f"Rotated component: {reference}",
                "component": {"reference": reference, "rotation": angle},
            }

        except Exception as e:
            logger.error(f"Error rotating component: {str(e)}")
            return failed("Failed to rotate component", e)

    def delete_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delete a component from the PCB"""
        try:
            if not self.board:
                return no_board_loaded()

            module, err = resolve_footprint(self.board, params)
            if err:
                return err
            reference = params.get("reference")

            # Delete (not Remove): Remove() leaks the detached C++ FOOTPRINT on
            # the KiCAD 10 SWIG bindings and can corrupt the SWIG object table;
            # Delete() frees it cleanly. See board/size.py for the full note.
            self.board.Delete(module)

            return {"success": True, "message": f"Deleted component: {reference}"}

        except Exception as e:
            logger.error(f"Error deleting component: {str(e)}")
            return failed("Failed to delete component", e)

    def duplicate_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Duplicate an existing footprint one or more times.

        Params:
          reference    (required) source footprint to copy.
          offset       {x, y, unit?} relative shift; copy *i* lands at
                       source + offset*i (unit defaults to mm). Preferred.
          position     {x, y, unit?} absolute placement of the first copy
                       (alternative to offset).
          newReference optional; auto-annotated from the source when omitted
                       (R2 → R3 …). For count>1 subsequent copies increment it.
          count        number of copies (default 1); each is offset*i apart
                       with a sequential reference.
          rotation     optional override; by default the source orientation
                       is preserved.

        The duplicate is a deep copy (keeps value / layer / orientation /
        footprint-id) but its pads have their nets cleared, matching KiCad's
        own Duplicate — a copy is not silently wired into the source's nets.
        """
        try:
            if not self.board:
                return no_board_loaded()

            reference = params.get("reference")
            new_reference = params.get("newReference")
            offset = params.get("offset")
            position = params.get("position")
            rotation = params.get("rotation")

            try:
                count = int(params.get("count") or 1)
            except (TypeError, ValueError):
                count = 1
            if count < 1:
                count = 1

            if not reference:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "reference is required",
                }

            source = self.board.FindFootprintByReference(reference)
            if not source:
                return {
                    "success": False,
                    "message": "Component not found",
                    "errorDetails": f"Could not find component: {reference}",
                }

            # Allocate the fresh reference(s). Explicit newReference that
            # collides is a hard error; auto mode never collides.
            used = {fp.GetReference() for fp in self.board.GetFootprints()}
            try:
                new_refs = _allocate_duplicate_refs(reference, new_reference, count, used)
            except ValueError as ve:
                return failed("Reference already exists", ve)

            # Compute per-copy positions.
            nm_per_mm = 1000000
            base = source.GetPosition()
            positions = []
            if offset is not None:
                oscale = unit_to_nm_scale(offset.get("unit", "mm"))
                ox = offset["x"] * oscale
                oy = offset["y"] * oscale
                for i in range(1, count + 1):
                    positions.append(pcbnew.VECTOR2I(int(base.x + ox * i), int(base.y + oy * i)))
            elif position is not None:
                pscale = unit_to_nm_scale(position.get("unit", "mm"))
                px = position["x"] * pscale
                py = position["y"] * pscale
                # Absolute placement of the first copy; stack any extras 5 mm
                # apart in x so count>1 doesn't pile them on one another.
                step = 5 * nm_per_mm
                for i in range(count):
                    positions.append(pcbnew.VECTOR2I(int(px + step * i), int(py)))
            else:
                # No offset/position given → 5 mm x step from the source.
                step = 5 * nm_per_mm
                for i in range(1, count + 1):
                    positions.append(pcbnew.VECTOR2I(int(base.x + step * i), int(base.y)))

            created = []
            for ref, pos in zip(new_refs, positions):
                # Deep copy via the copy constructor. FOOTPRINT.Duplicate()
                # exists on KiCAD 10 but returns a base BOARD_ITEM and needs an
                # addToParentGroup arg; the copy constructor yields a real
                # FOOTPRINT directly and keeps value / fpid / orientation /
                # layer / pad geometry. (PAD.Copy() was removed in KiCAD 10 —
                # the old per-pad copy path is why this used to crash.)
                new_module = pcbnew.FOOTPRINT(source)
                # A duplicate must not inherit net assignments — clear each pad
                # so the copy lands unconnected, exactly like KiCad's Duplicate.
                for pad in new_module.Pads():
                    pad.SetNetCode(0)
                new_module.SetReference(ref)
                new_module.SetPosition(pos)
                if rotation is not None:
                    new_module.SetOrientation(pcbnew.EDA_ANGLE(rotation, pcbnew.DEGREES_T))
                # else: copy constructor already preserved the source orientation

                self.board.Add(new_module)

                final = new_module.GetPosition()
                created.append(
                    {
                        "reference": ref,
                        "value": new_module.GetValue(),
                        "footprint": new_module.GetFPIDAsString(),
                        "position": {
                            "x": final.x / nm_per_mm,
                            "y": final.y / nm_per_mm,
                            "unit": "mm",
                        },
                        "rotation": new_module.GetOrientation().AsDegrees(),
                        "layer": self.board.GetLayerName(new_module.GetLayer()),
                    }
                )

            ref_list = ", ".join(c["reference"] for c in created)
            return {
                "success": True,
                "message": f"Duplicated {reference} → {ref_list}",
                "count": len(created),
                "components": created,
                # Backward-compatible single-copy field (the first duplicate).
                "component": created[0],
            }

        except Exception as e:
            logger.error(f"Error duplicating component: {str(e)}")
            return failed("Failed to duplicate component", e)

    def edit_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Edit the properties of an existing component"""
        try:
            if not self.board:
                return no_board_loaded()

            reference = params.get("reference")
            new_reference = params.get("newReference")
            value = params.get("value")
            footprint = params.get("footprint")

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

            if new_reference:
                module.SetReference(new_reference)
            if value:
                module.SetValue(value)
            if footprint:
                # For KiCAD 9.x compatibility, use SetFPID instead of SetFootprintName
                # Parse footprint string (format: "Library:Footprint")
                if ":" in footprint:
                    lib_name, fp_name = footprint.split(":", 1)
                    fpid = pcbnew.LIB_ID(lib_name, fp_name)
                    module.SetFPID(fpid)
                else:
                    # If no library specified, keep existing library
                    current_fpid = module.GetFPID()
                    lib_name = current_fpid.GetLibNickname().GetUTF8()
                    fpid = pcbnew.LIB_ID(lib_name, footprint)
                    module.SetFPID(fpid)

            return {
                "success": True,
                "message": f"Updated component: {reference}",
                "component": {
                    "reference": new_reference or reference,
                    "value": value or module.GetValue(),
                    "footprint": footprint or module.GetFPIDAsString(),
                },
            }

        except Exception as e:
            logger.error(f"Error editing component: {str(e)}")
            return failed("Failed to edit component", e)
