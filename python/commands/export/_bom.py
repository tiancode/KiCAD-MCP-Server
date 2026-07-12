"""Bill-of-materials export and its CSV/XML/HTML/JSON writers.

Split out of the former monolithic commands/export.py.
"""

import logging
import os
import re
from typing import Any, Dict, List

logger = logging.getLogger("kicad_interface")


def _natural_ref_key(ref: str) -> List[Any]:
    """Sort key so refs order naturally: MH1, MH2, MH10 (not MH1, MH10, MH2)."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", ref or "")]


def _is_mounting_hole(reference: str, footprint: str) -> bool:
    """Board hardware with nothing to purchase: reference prefix MH, or a
    MountingHole footprint id."""
    prefix = re.match(r"[A-Za-z]+", reference or "")
    if prefix and prefix.group(0).upper() == "MH":
        return True
    return "mountinghole" in (footprint or "").lower()


class BomMixin:
    def export_bom(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Export Bill of Materials"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            output_path = params.get("outputPath")
            format = params.get("format", "CSV")
            group_by_value = params.get("groupByValue", True)
            include_attributes = params.get("includeAttributes", [])
            include_mounting_holes = params.get("includeMountingHoles", False)

            if not output_path:
                return {
                    "success": False,
                    "message": "Missing output path",
                    "errorDetails": "outputPath parameter is required",
                }

            # Create output directory if it doesn't exist
            output_path = os.path.abspath(os.path.expanduser(output_path))
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            # Get all components
            components = []
            for module in self.board.GetFootprints():
                component = {
                    "reference": module.GetReference(),
                    "value": module.GetValue(),
                    "footprint": module.GetFPID().GetUniStringLibId(),
                    "layer": self.board.GetLayerName(module.GetLayer()),
                }

                # Add requested attributes
                for attr in include_attributes:
                    if hasattr(module, f"Get{attr}"):
                        component[attr] = getattr(module, f"Get{attr}")()

                components.append(component)

            # Drop board hardware (mounting holes, etc.) that has nothing to
            # purchase — it otherwise pollutes the BOM. Opt back in with
            # includeMountingHoles.
            excluded_mounting_holes = 0
            if not include_mounting_holes:
                kept = []
                for comp in components:
                    if _is_mounting_hole(comp["reference"], comp["footprint"]):
                        excluded_mounting_holes += 1
                    else:
                        kept.append(comp)
                components = kept

            # Group by value if requested
            if group_by_value:
                grouped = {}
                for comp in components:
                    key = f"{comp['value']}_{comp['footprint']}"
                    if key not in grouped:
                        grouped[key] = {
                            "value": comp["value"],
                            "footprint": comp["footprint"],
                            "quantity": 1,
                            "references": [comp["reference"]],
                        }
                    else:
                        grouped[key]["quantity"] += 1
                        grouped[key]["references"].append(comp["reference"])
                components = list(grouped.values())
                # Emit references as a clean, naturally-sorted string
                # ("MH1, MH2, MH10") instead of a Python list repr.
                for entry in components:
                    entry["references"] = ", ".join(
                        sorted(entry["references"], key=_natural_ref_key)
                    )

            # Export based on format
            if format == "CSV":
                self._export_bom_csv(output_path, components)
            elif format == "XML":
                self._export_bom_xml(output_path, components)
            elif format == "HTML":
                self._export_bom_html(output_path, components)
            elif format == "JSON":
                self._export_bom_json(output_path, components)
            else:
                return {
                    "success": False,
                    "message": "Unsupported format",
                    "errorDetails": f"Format {format} is not supported",
                }

            message = f"Exported BOM to {format}"
            if excluded_mounting_holes:
                message += f" ({excluded_mounting_holes} mounting hole(s) excluded)"
            return {
                "success": True,
                "message": message,
                "file": {
                    "path": output_path,
                    "format": format,
                    "componentCount": len(components),
                },
                "excludedMountingHoles": excluded_mounting_holes,
            }

        except Exception as e:
            logger.error(f"Error exporting BOM: {str(e)}")
            return {
                "success": False,
                "message": "Failed to export BOM",
                "errorDetails": str(e),
            }

    def _export_bom_csv(self, path: str, components: List[Dict[str, Any]]) -> None:
        """Export BOM to CSV format"""
        import csv

        fieldnames = (
            list(components[0].keys())
            if components
            else ["value", "footprint", "quantity", "references"]
        )
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(components)

    def _export_bom_xml(self, path: str, components: List[Dict[str, Any]]) -> None:
        """Export BOM to XML format"""
        import xml.etree.ElementTree as ET

        root = ET.Element("bom")
        for comp in components:
            comp_elem = ET.SubElement(root, "component")
            for key, value in comp.items():
                elem = ET.SubElement(comp_elem, key)
                elem.text = str(value)
        tree = ET.ElementTree(root)
        tree.write(path, encoding="utf-8", xml_declaration=True)

    def _export_bom_html(self, path: str, components: List[Dict[str, Any]]) -> None:
        """Export BOM to HTML format"""
        html = ["<html><head><title>Bill of Materials</title></head><body>"]
        html.append("<table border='1'><tr>")
        # Headers
        header_keys = (
            list(components[0].keys())
            if components
            else ["value", "footprint", "quantity", "references"]
        )
        for key in header_keys:
            html.append(f"<th>{key}</th>")
        html.append("</tr>")
        # Data
        for comp in components:
            html.append("<tr>")
            for value in comp.values():
                html.append(f"<td>{value}</td>")
            html.append("</tr>")
        html.append("</table></body></html>")
        with open(path, "w") as f:
            f.write("\n".join(html))

    def _export_bom_json(self, path: str, components: List[Dict[str, Any]]) -> None:
        """Export BOM to JSON format"""
        import json

        with open(path, "w") as f:
            json.dump({"components": components}, f, indent=2)
