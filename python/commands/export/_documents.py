"""Document exports: PDF and SVG.

Split out of the former monolithic commands/export.py.
"""

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pcbnew

logger = logging.getLogger("kicad_interface")


class DocumentMixin:
    def export_pdf(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Export PDF files"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            output_path = params.get("outputPath")
            layers = params.get("layers", [])
            black_and_white = params.get("blackAndWhite", False)
            frame_reference = params.get("frameReference", True)
            page_size = params.get("pageSize", "A4")

            if not output_path:
                return {
                    "success": False,
                    "message": "Missing output path",
                    "errorDetails": "outputPath parameter is required",
                }

            # Create output directory if it doesn't exist
            output_path = os.path.abspath(os.path.expanduser(output_path))
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            # Create plot controller
            plotter = pcbnew.PLOT_CONTROLLER(self.board)

            # Set up plot options
            plot_opts = plotter.GetPlotOptions()
            plot_opts.SetOutputDirectory(os.path.dirname(output_path))
            plot_opts.SetFormat(pcbnew.PLOT_FORMAT_PDF)
            plot_opts.SetPlotFrameRef(frame_reference)
            plot_opts.SetPlotValue(True)
            plot_opts.SetPlotReference(True)
            plot_opts.SetBlackAndWhite(black_and_white)

            # KiCAD 9.0 page size handling:
            # - SetPageSettings() was removed in KiCAD 9.0
            # - SetA4Output(bool) forces A4 page size when True
            # - For other sizes, KiCAD auto-scales to fit the board
            # - SetAutoScale(True) enables automatic scaling to fit page
            if page_size == "A4":
                plot_opts.SetA4Output(True)
            else:
                # For non-A4 sizes, disable A4 forcing and use auto-scale
                plot_opts.SetA4Output(False)
                plot_opts.SetAutoScale(True)
                # Note: KiCAD 9.0 doesn't support explicit page size selection
                # for formats other than A4. The PDF will auto-scale to fit.
                logger.warning(
                    f"Page size '{page_size}' requested, but KiCAD 9.0 only supports A4 explicitly. Using auto-scale instead."
                )

            # Open plot for writing
            # Note: For PDF, all layers are combined into a single file
            # KiCAD prepends the board filename to the plot file name
            base_name = os.path.basename(output_path).replace(".pdf", "")
            plotter.OpenPlotfile(base_name, pcbnew.PLOT_FORMAT_PDF, "")

            # Plot specified layers or all enabled layers
            plotted_layers = []
            if layers:
                for layer_name in layers:
                    layer_id = self.board.GetLayerID(layer_name)
                    if layer_id >= 0:
                        plotter.SetLayer(layer_id)
                        plotter.PlotLayer()
                        plotted_layers.append(layer_name)
            else:
                for layer_id in range(pcbnew.PCB_LAYER_ID_COUNT):
                    if self.board.IsLayerEnabled(layer_id):
                        layer_name = self.board.GetLayerName(layer_id)
                        plotter.SetLayer(layer_id)
                        plotter.PlotLayer()
                        plotted_layers.append(layer_name)

            # Close the plot file to finalize the PDF
            plotter.ClosePlot()

            # KiCAD automatically prepends the board name to the output file
            # Get the actual output filename that was created
            board_name = os.path.splitext(os.path.basename(self.board.GetFileName()))[0]
            actual_filename = f"{board_name}-{base_name}.pdf"
            actual_output_path = os.path.join(os.path.dirname(output_path), actual_filename)

            # Verify file actually landed on disk
            if not os.path.exists(actual_output_path):
                # Try the path KiCAD's plotter reports, in case naming changed
                reported = plotter.GetPlotFileName() if hasattr(plotter, "GetPlotFileName") else ""
                if reported and os.path.exists(reported):
                    actual_output_path = reported
                else:
                    return {
                        "success": False,
                        "message": "PDF export reported success but no file on disk",
                        "errorDetails": f"Expected file at {actual_output_path}",
                        "requestedPath": output_path,
                    }

            try:
                size_bytes = os.path.getsize(actual_output_path)
            except OSError:
                size_bytes = 0
            if size_bytes == 0:
                return {
                    "success": False,
                    "message": "PDF export produced an empty file",
                    "errorDetails": f"{actual_output_path} is zero bytes",
                }

            return {
                "success": True,
                "message": "Exported PDF file",
                "file": {
                    "path": actual_output_path,
                    "requestedPath": output_path,
                    "layers": plotted_layers,
                    "size_bytes": size_bytes,
                    "pageSize": page_size if page_size == "A4" else "auto-scaled",
                },
            }

        except Exception as e:
            logger.error(f"Error exporting PDF file: {str(e)}")
            return {
                "success": False,
                "message": "Failed to export PDF file",
                "errorDetails": str(e),
            }

    def export_svg(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Export SVG files"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            output_path = params.get("outputPath")
            layers = params.get("layers", [])
            black_and_white = params.get("blackAndWhite", False)
            include_components = params.get("includeComponents", True)

            if not output_path:
                return {
                    "success": False,
                    "message": "Missing output path",
                    "errorDetails": "outputPath parameter is required",
                }

            # Create output directory if it doesn't exist
            output_path = os.path.abspath(os.path.expanduser(output_path))
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            # Create plot controller
            plotter = pcbnew.PLOT_CONTROLLER(self.board)

            # Set up plot options
            plot_opts = plotter.GetPlotOptions()
            plot_opts.SetOutputDirectory(os.path.dirname(output_path))
            plot_opts.SetFormat(pcbnew.PLOT_FORMAT_SVG)
            plot_opts.SetPlotValue(include_components)
            plot_opts.SetPlotReference(include_components)
            plot_opts.SetBlackAndWhite(black_and_white)

            # Build list of (layer_name, layer_id) to plot
            target_layers: List[Tuple[str, int]] = []
            if layers:
                for layer_name in layers:
                    layer_id = self.board.GetLayerID(layer_name)
                    if layer_id < 0:
                        return {
                            "success": False,
                            "message": "Unknown layer",
                            "errorDetails": f"Layer '{layer_name}' not found on this board",
                        }
                    target_layers.append((layer_name, layer_id))
            else:
                for layer_id in range(pcbnew.PCB_LAYER_ID_COUNT):
                    if self.board.IsLayerEnabled(layer_id):
                        target_layers.append((self.board.GetLayerName(layer_id), layer_id))

            written_files: List[Dict[str, Any]] = []
            missing_layers: List[Dict[str, Any]] = []
            for layer_name, layer_id in target_layers:
                plotter.SetLayer(layer_id)
                suffix = layer_name.replace(".", "_")
                opened = plotter.OpenPlotfile(suffix, pcbnew.PLOT_FORMAT_SVG, layer_name)
                if not opened:
                    missing_layers.append(
                        {"layer": layer_name, "reason": "OpenPlotfile returned False"}
                    )
                    continue
                plot_ok = plotter.PlotLayer()
                produced = plotter.GetPlotFileName()
                plotter.ClosePlot()
                if not plot_ok or not produced or not os.path.exists(produced):
                    missing_layers.append(
                        {
                            "layer": layer_name,
                            "reason": (
                                "PlotLayer returned False"
                                if not plot_ok
                                else f"file not written to {produced}"
                            ),
                        }
                    )
                    continue
                try:
                    size_bytes = os.path.getsize(produced)
                except OSError:
                    size_bytes = 0
                written_files.append(
                    {"layer": layer_name, "path": produced, "size_bytes": size_bytes}
                )

            requested_count = len(target_layers)
            if not written_files and requested_count > 0:
                return {
                    "success": False,
                    "message": f"SVG export wrote 0 of {requested_count} requested layers",
                    "errorDetails": "No SVG files were created on disk",
                    "missing": missing_layers,
                }

            payload = {
                "success": len(missing_layers) == 0,
                "message": (
                    f"Exported {len(written_files)} of {requested_count} SVG layers"
                    if missing_layers
                    else "Exported SVG file"
                ),
                "file": {"layers": written_files, "outputDir": os.path.dirname(output_path)},
            }
            if missing_layers:
                payload["missing"] = missing_layers
            return payload

        except Exception as e:
            logger.error(f"Error exporting SVG file: {str(e)}")
            return {
                "success": False,
                "message": "Failed to export SVG file",
                "errorDetails": str(e),
            }
