"""
Schematic View handlers, extracted from kicad_interface.py.

See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import glob
import logging
import os
from typing import TYPE_CHECKING, Any, Dict, Optional

from commands.schematic import SchematicManager

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


def _pick_root_svg(out_dir: str, schematic_path: str) -> Optional[str]:
    """Pick the root-sheet SVG that ``kicad-cli sch export svg`` wrote.

    For a hierarchical schematic kicad-cli emits one SVG per sheet; the root
    sheet is named after the input file (``Foo.kicad_sch`` -> ``Foo.svg``).
    Prefer that exact match so callers get the top sheet instead of an
    arbitrary sub-sheet, falling back to the lexicographically-first SVG
    (deterministic, unlike raw glob/listdir order) when the named file is
    absent.  Returns an absolute path, or ``None`` if no SVG was produced.
    """
    svgs = sorted(glob.glob(os.path.join(out_dir, "*.svg")))
    if not svgs:
        return None
    stem = os.path.splitext(os.path.basename(schematic_path))[0]
    preferred = os.path.join(out_dir, f"{stem}.svg")
    return preferred if preferred in svgs else svgs[0]


def handle_snap_to_grid(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Snap schematic element coordinates to the nearest grid point"""
    logger.info("Snapping schematic elements to grid")
    try:
        from pathlib import Path

        from commands.schematic_snap import snap_to_grid

        schematic_path = params.get("schematicPath")
        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}

        grid_size = float(params.get("gridSize", 1.27))
        elements = params.get("elements")  # None → defaults inside snap_to_grid

        result = snap_to_grid(Path(schematic_path), grid_size=grid_size, elements=elements)
        total = result["snapped"] + result["already_on_grid"]
        return {
            "success": True,
            **result,
            "message": (
                f"Snapped {result['snapped']} element(s) to {grid_size} mm grid "
                f"({result['already_on_grid']} of {total} were already on grid)"
            ),
        }
    except Exception as e:
        logger.error(f"Error snapping to grid: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_list_floating_labels(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """List net labels that are not connected to any component pin"""
    logger.info("Listing floating net labels in schematic")
    try:
        from commands.wire_connectivity import list_floating_labels

        schematic_path = params.get("schematicPath")
        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}

        schematic = SchematicManager.load_schematic(schematic_path)
        if not schematic:
            return {"success": False, "message": "Failed to load schematic"}

        labels = list_floating_labels(schematic, schematic_path)
        return {
            "success": True,
            "floating_labels": labels,
            "count": len(labels),
            "message": f"Found {len(labels)} floating label(s)",
        }
    except Exception as e:
        logger.error(f"Error listing floating labels: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_find_orphaned_wires(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Find wire segments with at least one dangling (unconnected) endpoint"""
    logger.info("Finding orphaned wires in schematic")
    try:
        from pathlib import Path

        from commands.schematic_analysis import find_orphaned_wires

        schematic_path = params.get("schematicPath")
        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}

        result = find_orphaned_wires(Path(schematic_path))
        return {
            "success": True,
            **result,
            "message": f"Found {result['count']} orphaned wire(s)",
        }
    except Exception as e:
        logger.error(f"Error finding orphaned wires: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_find_wires_crossing_symbols(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Find wires that cross over component symbol bodies"""
    logger.info("Finding wires crossing symbols in schematic")
    try:
        from pathlib import Path

        from commands.schematic_analysis import find_wires_crossing_symbols

        schematic_path = params.get("schematicPath")
        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}

        result = find_wires_crossing_symbols(Path(schematic_path))
        return {
            "success": True,
            "collisions": result,
            "count": len(result),
            "message": f"Found {len(result)} wire(s) crossing symbols",
        }
    except Exception as e:
        logger.error(f"Error checking wire collisions: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_get_elements_in_region(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """List all wires, labels, and symbols within a rectangular region"""
    logger.info("Getting elements in schematic region")
    try:
        from pathlib import Path

        from commands.schematic_analysis import get_elements_in_region

        schematic_path = params.get("schematicPath")
        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}

        x1 = float(params.get("x1", 0))
        y1 = float(params.get("y1", 0))
        x2 = float(params.get("x2", 0))
        y2 = float(params.get("y2", 0))

        result = get_elements_in_region(Path(schematic_path), x1, y1, x2, y2)
        return {
            "success": True,
            **result,
            "message": f"Found {result['counts']['symbols']} symbols, {result['counts']['wires']} wires, {result['counts']['labels']} labels in region",
        }
    except Exception as e:
        logger.error(f"Error getting elements in region: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_find_overlapping_elements(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Detect spatially overlapping symbols, wires, and labels"""
    logger.info("Finding overlapping elements in schematic")
    try:
        from pathlib import Path

        from commands.schematic_analysis import find_overlapping_elements

        schematic_path = params.get("schematicPath")
        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}

        tolerance = float(params.get("tolerance", 0.5))
        result = find_overlapping_elements(Path(schematic_path), tolerance)
        return {
            "success": True,
            **result,
            "message": f"Found {result['totalOverlaps']} overlap(s)",
        }
    except Exception as e:
        logger.error(f"Error finding overlapping elements: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_get_schematic_view_region(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Export a cropped region of the schematic as an image"""
    logger.info("Exporting schematic view region")
    import base64
    import subprocess
    import tempfile

    try:
        schematic_path = params.get("schematicPath")
        if not schematic_path or not os.path.exists(schematic_path):
            return {"success": False, "message": "Schematic file not found"}

        x1 = float(params.get("x1", 0))
        y1 = float(params.get("y1", 0))
        x2 = float(params.get("x2", 297))
        y2 = float(params.get("y2", 210))
        x1, x2 = min(x1, x2), max(x1, x2)
        y1, y2 = min(y1, y2), max(y1, y2)
        out_format = params.get("format", "png")
        width = int(params.get("width", 800))
        height = int(params.get("height", 600))

        kicad_cli = iface.design_rule_commands._find_kicad_cli()
        if not kicad_cli:
            return {"success": False, "message": "kicad-cli not found"}

        tmp_dir = tempfile.mkdtemp()
        svg_output = None

        try:
            cmd = [
                kicad_cli,
                "sch",
                "export",
                "svg",
                "--output",
                tmp_dir,
                schematic_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

            if result.returncode != 0:
                return {
                    "success": False,
                    "message": f"SVG export failed: {result.stderr}",
                }

            # kicad-cli names the file after the schematic; for a multi-sheet
            # schematic it writes one SVG per sheet, so select the root sheet.
            svg_output = _pick_root_svg(tmp_dir, schematic_path)
            if not svg_output:
                return {
                    "success": False,
                    "message": "kicad-cli produced no SVG output",
                }

            import xml.etree.ElementTree as ET

            tree = ET.parse(svg_output)
            root = tree.getroot()

            # KiCad schematic SVGs use mm as viewBox units directly
            vb = root.get("viewBox", "")
            if vb:
                parts = vb.split()
                if len(parts) == 4:
                    orig_vb_x = float(parts[0])
                    orig_vb_y = float(parts[1])

                    new_x = orig_vb_x + x1
                    new_y = orig_vb_y + y1
                    new_w = x2 - x1
                    new_h = y2 - y1

                    root.set("viewBox", f"{new_x} {new_y} {new_w} {new_h}")
                    root.set("width", str(width))
                    root.set("height", str(height))

            cropped_svg_path = os.path.join(tmp_dir, "cropped.svg")
            tree.write(cropped_svg_path, xml_declaration=True, encoding="utf-8")

            if out_format == "svg":
                with open(cropped_svg_path, "r", encoding="utf-8") as f:
                    svg_data = f.read()
                return {"success": True, "imageData": svg_data, "format": "svg"}
            else:
                try:
                    from cairosvg import svg2png
                except ImportError:
                    return {
                        "success": False,
                        "message": "PNG export requires the 'cairosvg' package. Install it with: pip install cairosvg",
                    }
                png_data = svg2png(url=cropped_svg_path, output_width=width, output_height=height)
                return {
                    "success": True,
                    "imageData": base64.b64encode(png_data).decode("utf-8"),
                    "format": "png",
                }
        finally:
            import shutil

            shutil.rmtree(tmp_dir, ignore_errors=True)

    except Exception as e:
        logger.error(f"Error in get_schematic_view_region: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_get_schematic_view(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Get a rasterised image of the schematic (SVG export → optional PNG conversion)"""
    logger.info("Getting schematic view")
    import base64
    import subprocess
    import tempfile

    try:
        schematic_path = params.get("schematicPath")
        if not schematic_path or not os.path.exists(schematic_path):
            return {
                "success": False,
                "message": f"Schematic not found: {schematic_path}",
            }

        fmt = params.get("format", "png")
        width = int(params.get("width", 1200))
        height = int(params.get("height", 900))

        # Resolve kicad-cli the same way the region exporter does: PATH first,
        # then platform bundle locations (e.g. KiCad.app/Contents/MacOS on
        # macOS, where kicad-cli is not on PATH).
        kicad_cli = iface.design_rule_commands._find_kicad_cli()
        if not kicad_cli:
            return {"success": False, "message": "kicad-cli not found"}

        # Step 1: Export schematic to SVG via kicad-cli
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = [
                kicad_cli,
                "sch",
                "export",
                "svg",
                "--output",
                tmpdir,
                schematic_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

            if result.returncode != 0:
                return {
                    "success": False,
                    "message": f"kicad-cli SVG export failed: {result.stderr}",
                }

            # kicad-cli names the file after the schematic; for a multi-sheet
            # schematic it writes one SVG per sheet, so select the root sheet.
            svg_path = _pick_root_svg(tmpdir, schematic_path)
            if not svg_path:
                return {
                    "success": False,
                    "message": "No SVG file produced by kicad-cli",
                }

            if fmt == "svg":
                with open(svg_path, "r", encoding="utf-8") as f:
                    svg_data = f.read()
                return {"success": True, "imageData": svg_data, "format": "svg"}

            # Step 2: Convert SVG to PNG using cairosvg
            try:
                from cairosvg import svg2png
            except ImportError:
                # Fallback: return SVG data with a note
                with open(svg_path, "r", encoding="utf-8") as f:
                    svg_data = f.read()
                return {
                    "success": True,
                    "imageData": svg_data,
                    "format": "svg",
                    "message": "cairosvg not installed — returning SVG instead of PNG. Install with: pip install cairosvg",
                }

            png_data = svg2png(url=svg_path, output_width=width, output_height=height)

            return {
                "success": True,
                "imageData": base64.b64encode(png_data).decode("utf-8"),
                "format": "png",
                "width": width,
                "height": height,
            }

    except Exception as e:
        logger.error(f"Error getting schematic view: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}
