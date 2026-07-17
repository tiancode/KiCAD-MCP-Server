"""Region cropping for plotted board SVGs.

KiCad's PLOT_CONTROLLER always plots the full page; the board's mm
coordinate space maps 1:1 onto the page. Cropping the root viewBox to a
board-space rectangle therefore yields a zoomed view of just that region —
and doing it on the SVG (before any raster conversion) makes the same
region parameter work for svg, png, and jpg outputs alike.
"""

import re
from typing import Optional, Tuple

_SVG_TAG_RE = re.compile(r"<svg\b[^>]*>", re.DOTALL)
_ATTR_RE = re.compile(r'(\w[\w:-]*)\s*=\s*"([^"]*)"')

_UNIT_TO_MM = {"mm": 1.0, "cm": 10.0, "in": 25.4, "": 1.0, "px": 25.4 / 96.0}


def _parse_length_mm(value: str) -> Optional[float]:
    """Parse an SVG length attribute ('297.002mm', '29.7cm') into mm."""
    m = re.fullmatch(r"\s*([0-9.eE+-]+)\s*([a-z%]*)\s*", value)
    if not m or m.group(2) not in _UNIT_TO_MM:
        return None
    try:
        return float(m.group(1)) * _UNIT_TO_MM[m.group(2)]
    except ValueError:
        return None


def crop_svg_to_region(
    svg_text: str,
    region_mm: Tuple[float, float, float, float],
) -> Optional[str]:
    """Rewrite the root viewBox so only the given board-mm region is shown.

    region_mm: (x1, y1, x2, y2) in board/page millimeters.
    Returns the modified SVG text, or None when the root tag can't be
    parsed (caller should fall back to the uncropped image).
    """
    x1, y1, x2, y2 = region_mm
    if x2 <= x1 or y2 <= y1:
        return None

    tag_match = _SVG_TAG_RE.search(svg_text)
    if not tag_match:
        return None
    tag = tag_match.group(0)
    attrs = dict(_ATTR_RE.findall(tag))

    view_box = attrs.get("viewBox") or attrs.get("viewbox")
    width_mm = _parse_length_mm(attrs.get("width", ""))
    if not view_box or not width_mm:
        return None
    try:
        vb = [float(v) for v in view_box.replace(",", " ").split()]
        vb_x, vb_y, vb_w, _vb_h = vb
    except (ValueError, IndexError):
        return None
    if vb_w <= 0 or width_mm <= 0:
        return None

    # viewBox units per page millimeter (KiCad emits mm * 10^precision).
    scale = vb_w / width_mm

    new_vb = (
        f"{vb_x + x1 * scale:.3f} {vb_y + y1 * scale:.3f} "
        f"{(x2 - x1) * scale:.3f} {(y2 - y1) * scale:.3f}"
    )

    new_tag = tag
    new_tag = re.sub(r'viewBox\s*=\s*"[^"]*"', f'viewBox="{new_vb}"', new_tag, count=1)
    new_tag = re.sub(r'width\s*=\s*"[^"]*"', f'width="{x2 - x1:.3f}mm"', new_tag, count=1)
    new_tag = re.sub(r'height\s*=\s*"[^"]*"', f'height="{y2 - y1:.3f}mm"', new_tag, count=1)
    return svg_text[: tag_match.start()] + new_tag + svg_text[tag_match.end() :]
