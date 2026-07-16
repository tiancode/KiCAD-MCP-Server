"""Annotation / grouping / footprint-swap component commands.

Three SWIG-path board mutations that were part of the public tool surface,
removed when they had no Python backend, and re-implemented here for real:

  * ``add_component_annotation`` — drop a ``pcbnew.PCB_TEXT`` near a footprint
    on a silkscreen / comments layer.
  * ``group_components``         — collect footprints into a ``pcbnew.PCB_GROUP``
    (deterministic re-grouping: a member already in another group is moved,
    and a group emptied by that move is removed).
  * ``replace_component``        — swap a footprint for a different library id,
    preserving reference / position / rotation / side and transferring pad net
    assignments by pad number, reporting anything that could not be carried
    over.

Split out of the per-area mixins so the swap/group/annotate logic lives in one
cohesive place.  All three are registered as identity routes on
``component_commands`` (so they inherit the SWIG board-reload guard) and listed
in ``_BOARD_MUTATING_COMMANDS`` (so they auto-save and hit the cross-backend
conflict gate exactly like move_component / delete_component).
"""

import logging
from typing import Any, Dict, List, Optional

import pcbnew

from utils.responses import failed, no_board_loaded
from utils.units import unit_to_nm_scale

logger = logging.getLogger("kicad_interface")


# KiCad 6+ renamed several technical layers; the canonical KiCad 10 names are
# what GetLayerID resolves, but agents (and the tool's own examples) commonly
# pass the legacy short names.  Accept both by mapping legacy -> canonical when
# the direct lookup fails.
_LEGACY_LAYER_ALIASES = {
    "F.SilkS": "F.Silkscreen",
    "B.SilkS": "B.Silkscreen",
    "Cmts.User": "User.Comments",
    "Dwgs.User": "User.Drawings",
    "Eco1.User": "User.Eco1",
    "Eco2.User": "User.Eco2",
}


def _resolve_layer_id(board: Any, layer: str) -> int:
    """Resolve a layer name to its id, tolerating legacy short names.

    Returns the layer id, or -1 when the name is unknown even after the
    legacy-alias fallback.
    """
    layer_id = board.GetLayerID(layer)
    if layer_id is not None and layer_id >= 0:
        return layer_id
    alias = _LEGACY_LAYER_ALIASES.get(layer)
    if alias is not None:
        return board.GetLayerID(alias)
    return -1


class AnnotateGroupReplaceMixin:
    # ------------------------------------------------------------------ #
    # add_component_annotation
    # ------------------------------------------------------------------ #
    def add_component_annotation(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Place a free text annotation near a component.

        Params:
          reference (required) footprint to annotate.
          text      (required) annotation text (alias: ``annotation`` for
                    backward compatibility with the pre-removal tool).
          layer     silkscreen / comments layer name (default
                    ``F.Silkscreen``; legacy short names like ``F.SilkS`` are
                    also accepted).
          offset    {x, y, unit?} shift from the footprint's origin (default
                    (0, 0) mm — text lands at the component position).
          size      text height in mm (default 1.0).

        A board-level ``PCB_TEXT`` is added (not a footprint field), mirroring
        board.add_text — it is independent graphics positioned at the part.
        """
        try:
            if not self.board:
                return no_board_loaded()

            reference = params.get("reference")
            # `text` is the new name; `annotation` is the pre-removal name.
            text = params.get("text")
            if text is None:
                text = params.get("annotation")
            layer = params.get("layer", "F.Silkscreen")
            offset = params.get("offset")
            try:
                size = float(params.get("size", 1.0))
            except (TypeError, ValueError):
                size = 1.0

            if not reference:
                return {
                    "success": False,
                    "message": "Missing reference",
                    "errorDetails": "reference parameter is required",
                    "errorCode": "VALIDATION",
                }
            if not text:
                return {
                    "success": False,
                    "message": "Missing annotation text",
                    "errorDetails": "text (or annotation) parameter is required",
                    "errorCode": "VALIDATION",
                }

            module = self.board.FindFootprintByReference(reference)
            if not module:
                return {
                    "success": False,
                    "message": f"Component not found: {reference}",
                    "errorDetails": f"No footprint with reference {reference} on the board",
                    "errorCode": "COMPONENT_NOT_FOUND",
                }

            layer_id = _resolve_layer_id(self.board, layer)
            if layer_id < 0:
                return {
                    "success": False,
                    "message": "Invalid layer",
                    "errorDetails": f"Layer '{layer}' does not exist on this board",
                    "errorCode": "VALIDATION",
                }

            base = module.GetPosition()
            dx_nm = dy_nm = 0
            if offset is not None:
                oscale = unit_to_nm_scale(offset.get("unit", "mm"))
                dx_nm = int(float(offset.get("x", 0)) * oscale)
                dy_nm = int(float(offset.get("y", 0)) * oscale)
            x_nm = int(base.x) + dx_nm
            y_nm = int(base.y) + dy_nm

            size_nm = int(size * 1000000)

            pcb_text = pcbnew.PCB_TEXT(self.board)
            pcb_text.SetText(text)
            pcb_text.SetPosition(pcbnew.VECTOR2I(x_nm, y_nm))
            pcb_text.SetLayer(layer_id)
            pcb_text.SetTextSize(pcbnew.VECTOR2I(size_nm, size_nm))
            # Back-layer text is read through the board and must be mirrored,
            # matching board.add_text's convention (else DRC flags
            # nonmirrored_text_on_back_layer).
            pcb_text.SetMirrored(str(layer).startswith("B."))
            self.board.Add(pcb_text)

            return {
                "success": True,
                "message": f"Added annotation to {reference}",
                "annotation": {
                    "reference": reference,
                    "text": text,
                    "layer": layer,
                    "position": {
                        "x": x_nm / 1000000.0,
                        "y": y_nm / 1000000.0,
                        "unit": "mm",
                    },
                    "size": size,
                },
            }

        except Exception as e:
            logger.error(f"Error adding component annotation: {str(e)}")
            return failed("Failed to add component annotation", e)

    # ------------------------------------------------------------------ #
    # group_components
    # ------------------------------------------------------------------ #
    def group_components(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Group footprints into a named ``PCB_GROUP``.

        Params:
          references (required) list of reference designators to group.
          groupName  (required) name for the new group.

        Refuses (without creating any group) if ANY reference is unknown, so a
        typo never yields a silent partial group.  Deterministic re-grouping: a
        footprint already in another group is MOVED into the new one, and a
        group left empty by that move is removed — both reported in the result.
        """
        try:
            if not self.board:
                return no_board_loaded()

            references = params.get("references")
            group_name = params.get("groupName")

            if not references or not isinstance(references, list):
                return {
                    "success": False,
                    "message": "Missing references",
                    "errorDetails": "references must be a non-empty list of designators",
                    "errorCode": "VALIDATION",
                }
            if not group_name:
                return {
                    "success": False,
                    "message": "Missing groupName",
                    "errorDetails": "groupName parameter is required",
                    "errorCode": "VALIDATION",
                }

            # De-duplicate while preserving order.
            seen: set = set()
            ordered_refs: List[str] = []
            for ref in references:
                if ref not in seen:
                    seen.add(ref)
                    ordered_refs.append(ref)

            # Resolve every ref BEFORE mutating anything.  One unknown ref → hard
            # refusal with the full missing list; no partial group is created.
            modules: List[Any] = []
            missing: List[str] = []
            for ref in ordered_refs:
                fp = self.board.FindFootprintByReference(ref)
                if fp is None:
                    missing.append(ref)
                else:
                    modules.append(fp)
            if missing:
                return {
                    "success": False,
                    "message": f"Unknown reference(s): {', '.join(missing)}",
                    "errorDetails": (
                        "Refusing to create a partial group; every reference must "
                        "exist. Verify with get_component_list / find_component."
                    ),
                    "errorCode": "COMPONENT_NOT_FOUND",
                    "missing": missing,
                }

            # Map every currently-grouped footprint to the reliable group object
            # from board.Groups().  (GetItems() on the proxy that GetParentGroup()
            # returns is a raw SwigPyObject with no len() after a RemoveItem — the
            # board.Groups() objects stay iterable, so use those for the emptiness
            # check.)  Reference designators are unique per board, so this maps a
            # footprint to its containing group unambiguously.
            ref_to_group: Dict[str, Any] = {}
            for grp_obj in self.board.Groups():
                for item in grp_obj.GetItems():
                    if hasattr(item, "GetReference"):
                        ref_to_group[item.GetReference()] = grp_obj

            # Detach any member already in a group, tracking the source groups so
            # ones this call empties can be cleaned up deterministically.
            reassigned: List[Dict[str, str]] = []
            source_groups: Dict[int, Any] = {}
            for fp in modules:
                existing = fp.GetParentGroup()
                if existing is not None:
                    reassigned.append(
                        {"reference": fp.GetReference(), "fromGroup": existing.GetName()}
                    )
                    reliable = ref_to_group.get(fp.GetReference(), existing)
                    source_groups[id(reliable)] = reliable
                    existing.RemoveItem(fp)

            group = pcbnew.PCB_GROUP(self.board)
            group.SetName(group_name)
            for fp in modules:
                group.AddItem(fp)
            self.board.Add(group)

            removed_empty: List[str] = []
            for src in source_groups.values():
                if len(src.GetItems()) == 0:
                    removed_empty.append(src.GetName())
                    self.board.Remove(src)

            result: Dict[str, Any] = {
                "success": True,
                "message": f"Grouped {len(modules)} component(s) as '{group_name}'",
                "group": {
                    "name": group_name,
                    "memberCount": len(modules),
                    "members": [fp.GetReference() for fp in modules],
                },
            }
            if reassigned:
                result["reassigned"] = reassigned
            if removed_empty:
                result["removedEmptyGroups"] = removed_empty
            return result

        except Exception as e:
            logger.error(f"Error grouping components: {str(e)}")
            return failed("Failed to group components", e)

    # ------------------------------------------------------------------ #
    # replace_component
    # ------------------------------------------------------------------ #
    def replace_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Swap a placed footprint for a different library footprint.

        Params:
          reference      (required) footprint to replace.
          newFootprint / newComponentId (required) new footprint library id
                         ("Lib:Fp" or bare "Fp"; the two names are aliases, the
                         former preferred, the latter kept for backward compat).
          newValue       optional new value (defaults to the old value).

        DESTRUCTIVE: the old footprint is deleted and a fresh one added. The
        reference, position, rotation, board side and — where pad numbers match
        — pad net assignments are preserved. Pads that could not be matched (on
        either side) are reported truthfully.
        """
        try:
            if not self.board:
                return no_board_loaded()

            reference = params.get("reference")
            # Preferred name is newFootprint; newComponentId is the pre-removal
            # name kept for backward compatibility.
            new_spec = params.get("newFootprint") or params.get("newComponentId")
            new_value = params.get("newValue")

            if not reference:
                return {
                    "success": False,
                    "message": "Missing reference",
                    "errorDetails": "reference parameter is required",
                    "errorCode": "VALIDATION",
                }
            if not new_spec:
                return {
                    "success": False,
                    "message": "Missing new footprint",
                    "errorDetails": "newFootprint (or newComponentId) is required",
                    "errorCode": "VALIDATION",
                }

            old = self.board.FindFootprintByReference(reference)
            if not old:
                return {
                    "success": False,
                    "message": f"Component not found: {reference}",
                    "errorDetails": f"No footprint with reference {reference} on the board",
                    "errorCode": "COMPONENT_NOT_FOUND",
                }

            # Resolve the new footprint the same way place_component does.
            footprint_result = self.library_manager.find_footprint(new_spec)
            if not footprint_result:
                suggestions = self.library_manager.search_footprints(f"*{new_spec}*", limit=5)
                suggestion_text = ""
                if suggestions:
                    suggestion_text = "\n\nDid you mean one of these?\n" + "\n".join(
                        f"  - {s['full_name']}" for s in suggestions
                    )
                return {
                    "success": False,
                    "message": "New footprint not found",
                    "errorDetails": f"Could not find footprint: {new_spec}{suggestion_text}",
                    "errorCode": "FOOTPRINT_NOT_FOUND",
                }

            library_path, footprint_name = footprint_result
            library_nickname = None
            for nick, path in self.library_manager.libraries.items():
                if path == library_path:
                    library_nickname = nick
                    break

            new_module = pcbnew.FootprintLoad(library_path, footprint_name)
            if not new_module:
                return {
                    "success": False,
                    "message": "Failed to load footprint",
                    "errorDetails": f"Could not load footprint from {library_path}/{footprint_name}",
                    "errorCode": "FOOTPRINT_NOT_FOUND",
                }

            # FootprintLoad only knows the directory path, so the loaded FPID
            # carries no library nickname — restore it so the board file keeps
            # a resolvable "Lib:Name" id (round-7 live-smoke finding).
            if library_nickname and hasattr(pcbnew, "LIB_ID"):
                new_module.SetFPID(pcbnew.LIB_ID(library_nickname, footprint_name))

            # Capture everything worth preserving BEFORE deleting the old part.
            old_pos = old.GetPosition()
            old_rot = old.GetOrientation()
            old_layer = old.GetLayer()
            old_flipped = old.IsFlipped()
            old_value = old.GetValue()
            old_group = old.GetParentGroup()
            # Pad number -> net code (first real net wins for a repeated number).
            old_pad_nets: Dict[str, int] = {}
            for pad in old.Pads():
                code = pad.GetNetCode()
                if code and code != 0:
                    old_pad_nets.setdefault(pad.GetNumber(), code)

            new_module.SetReference(reference)
            new_module.SetValue(new_value if new_value else old_value)
            new_module.SetPosition(old_pos)
            new_module.SetOrientation(old_rot)

            # Detach the old part from its group first (avoid a dangling member),
            # then Delete (not Remove — Remove leaks the FOOTPRINT on KiCAD 10).
            if old_group is not None:
                old_group.RemoveItem(old)
            self.board.Delete(old)

            # Add to the board BEFORE assigning pad nets — SetNet only resolves
            # against the board's NETINFO once the footprint is attached.
            self.board.Add(new_module)

            # Preserve the board side: flip if the new part landed on the wrong
            # side, then re-assert the captured orientation (flip mirrors it).
            if new_module.GetLayer() != old_layer:
                if new_module.IsFlipped() != old_flipped:
                    new_module.Flip(new_module.GetPosition(), False)
                    new_module.SetOrientation(old_rot)
                # Non-flip layer mismatch (rare) — set the layer directly.
                if new_module.GetLayer() != old_layer and not old_flipped:
                    new_module.SetLayer(old_layer)

            # Transfer pad nets by pad number.
            new_pad_numbers = {p.GetNumber() for p in new_module.Pads()}
            matched_pads: List[str] = []
            unmatched_new_pads: List[str] = []
            for pad in new_module.Pads():
                num = pad.GetNumber()
                if num in old_pad_nets:
                    net = self.board.FindNet(old_pad_nets[num])
                    if net is not None:
                        pad.SetNet(net)
                    else:
                        pad.SetNetCode(old_pad_nets[num])
                    matched_pads.append(num)
                else:
                    unmatched_new_pads.append(num)

            # Old pad nets with no matching new pad number are dropped — report.
            dropped_nets: List[Dict[str, Any]] = []
            for num, code in old_pad_nets.items():
                if num not in new_pad_numbers:
                    net = self.board.FindNet(code)
                    dropped_nets.append(
                        {"pad": num, "net": net.GetNetname() if net is not None else str(code)}
                    )

            # Restore group membership.
            if old_group is not None:
                old_group.AddItem(new_module)

            final_pos = new_module.GetPosition()
            new_fpid = (
                f"{library_nickname}:{footprint_name}"
                if library_nickname
                else new_module.GetFPIDAsString()
            )
            return {
                "success": True,
                "message": f"Replaced {reference} with {new_fpid}",
                "component": {
                    "reference": reference,
                    "value": new_module.GetValue(),
                    "footprint": new_module.GetFPIDAsString(),
                    "position": {
                        "x": final_pos.x / 1000000.0,
                        "y": final_pos.y / 1000000.0,
                        "unit": "mm",
                    },
                    "rotation": new_module.GetOrientation().AsDegrees(),
                    "layer": self.board.GetLayerName(new_module.GetLayer()),
                },
                "padMatch": {
                    "matched": matched_pads,
                    "unmatchedNewPads": unmatched_new_pads,
                    "droppedNets": dropped_nets,
                },
            }

        except Exception as e:
            logger.error(f"Error replacing component: {str(e)}")
            return failed("Failed to replace component", e)
