"""Document exports: PDF and SVG.

Split out of the former monolithic commands/export.py.
"""

import logging
import os
from typing import Any, Dict, List, Tuple

import pcbnew
from utils.responses import failed, no_board_loaded

logger = logging.getLogger("kicad_interface")


class DocumentMixin:
    def export_pdf(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Export PDF files"""
        try:
            if not self.board:
                return no_board_loaded()

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

            # KiCAD's plotter prepends the board name to the requested base
            # name (requesting "foo.pdf" yields "<board>-foo.pdf"). Locate the
            # file KiCAD actually produced, then rename it to the exact
            # requested path so the file lands where the caller asked —
            # consistent with export_gerber / export_bom / export_position_file.
            board_name = os.path.splitext(os.path.basename(self.board.GetFileName()))[0]
            out_dir = os.path.dirname(output_path)

            produced_candidates = [os.path.join(out_dir, f"{board_name}-{base_name}.pdf")]
            # Try the path KiCAD's plotter reports, in case naming changed.
            reported = plotter.GetPlotFileName() if hasattr(plotter, "GetPlotFileName") else ""
            if isinstance(reported, str) and reported:
                produced_candidates.append(os.path.abspath(reported))
            # KiCAD may already have honoured the requested name directly.
            produced_candidates.append(output_path)

            produced_path = next((p for p in produced_candidates if p and os.path.exists(p)), None)
            if produced_path is None:
                return {
                    "success": False,
                    "message": "PDF export reported success but no file on disk",
                    "errorDetails": f"Expected file at {produced_candidates[0]}",
                    "requestedPath": output_path,
                }

            # Rename the produced file to the literal requested path.
            if os.path.abspath(produced_path) != os.path.abspath(output_path):
                os.replace(produced_path, output_path)
            actual_output_path = output_path

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
            return failed("Failed to export PDF file", e)
