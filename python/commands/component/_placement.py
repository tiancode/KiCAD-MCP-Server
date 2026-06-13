"""Place / move / rotate / delete / duplicate / edit single-component commands.

Split out of the former monolithic commands/component.py.
"""

import logging
from typing import Any, Dict, List, Optional

import pcbnew

logger = logging.getLogger("kicad_interface")


class PlacementMixin:
    def place_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Place a component on the PCB"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            # Get parameters
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
                        "existingReference": reference,
                    }

            # Find footprint using library manager
            # component_id can be "Library:Footprint" or just "Footprint"
            footprint_result = self.library_manager.find_footprint(component_id)

            if not footprint_result:
                # Try to suggest similar footprints
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

            # Load footprint from library
            # Extract library nickname from path
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

            # Load the footprint
            module = pcbnew.FootprintLoad(library_path, footprint_name)
            if not module:
                return {
                    "success": False,
                    "message": "Failed to load footprint",
                    "errorDetails": f"Could not load footprint from {library_path}/{footprint_name}",
                }

            # Set position
            scale = (
                1000000
                if position["unit"] == "mm"
                else (25400 if position["unit"] == "mil" else 25400000)
            )  # mm, mil, or inch to nm
            x_nm = int(position["x"] * scale)
            y_nm = int(position["y"] * scale)
            module.SetPosition(pcbnew.VECTOR2I(x_nm, y_nm))

            # Set reference if provided
            if reference:
                module.SetReference(reference)

            # Set value if provided
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
                    "position": {"x": position["x"], "y": position["y"], "unit": position["unit"]},
                    "rotation": rotation,
                    "layer": layer,
                },
            }

        except Exception as e:
            logger.error(f"Error placing component: {str(e)}")
            return {
                "success": False,
                "message": "Failed to place component",
                "errorDetails": str(e),
            }

    def move_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Move an existing component to a new position"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            reference = params.get("reference")
            position = params.get("position")
            rotation = params.get("rotation")
            layer = params.get("layer")

            if not reference or not position:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "reference and position are required",
                }

            # Find the component
            module = self.board.FindFootprintByReference(reference)
            if not module:
                return {
                    "success": False,
                    "message": "Component not found",
                    "errorDetails": f"Could not find component: {reference}",
                }

            # Set new position
            scale = (
                1000000
                if position["unit"] == "mm"
                else (25400 if position["unit"] == "mil" else 25400000)
            )  # mm, mil, or inch to nm
            x_nm = int(position["x"] * scale)
            y_nm = int(position["y"] * scale)
            module.SetPosition(pcbnew.VECTOR2I(x_nm, y_nm))

            # Set new rotation if provided
            if rotation is not None:
                angle = pcbnew.EDA_ANGLE(rotation, pcbnew.DEGREES_T)
                module.SetOrientation(angle)

            # Flip to target layer if specified
            if layer:
                current_layer = self.board.GetLayerName(module.GetLayer())
                if layer == "B.Cu" and current_layer != "B.Cu":
                    module.Flip(module.GetPosition(), False)
                elif layer == "F.Cu" and current_layer != "F.Cu":
                    module.Flip(module.GetPosition(), False)

            return {
                "success": True,
                "message": f"Moved component: {reference}",
                "component": {
                    "reference": reference,
                    "position": {"x": position["x"], "y": position["y"], "unit": position["unit"]},
                    "rotation": (
                        rotation if rotation is not None else module.GetOrientation().AsDegrees()
                    ),
                    "layer": self.board.GetLayerName(module.GetLayer()),
                },
            }

        except Exception as e:
            logger.error(f"Error moving component: {str(e)}")
            return {"success": False, "message": "Failed to move component", "errorDetails": str(e)}

    def rotate_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Rotate an existing component"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            reference = params.get("reference")
            angle = params.get("angle")

            if not reference or angle is None:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "reference and angle are required",
                }

            # Find the component
            module = self.board.FindFootprintByReference(reference)
            if not module:
                return {
                    "success": False,
                    "message": "Component not found",
                    "errorDetails": f"Could not find component: {reference}",
                }

            # Set rotation
            rotation_angle = pcbnew.EDA_ANGLE(angle, pcbnew.DEGREES_T)
            module.SetOrientation(rotation_angle)

            return {
                "success": True,
                "message": f"Rotated component: {reference}",
                "component": {"reference": reference, "rotation": angle},
            }

        except Exception as e:
            logger.error(f"Error rotating component: {str(e)}")
            return {
                "success": False,
                "message": "Failed to rotate component",
                "errorDetails": str(e),
            }

    def delete_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delete a component from the PCB"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            reference = params.get("reference")
            if not reference:
                return {
                    "success": False,
                    "message": "Missing reference",
                    "errorDetails": "reference parameter is required",
                }

            # Find the component
            module = self.board.FindFootprintByReference(reference)
            if not module:
                return {
                    "success": False,
                    "message": "Component not found",
                    "errorDetails": f"Could not find component: {reference}",
                }

            # Delete (not Remove): Remove() leaks the detached C++ FOOTPRINT on
            # the KiCAD 10 SWIG bindings and can corrupt the SWIG object table;
            # Delete() frees it cleanly. See board/size.py for the full note.
            self.board.Delete(module)

            return {"success": True, "message": f"Deleted component: {reference}"}

        except Exception as e:
            logger.error(f"Error deleting component: {str(e)}")
            return {
                "success": False,
                "message": "Failed to delete component",
                "errorDetails": str(e),
            }

    def duplicate_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Duplicate an existing component"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            reference = params.get("reference")
            new_reference = params.get("newReference")
            position = params.get("position")
            rotation = params.get("rotation")

            if not reference or not new_reference:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "reference and newReference are required",
                }

            # Find the source component
            source = self.board.FindFootprintByReference(reference)
            if not source:
                return {
                    "success": False,
                    "message": "Component not found",
                    "errorDetails": f"Could not find component: {reference}",
                }

            # Check if new reference already exists
            if self.board.FindFootprintByReference(new_reference):
                return {
                    "success": False,
                    "message": "Reference already exists",
                    "errorDetails": f"A component with reference {new_reference} already exists",
                }

            # Create new footprint with the same properties
            new_module = pcbnew.FOOTPRINT(self.board)
            # For KiCAD 9.x compatibility, use SetFPID instead of SetFootprintName
            new_module.SetFPID(source.GetFPID())
            new_module.SetValue(source.GetValue())
            new_module.SetReference(new_reference)
            new_module.SetLayer(source.GetLayer())

            # Copy pads and other items
            for pad in source.Pads():
                new_pad = pcbnew.PAD(new_module)
                new_pad.Copy(pad)
                new_module.Add(new_pad)

            # Set position if provided, otherwise use offset from original
            if position:
                scale = (
                    1000000
                    if position.get("unit", "mm") == "mm"
                    else (25400 if position.get("unit", "mm") == "mil" else 25400000)
                )  # mm, mil, or inch to nm
                x_nm = int(position["x"] * scale)
                y_nm = int(position["y"] * scale)
                new_module.SetPosition(pcbnew.VECTOR2I(x_nm, y_nm))
            else:
                # Offset by 5mm
                source_pos = source.GetPosition()
                new_module.SetPosition(pcbnew.VECTOR2I(source_pos.x + 5000000, source_pos.y))

            # Set rotation if provided, otherwise use same as original
            if rotation is not None:
                rotation_angle = pcbnew.EDA_ANGLE(rotation, pcbnew.DEGREES_T)
                new_module.SetOrientation(rotation_angle)
            else:
                new_module.SetOrientation(source.GetOrientation())

            # Add to board
            self.board.Add(new_module)

            # Get final position in mm
            pos = new_module.GetPosition()

            return {
                "success": True,
                "message": f"Duplicated component {reference} to {new_reference}",
                "component": {
                    "reference": new_reference,
                    "value": new_module.GetValue(),
                    "footprint": new_module.GetFPIDAsString(),
                    "position": {"x": pos.x / 1000000, "y": pos.y / 1000000, "unit": "mm"},
                    "rotation": new_module.GetOrientation().AsDegrees(),
                    "layer": self.board.GetLayerName(new_module.GetLayer()),
                },
            }

        except Exception as e:
            logger.error(f"Error duplicating component: {str(e)}")
            return {
                "success": False,
                "message": "Failed to duplicate component",
                "errorDetails": str(e),
            }

    def edit_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Edit the properties of an existing component"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

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

            # Find the component
            module = self.board.FindFootprintByReference(reference)
            if not module:
                return {
                    "success": False,
                    "message": "Component not found",
                    "errorDetails": f"Could not find component: {reference}",
                }

            # Update properties
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
            return {"success": False, "message": "Failed to edit component", "errorDetails": str(e)}
