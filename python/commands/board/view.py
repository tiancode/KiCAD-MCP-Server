"""
Board view command implementations for KiCAD interface
"""

import base64
import io
import logging
import os
from typing import Any, Dict, Optional

import pcbnew
from PIL import Image

logger = logging.getLogger("kicad_interface")


def _fit_within_box(img: "Image.Image", max_w: int, max_h: int) -> "Image.Image":
    """Scale ``img`` to fit within ``max_w`` x ``max_h``, preserving aspect ratio.

    Scales both up and down ("contain" semantics) so an explicitly requested
    size is honored regardless of the source resolution.  Returns ``img``
    unchanged when the box is degenerate or the size already matches.
    """
    if max_w <= 0 or max_h <= 0:
        return img
    w, h = img.size
    if w <= 0 or h <= 0:
        return img
    scale = min(max_w / w, max_h / h)
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    if (new_w, new_h) == (w, h):
        return img
    return img.resize((new_w, new_h), Image.LANCZOS)


class BoardViewCommands:
    """Handles board viewing operations"""

    # Default layer set for get_board_2d_view when the caller passes no
    # ``layers`` (finding N5).  Plotting EVERY enabled layer in ascending
    # layer-id order rendered F.Cu first and then drew the B.Cu zone fill,
    # courtyard, and Fab outlines OVER it in the same colour — a fully
    # populated board looked like a black rectangle with a few stray shapes.
    # This curated set is plotted back-to-front (list order == paint order)
    # so front copper and silkscreen stay visible; pads, vias and filled
    # zones plot as part of their copper layers.  Explicit ``layers``
    # behaviour is unchanged.
    _DEFAULT_VIEW_LAYERS = (
        "B.Cu",
        "F.Cu",
        "B.SilkS",
        "F.SilkS",
        "Edge.Cuts",
    )

    def __init__(self, board: Optional[pcbnew.BOARD] = None):
        """Initialize with optional board instance"""
        self.board = board

    def get_board_info(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get information about the current board"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            # Get board dimensions
            board_box = self.board.GetBoardEdgesBoundingBox()
            width_nm = board_box.GetWidth()
            height_nm = board_box.GetHeight()

            # Convert to mm
            width_mm = width_nm / 1000000
            height_mm = height_nm / 1000000

            # Get layer information
            layers = []
            for layer_id in range(pcbnew.PCB_LAYER_ID_COUNT):
                if self.board.IsLayerEnabled(layer_id):
                    layers.append(
                        {
                            "name": self.board.GetLayerName(layer_id),
                            "type": self._get_layer_type_name(self.board.GetLayerType(layer_id)),
                            "id": layer_id,
                        }
                    )

            return {
                "success": True,
                "board": {
                    "filename": self.board.GetFileName(),
                    "size": {"width": width_mm, "height": height_mm, "unit": "mm"},
                    "layers": layers,
                    "title": self.board.GetTitleBlock().GetTitle(),
                    # Note: activeLayer removed - GetActiveLayer() doesn't exist in KiCAD 9.0
                    # Active layer is a UI concept not applicable to headless scripting
                },
            }

        except Exception as e:
            logger.error(f"Error getting board info: {str(e)}")
            return {
                "success": False,
                "message": "Failed to get board information",
                "errorDetails": str(e),
            }

    def get_board_2d_view(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get a 2D image of the PCB.

        responseMode controls how the image is returned:
        - "inline" (default): image bytes are base64-encoded and returned as ``imageData``.
        - "file": image is written next to the .kicad_pcb file and ``filePath`` is returned.
        """
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            # Get parameters
            width = params.get("width", 800)
            height = params.get("height", 600)
            # Whether the caller explicitly asked for a size. In crop-to-board
            # mode the alpha-crop resizes the raster down to just the board
            # content, which otherwise discards a requested width/height; when
            # a size is explicit we fit the cropped board to it (see below).
            size_requested = params.get("width") is not None or params.get("height") is not None
            format = params.get("format", "png")
            layers = params.get("layers", [])
            response_mode = params.get("responseMode", "inline")
            # Auto-fit to the Edge.Cuts bbox + margin so stray objects outside
            # the board outline don't squash the actual PCB into a corner.
            # Default on — was the user's #8 complaint.
            crop_to_board = bool(params.get("cropToBoard", True))
            crop_margin_px = int(params.get("cropMarginPx", 20))
            # Optional zoom to a board-space region {x1,y1,x2,y2} in mm —
            # applied to the plotted SVG before any raster conversion, so it
            # works for svg/png/jpg alike and supersedes the alpha auto-crop.
            region = params.get("region")

            # Create plot controller
            plotter = pcbnew.PLOT_CONTROLLER(self.board)

            # Set up plot options
            plot_opts = plotter.GetPlotOptions()
            plot_opts.SetOutputDirectory(os.path.dirname(self.board.GetFileName()))
            plot_opts.SetScale(1)
            plot_opts.SetMirror(False)
            # Note: SetExcludeEdgeLayer() removed in KiCAD 9.0 - default behavior includes all layers
            plot_opts.SetPlotFrameRef(False)
            plot_opts.SetPlotValue(True)
            plot_opts.SetPlotReference(True)

            # N5: colour plot for the default preview so the stacked layers
            # stay distinguishable — in monochrome a filled B.Cu zone paints
            # the whole board black and everything plotted on top vanishes
            # into it.  Best-effort: falls back to monochrome when colour
            # settings aren't available headless.  Explicit-`layers` calls
            # keep the previous monochrome output.
            if not layers:
                try:
                    # "_builtin_default" is KiCad's classic theme and always
                    # exists; GetColorSettings requires the name explicitly
                    # under the KiCAD 10 SWIG bindings.
                    color_settings = pcbnew.GetSettingsManager().GetColorSettings(
                        "_builtin_default"
                    )
                    plot_opts.SetColorSettings(color_settings)
                    plot_opts.SetBlackAndWhite(False)
                except Exception as color_err:
                    logger.debug(f"Colour plot unavailable, using monochrome: {color_err}")

            # Plot to SVG first (for vector output)
            # Note: KiCAD 9.0 prepends the project name to the filename, so we use GetPlotFileName() to get the actual path
            plotter.OpenPlotfile("temp_view", pcbnew.PLOT_FORMAT_SVG, "Temporary View")

            # Plot specified layers, or the curated default preview set.
            # Note: In KiCAD 9.0, SetLayer() must be called before PlotLayer()
            if layers:
                for layer_name in layers:
                    layer_id = self.board.GetLayerID(layer_name)
                    if layer_id >= 0 and self.board.IsLayerEnabled(layer_id):
                        plotter.SetLayer(layer_id)
                        plotter.PlotLayer()
            else:
                # N5: default to a meaningful preview (back-to-front paint
                # order) instead of all enabled layers — see
                # _DEFAULT_VIEW_LAYERS for the rationale.
                for layer_name in self._DEFAULT_VIEW_LAYERS:
                    layer_id = self.board.GetLayerID(layer_name)
                    # Real pcbnew returns an int (negative = unknown layer);
                    # the isinstance guard keeps stubbed boards (unit tests)
                    # on the plot path like the old range() loop did.
                    if isinstance(layer_id, int) and layer_id < 0:
                        continue
                    if self.board.IsLayerEnabled(layer_id):
                        plotter.SetLayer(layer_id)
                        plotter.PlotLayer()

            # Get the actual filename that was created (includes project name prefix)
            temp_svg = plotter.GetPlotFileName()

            plotter.ClosePlot()

            if region:
                from ._svg_region import crop_svg_to_region

                try:
                    region_tuple = (
                        float(region["x1"]),
                        float(region["y1"]),
                        float(region["x2"]),
                        float(region["y2"]),
                    )
                except (KeyError, TypeError, ValueError):
                    os.remove(temp_svg)
                    return {
                        "success": False,
                        "message": "Invalid region: expected {x1, y1, x2, y2} in mm",
                    }
                with open(temp_svg, "r", encoding="utf-8") as f:
                    svg_text = f.read()
                cropped = crop_svg_to_region(svg_text, region_tuple)
                if cropped is None:
                    os.remove(temp_svg)
                    return {
                        "success": False,
                        "message": "Could not crop SVG to region",
                        "errorDetails": (
                            "Region must have x2 > x1 and y2 > y1, and the plotted "
                            "SVG must carry width/viewBox attributes"
                        ),
                    }
                with open(temp_svg, "w", encoding="utf-8") as f:
                    f.write(cropped)
                crop_to_board = False  # the region IS the crop

            # Determine output path next to the PCB file
            board_dir = os.path.dirname(self.board.GetFileName())
            board_name = os.path.splitext(os.path.basename(self.board.GetFileName()))[0]

            # --- Render to bytes (shared for both response modes) ---
            if format == "svg":
                with open(temp_svg, "rb") as f:
                    image_bytes = f.read()
                os.remove(temp_svg)
                mime_format = "svg"
            else:
                from cairosvg import svg2png

                image_bytes = svg2png(url=temp_svg, output_width=width, output_height=height)
                os.remove(temp_svg)

                # Auto-crop: KiCAD's plot canvas spans the full sheet; if
                # the Edge.Cuts outline occupies only a corner, the raster
                # has the actual board crammed into a fraction of the
                # output. Use PIL's alpha-channel bbox to crop to actual
                # content + margin. Works for any KiCAD SVG variant since
                # we crop on rendered pixels, not on SVG coordinates.
                if crop_to_board:
                    try:
                        img = Image.open(io.BytesIO(image_bytes))
                        bbox = img.getbbox()
                        if bbox is not None:
                            x0, y0, x1, y1 = bbox
                            m = crop_margin_px
                            x0 = max(0, x0 - m)
                            y0 = max(0, y0 - m)
                            x1 = min(img.width, x1 + m)
                            y1 = min(img.height, y1 + m)
                            if (x1 - x0) > 10 and (y1 - y0) > 10:
                                img = img.crop((x0, y0, x1, y1))
                                buf = io.BytesIO()
                                img.save(buf, format="PNG")
                                image_bytes = buf.getvalue()
                    except Exception as crop_err:
                        logger.debug(f"Auto-crop to board failed (continuing): {crop_err}")

                # Honor an explicitly requested width/height even after the
                # crop-to-board resize discarded it. The board is fitted
                # (aspect-preserving) within the requested box so callers that
                # ask for a specific size get one; the default (no size given)
                # keeps the native cropped-content resolution as before.
                # Region-crop mode renders straight to width/height already.
                if size_requested:
                    try:
                        img = Image.open(io.BytesIO(image_bytes))
                        fitted = _fit_within_box(img, width, height)
                        if fitted is not img:
                            buf = io.BytesIO()
                            fitted.save(buf, format="PNG")
                            image_bytes = buf.getvalue()
                    except Exception as resize_err:
                        logger.debug(f"Fit-to-requested-size failed (continuing): {resize_err}")

                if format == "jpg":
                    img = Image.open(io.BytesIO(image_bytes))
                    buf = io.BytesIO()
                    img.convert("RGB").save(buf, format="JPEG")
                    image_bytes = buf.getvalue()
                mime_format = format

            # --- Package response according to responseMode ---
            if response_mode == "file":
                output_path = os.path.join(board_dir, f"{board_name}_2d_view.{mime_format}")
                with open(output_path, "wb") as f:
                    f.write(image_bytes)
                return {
                    "success": True,
                    "format": mime_format,
                    "filePath": output_path,
                    "message": f"2D view saved to {output_path}",
                }
            else:
                # inline mode: base64-encode and return imageData
                image_b64 = base64.b64encode(image_bytes).decode("utf-8")
                return {
                    "success": True,
                    "format": mime_format,
                    "imageData": image_b64,
                }

        except Exception as e:
            logger.error(f"Error getting board 2D view: {str(e)}")
            return {
                "success": False,
                "message": "Failed to get board 2D view",
                "errorDetails": str(e),
            }

    def _get_layer_type_name(self, type_id: int) -> str:
        """Convert KiCAD layer type constant to name"""
        type_map = {
            pcbnew.LT_SIGNAL: "signal",
            pcbnew.LT_POWER: "power",
            pcbnew.LT_MIXED: "mixed",
            pcbnew.LT_JUMPER: "jumper",
        }
        # Note: LT_USER was removed in KiCAD 9.0
        return type_map.get(type_id, "unknown")

    def get_board_extents(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get the bounding box extents of the board"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            # Get unit preference (default to mm)
            unit = params.get("unit", "mm")
            scale = (
                1000000 if unit == "mm" else (25400 if unit == "mil" else 25400000)
            )  # mm, mil, or inch to nm

            # Get board bounding box
            board_box = self.board.GetBoardEdgesBoundingBox()

            # Extract bounds in nanometers, then convert
            left = board_box.GetLeft() / scale
            top = board_box.GetTop() / scale
            right = board_box.GetRight() / scale
            bottom = board_box.GetBottom() / scale
            width = board_box.GetWidth() / scale
            height = board_box.GetHeight() / scale

            # Get center point
            center_x = board_box.GetCenter().x / scale
            center_y = board_box.GetCenter().y / scale

            return {
                "success": True,
                "extents": {
                    "left": left,
                    "top": top,
                    "right": right,
                    "bottom": bottom,
                    "width": width,
                    "height": height,
                    "center": {"x": center_x, "y": center_y},
                    "unit": unit,
                },
            }

        except Exception as e:
            logger.error(f"Error getting board extents: {str(e)}")
            return {
                "success": False,
                "message": "Failed to get board extents",
                "errorDetails": str(e),
            }
