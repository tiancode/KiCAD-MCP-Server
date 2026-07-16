"""Bill-of-materials export and its CSV/XML/HTML/JSON writers.

Split out of the former monolithic commands/export.py.
"""

import logging
import os
import re
from typing import Any, Dict, List

from utils.responses import failed, no_board_loaded

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


# Base BOM columns that footprints always have — never treated as a custom
# sourcing attribute (a caller asking for "value" already gets the value column).
_BASE_BOM_KEYS = {"reference", "value", "footprint", "layer"}

# Separator used to join distinct values when a groupByValue group's members
# disagree on an attribute (e.g. two MPNs behind one value/footprint line).
_ATTR_MULTI_SEP = "; "


def _footprint_fields(module: Any) -> Dict[str, str]:
    """Return {field_name: value} for a footprint's non-empty custom fields.

    Uses KiCad 8+/10 ``GetFieldsText()`` (a name→text dict covering the
    standard Reference/Value/Datasheet/Description plus every user field such
    as MPN, Manufacturer, "LCSC Part").  Robust against SWIG proxies and unit
    MagicMocks that don't return a real dict — anything unexpected yields {}.
    """
    getter = getattr(module, "GetFieldsText", None)
    if not callable(getter):
        return {}
    try:
        raw = dict(getter())
    except Exception:
        return {}
    fields: Dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str):
            continue
        text = "" if value is None else str(value)
        if text.strip():
            fields[name] = text
    return fields


def _resolve_attribute(attr: str, available: List[str]) -> str:
    """Resolve a requested BOM attribute to an actual footprint field name.

    Resolution precedence (first hit wins), against the field names that are
    actually populated on at least one footprint:

      1. exact match                       ("MPN" -> "MPN")
      2. case-insensitive match            ("mpn" -> "MPN")
      3. leading-token alias               ("LCSC" -> "LCSC Part")
         — the requested name equals the first whitespace/``-``/``_``/``#``
         delimited token of a field, so "LCSC" finds "LCSC Part" and
         "JLCPCB" finds "JLCPCB Part #".

    Returns "" when nothing matches (the caller warns).
    """
    if attr in available:
        return attr
    lower = {f.lower(): f for f in available}
    if attr.lower() in lower:
        return lower[attr.lower()]
    attr_l = attr.lower()
    for field in sorted(available):
        first_tok = re.split(r"[\s_\-#]+", field, 1)[0]
        if first_tok.lower() == attr_l:
            return field
    return ""


class BomMixin:
    def export_bom(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Export Bill of Materials"""
        try:
            if not self.board:
                return no_board_loaded()

            output_path = params.get("outputPath")
            format = params.get("format", "CSV")
            group_by_value = params.get("groupByValue", True)
            # Accept both the schema name (includeAttributes) and the shorthand
            # (attributes) some callers use — the sourcing columns were silently
            # dropped when the two disagreed.
            include_attributes = params.get("includeAttributes") or params.get("attributes") or []
            include_mounting_holes = params.get("includeMountingHoles", False)

            if not output_path:
                return {
                    "success": False,
                    "message": "Missing output path",
                    "errorDetails": "outputPath parameter is required",
                }

            output_path = os.path.abspath(os.path.expanduser(output_path))
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            # Get all components.  Custom fields (MPN, "LCSC Part", …) are read
            # only when attributes are requested — keeps the default path
            # identical and avoids GetFieldsText() on every footprint otherwise.
            components = []
            available_fields: set = set()
            for module in self.board.GetFootprints():
                component = {
                    "reference": module.GetReference(),
                    "value": module.GetValue(),
                    "footprint": module.GetFPID().GetUniStringLibId(),
                    "layer": self.board.GetLayerName(module.GetLayer()),
                }
                if include_attributes:
                    fields = _footprint_fields(module)
                    component["_fields"] = fields
                    available_fields.update(fields.keys())
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

            # Resolve each requested attribute to a real (populated) footprint
            # field.  Resolved ones become columns; ones missing everywhere are
            # reported as an explicit warning instead of silently vanishing.
            attribute_columns: List[str] = []  # ordered output column headers
            resolved_map: Dict[str, str] = {}  # column header -> field name
            attributes_resolved: List[Dict[str, str]] = []
            missing_attributes: List[str] = []
            candidate_fields = sorted(
                f for f in available_fields if f.lower() not in _BASE_BOM_KEYS
            )
            for attr in include_attributes:
                if attr.lower() in _BASE_BOM_KEYS:
                    # Already a base column — not a "missing" attribute.
                    continue
                resolved = _resolve_attribute(attr, candidate_fields)
                if resolved:
                    if attr not in attribute_columns:
                        attribute_columns.append(attr)
                        resolved_map[attr] = resolved
                        attributes_resolved.append({"requested": attr, "field": resolved})
                else:
                    missing_attributes.append(attr)

            # Attach each footprint's per-attribute value (ungrouped rows).
            for comp in components:
                fields = comp.pop("_fields", {}) if "_fields" in comp else {}
                for header in attribute_columns:
                    comp[header] = fields.get(resolved_map[header], "")

            if group_by_value:
                grouped = {}
                for comp in components:
                    key = f"{comp['value']}_{comp['footprint']}"
                    if key not in grouped:
                        entry = {
                            "value": comp["value"],
                            "footprint": comp["footprint"],
                            "quantity": 1,
                            "references": [comp["reference"]],
                        }
                        # Track distinct values per attribute so a group whose
                        # members disagree degrades predictably (see below).
                        entry["_attrs"] = {h: [] for h in attribute_columns}
                        grouped[key] = entry
                    else:
                        grouped[key]["quantity"] += 1
                        grouped[key]["references"].append(comp["reference"])
                    for header in attribute_columns:
                        val = comp.get(header, "")
                        if val and val not in grouped[key]["_attrs"][header]:
                            grouped[key]["_attrs"][header].append(val)
                components = list(grouped.values())
                # Emit references as a clean, naturally-sorted string
                # ("MH1, MH2, MH10") instead of a Python list repr.  For each
                # attribute, join the distinct values the group's members
                # carried: one value stays as-is, disagreements are joined
                # (sorted) so no data is silently dropped and the column stays
                # single-cell (groups are NOT split).
                for entry in components:
                    entry["references"] = ", ".join(
                        sorted(entry["references"], key=_natural_ref_key)
                    )
                    attrs = entry.pop("_attrs", {})
                    for header in attribute_columns:
                        vals = attrs.get(header, [])
                        entry[header] = _ATTR_MULTI_SEP.join(sorted(vals)) if vals else ""

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

            result: Dict[str, Any] = {
                "success": True,
                "message": message,
                "file": {
                    "path": output_path,
                    "format": format,
                    "componentCount": len(components),
                },
                "excludedMountingHoles": excluded_mounting_holes,
            }
            if include_attributes:
                # Report which requested attributes became columns (and the
                # field they resolved to), and warn — explicitly, not silently —
                # about any that no footprint carries.
                result["attributesResolved"] = attributes_resolved
                result["attributesMissing"] = missing_attributes
                if missing_attributes:
                    hint = (
                        f"Requested attribute(s) not found on any footprint: "
                        f"{missing_attributes}."
                    )
                    if candidate_fields:
                        hint += f" Available footprint fields: {candidate_fields}."
                    else:
                        hint += (
                            " No footprint carries any custom field — run "
                            "sync_schematic_to_board so schematic sourcing "
                            "fields (MPN, Manufacturer, LCSC Part, …) are copied "
                            "onto the board."
                        )
                    result["warning"] = hint
            return result

        except Exception as e:
            logger.error(f"Error exporting BOM: {str(e)}")
            return failed("Failed to export BOM", e)

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
        header_keys = (
            list(components[0].keys())
            if components
            else ["value", "footprint", "quantity", "references"]
        )
        for key in header_keys:
            html.append(f"<th>{key}</th>")
        html.append("</tr>")
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
