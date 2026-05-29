"""
Export command implementations for KiCAD interface
"""

import base64
import logging
import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pcbnew

logger = logging.getLogger("kicad_interface")


class ExportCommands:
    """Handles export-related KiCAD operations"""

    def __init__(self, board: Optional[pcbnew.BOARD] = None):
        """Initialize with optional board instance"""
        self.board = board

    def export_gerber(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Export Gerber files"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            output_dir = params.get("outputDir")
            layers = params.get("layers", [])
            use_protel_extensions = params.get("useProtelExtensions", False)
            generate_drill_files = params.get("generateDrillFiles", True)
            generate_map_file = params.get("generateMapFile", False)
            use_aux_origin = params.get("useAuxOrigin", False)

            if not output_dir:
                return {
                    "success": False,
                    "message": "Missing output directory",
                    "errorDetails": "outputDir parameter is required",
                }

            # Create output directory if it doesn't exist
            output_dir = os.path.abspath(os.path.expanduser(output_dir))
            os.makedirs(output_dir, exist_ok=True)

            # Create plot controller
            plotter = pcbnew.PLOT_CONTROLLER(self.board)

            # Set up plot options
            plot_opts = plotter.GetPlotOptions()
            plot_opts.SetOutputDirectory(output_dir)
            plot_opts.SetFormat(pcbnew.PLOT_FORMAT_GERBER)
            plot_opts.SetUseGerberProtelExtensions(use_protel_extensions)
            plot_opts.SetUseAuxOrigin(use_aux_origin)
            plot_opts.SetCreateGerberJobFile(generate_map_file)
            plot_opts.SetSubtractMaskFromSilk(True)

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

            # PLOT_CONTROLLER requires OpenPlotfile() before PlotLayer() — without it
            # PlotLayer() silently returns False and no file is written. After plotting,
            # GetPlotFileName() returns the actual path KiCAD wrote.
            written_files: List[Dict[str, Any]] = []
            missing_layers: List[Dict[str, Any]] = []
            for layer_name, layer_id in target_layers:
                plotter.SetLayer(layer_id)
                # Safe-ify layer name for filename suffix (e.g. "F.Cu" -> "F_Cu")
                suffix = layer_name.replace(".", "_")
                opened = plotter.OpenPlotfile(suffix, pcbnew.PLOT_FORMAT_GERBER, layer_name)
                if not opened:
                    missing_layers.append(
                        {"layer": layer_name, "reason": "OpenPlotfile returned False"}
                    )
                    continue
                plot_ok = plotter.PlotLayer()
                expected_path = plotter.GetPlotFileName()
                plotter.ClosePlot()
                if not plot_ok or not expected_path or not os.path.exists(expected_path):
                    missing_layers.append(
                        {
                            "layer": layer_name,
                            "reason": (
                                "PlotLayer returned False"
                                if not plot_ok
                                else f"file not written to {expected_path}"
                            ),
                        }
                    )
                    continue
                try:
                    size_bytes = os.path.getsize(expected_path)
                except OSError:
                    size_bytes = 0
                written_files.append(
                    {"layer": layer_name, "path": expected_path, "size_bytes": size_bytes}
                )

            # Generate drill files if requested
            drill_files = []
            if generate_drill_files:
                # KiCAD 9.0: Use kicad-cli for more reliable drill file generation
                # The Python API's EXCELLON_WRITER.SetOptions() signature changed
                board_file = self.board.GetFileName()
                kicad_cli = self._find_kicad_cli()

                if kicad_cli and board_file and os.path.exists(board_file):
                    import subprocess

                    # Generate drill files using kicad-cli
                    cmd = [
                        kicad_cli,
                        "pcb",
                        "export",
                        "drill",
                        "--output",
                        output_dir,
                        "--format",
                        "excellon",
                        "--drill-origin",
                        "absolute",
                        "--excellon-separate-th",  # Separate plated/non-plated
                        board_file,
                    ]

                    try:
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                        if result.returncode == 0:
                            # Get list of generated drill files
                            for file in os.listdir(output_dir):
                                if file.endswith((".drl", ".cnc")):
                                    drill_files.append(file)
                        else:
                            logger.warning(f"Drill file generation failed: {result.stderr}")
                    except Exception as drill_error:
                        logger.warning(f"Could not generate drill files: {str(drill_error)}")
                else:
                    logger.warning("kicad-cli not available for drill file generation")

            # DEV MODE: copy MCP server log into project folder for later analysis
            if os.environ.get("KICAD_MCP_DEV") == "1":
                try:
                    self._dev_copy_mcp_log(output_dir)
                except Exception as dev_err:
                    logger.warning(f"[DEV] Could not copy MCP log: {dev_err}")

            # Verify gerber job file if requested
            job_files: List[str] = []
            if generate_map_file:
                for file in os.listdir(output_dir):
                    if file.endswith(".gbrjob"):
                        job_files.append(os.path.join(output_dir, file))

            requested_count = len(target_layers)
            written_count = len(written_files)

            if written_count == 0 and requested_count > 0:
                return {
                    "success": False,
                    "message": f"Gerber export wrote 0 of {requested_count} requested layers",
                    "errorDetails": "No gerber files were created on disk",
                    "missing": missing_layers,
                    "outputDir": output_dir,
                }

            payload = {
                "success": len(missing_layers) == 0,
                "message": (
                    f"Exported {written_count} of {requested_count} gerber layers"
                    if missing_layers
                    else "Exported Gerber files"
                ),
                "files": {
                    "gerber": written_files,
                    "drill": drill_files,
                    "map": job_files,
                },
                "outputDir": output_dir,
            }
            if missing_layers:
                payload["missing"] = missing_layers
            return payload

        except Exception as e:
            logger.error(f"Error exporting Gerber files: {str(e)}")
            return {
                "success": False,
                "message": "Failed to export Gerber files",
                "errorDetails": str(e),
            }

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

    def export_3d(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Export 3D model files using kicad-cli (KiCAD 9.0 compatible)"""
        import platform
        import shutil
        import subprocess

        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            output_path = params.get("outputPath")
            format = params.get("format", "STEP")
            include_components = params.get("includeComponents", True)
            include_copper = params.get("includeCopper", True)
            include_solder_mask = params.get("includeSolderMask", True)
            include_silkscreen = params.get("includeSilkscreen", True)

            if not output_path:
                return {
                    "success": False,
                    "message": "Missing output path",
                    "errorDetails": "outputPath parameter is required",
                }

            # Get board file path
            board_file = self.board.GetFileName()
            if not board_file or not os.path.exists(board_file):
                return {
                    "success": False,
                    "message": "Board file not found",
                    "errorDetails": "Board must be saved before exporting 3D models",
                }

            # Create output directory if it doesn't exist
            output_path = os.path.abspath(os.path.expanduser(output_path))
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            # Find kicad-cli executable
            kicad_cli = self._find_kicad_cli()
            if not kicad_cli:
                return {
                    "success": False,
                    "message": "kicad-cli not found",
                    "errorDetails": "KiCAD CLI tool not found. Install KiCAD 8.0+ or set PATH.",
                }

            # Build command based on format
            format_upper = format.upper()

            if format_upper == "STEP":
                cmd = [
                    kicad_cli,
                    "pcb",
                    "export",
                    "step",
                    "--output",
                    output_path,
                    "--force",  # Overwrite existing file
                ]

                # Add options based on parameters
                if not include_components:
                    cmd.append("--no-components")
                if include_copper:
                    cmd.extend(["--include-tracks", "--include-pads", "--include-zones"])
                if include_silkscreen:
                    cmd.append("--include-silkscreen")
                if include_solder_mask:
                    cmd.append("--include-soldermask")

                cmd.append(board_file)

            elif format_upper == "VRML":
                cmd = [
                    kicad_cli,
                    "pcb",
                    "export",
                    "vrml",
                    "--output",
                    output_path,
                    "--units",
                    "mm",  # Use mm for consistency
                    "--force",
                ]

                if not include_components:
                    # Note: VRML export doesn't have a direct --no-components flag
                    # The models will be included by default, but can be controlled via 3D settings
                    pass

                cmd.append(board_file)

            else:
                return {
                    "success": False,
                    "message": "Unsupported format",
                    "errorDetails": f"Format {format} is not supported. Use 'STEP' or 'VRML'.",
                }

            # Execute kicad-cli command
            logger.info(f"Running 3D export command: {' '.join(cmd)}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout for 3D export
            )

            if result.returncode != 0:
                logger.error(f"3D export command failed: {result.stderr}")
                return {
                    "success": False,
                    "message": "3D export command failed",
                    "errorDetails": result.stderr,
                }

            if not os.path.exists(output_path):
                return {
                    "success": False,
                    "message": "3D export reported success but no file on disk",
                    "errorDetails": (
                        f"kicad-cli exit 0 but {output_path} is missing. "
                        f"stderr: {result.stderr.strip() or '(empty)'}"
                    ),
                }
            try:
                size_bytes = os.path.getsize(output_path)
            except OSError:
                size_bytes = 0
            if size_bytes == 0:
                return {
                    "success": False,
                    "message": "3D export produced an empty file",
                    "errorDetails": f"{output_path} is zero bytes",
                }

            return {
                "success": True,
                "message": f"Exported {format_upper} file",
                "file": {"path": output_path, "format": format_upper, "size_bytes": size_bytes},
            }

        except subprocess.TimeoutExpired:
            logger.error("3D export command timed out")
            return {
                "success": False,
                "message": "3D export timed out",
                "errorDetails": "Export took longer than 5 minutes",
            }
        except Exception as e:
            logger.error(f"Error exporting 3D model: {str(e)}")
            return {
                "success": False,
                "message": "Failed to export 3D model",
                "errorDetails": str(e),
            }

    def export_position_file(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Export a component placement / pick-and-place file via kicad-cli.

        Wraps ``kicad-cli pcb export pos``. Format CSV|ASCII, units mm/mil/inch,
        side top/bottom/both.
        """
        import subprocess

        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            output_path = params.get("outputPath")
            fmt = str(params.get("format", "CSV")).lower()
            units = str(params.get("units", "mm")).lower()
            side = str(params.get("side", "both")).lower()

            if not output_path:
                return {
                    "success": False,
                    "message": "Missing output path",
                    "errorDetails": "outputPath parameter is required",
                }

            board_file = self.board.GetFileName()
            if not board_file or not os.path.exists(board_file):
                return {
                    "success": False,
                    "message": "Board file not found",
                    "errorDetails": "Board must be saved before exporting a position file",
                }

            output_path = os.path.abspath(os.path.expanduser(output_path))
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            kicad_cli = self._find_kicad_cli()
            if not kicad_cli:
                return {
                    "success": False,
                    "message": "kicad-cli not found",
                    "errorDetails": "KiCAD CLI tool not found. Install KiCAD 8.0+ or set PATH.",
                }

            # Map MCP enums onto kicad-cli's vocabulary. kicad-cli pos only
            # speaks mm/in, front/back/both, csv/ascii/gerber.
            cli_format = "ascii" if fmt == "ascii" else ("gerber" if fmt == "gerber" else "csv")
            cli_units = "in" if units in ("in", "inch", "mil") else "mm"
            cli_side = {"top": "front", "bottom": "back", "both": "both"}.get(side, "both")

            cmd = [
                kicad_cli,
                "pcb",
                "export",
                "pos",
                "--output",
                output_path,
                "--format",
                cli_format,
                "--units",
                cli_units,
                "--side",
                cli_side,
                board_file,
            ]

            logger.info(f"Running position-file export command: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

            if result.returncode != 0:
                logger.error(f"Position-file export failed: {result.stderr}")
                return {
                    "success": False,
                    "message": "Position-file export command failed",
                    "errorDetails": result.stderr.strip() or "kicad-cli returned non-zero",
                }

            if not os.path.exists(output_path):
                return {
                    "success": False,
                    "message": "Export reported success but no file on disk",
                    "errorDetails": (
                        f"kicad-cli exit 0 but {output_path} is missing. "
                        f"stderr: {result.stderr.strip() or '(empty)'}"
                    ),
                }

            try:
                size_bytes = os.path.getsize(output_path)
            except OSError:
                size_bytes = 0

            return {
                "success": True,
                "message": "Exported position file",
                "file": {
                    "path": output_path,
                    "format": cli_format,
                    "units": cli_units,
                    "side": cli_side,
                    "size_bytes": size_bytes,
                },
            }

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "message": "Position-file export timed out",
                "errorDetails": "Export took longer than 2 minutes",
            }
        except Exception as e:
            logger.error(f"Error exporting position file: {str(e)}")
            return {
                "success": False,
                "message": "Failed to export position file",
                "errorDetails": str(e),
            }

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

            return {
                "success": True,
                "message": f"Exported BOM to {format}",
                "file": {
                    "path": output_path,
                    "format": format,
                    "componentCount": len(components),
                },
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

        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=components[0].keys())
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
        for key in components[0].keys():
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

    def _find_kicad_cli(self) -> Optional[str]:
        """Find kicad-cli executable in system PATH or common locations

        Returns:
            Path to kicad-cli executable, or None if not found
        """
        import platform
        import shutil

        # Try system PATH first
        cli_path = shutil.which("kicad-cli")
        if cli_path:
            return cli_path

        # Try platform-specific default locations
        system = platform.system()

        if system == "Windows":
            possible_paths = [
                r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
                r"C:\Program Files\KiCad\8.0\bin\kicad-cli.exe",
                r"C:\Program Files (x86)\KiCad\9.0\bin\kicad-cli.exe",
                r"C:\Program Files (x86)\KiCad\8.0\bin\kicad-cli.exe",
            ]
        elif system == "Darwin":  # macOS
            possible_paths = [
                "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
                "/usr/local/bin/kicad-cli",
            ]
        else:  # Linux
            possible_paths = [
                "/usr/bin/kicad-cli",
                "/usr/local/bin/kicad-cli",
            ]

        for path in possible_paths:
            if os.path.exists(path):
                return path

        return None

    def _dev_copy_mcp_log(self, output_dir: str) -> None:
        """DEV MODE: Copy the MCP server log for the current session into the project folder.

        Activated by env var KICAD_MCP_DEV=1.
        The log is placed alongside the Gerber output as:
            <project_dir>/mcp_log_<YYYYMMDD_HHMMSS>.txt

        Only lines from the current server session (today's date) are included
        to keep the file focused on the relevant run.
        """
        import platform

        # Resolve Claude log path per platform
        system = platform.system()
        if system == "Windows":
            log_dir = os.path.join(os.environ.get("APPDATA", ""), "Claude", "logs")
        elif system == "Darwin":
            log_dir = os.path.expanduser("~/Library/Logs/Claude")
        else:
            log_dir = os.path.expanduser("~/.config/Claude/logs")

        log_src = os.path.join(log_dir, "mcp-server-kicad.log")
        if not os.path.exists(log_src):
            logger.warning(f"[DEV] MCP log not found at: {log_src}")
            return

        # Project dir = parent of outputDir (the Gerber subfolder)
        project_dir = os.path.dirname(output_dir)

        # Extract only lines from the current session start (find last "Initializing server")
        with open(log_src, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        # Find last occurrence of server start so we get only the current run
        session_start = 0
        for i, line in enumerate(all_lines):
            if "Initializing server" in line:
                session_start = i

        session_lines = all_lines[session_start:]

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        from pathlib import Path

        logs_dir = Path(project_dir) / "logs"
        logs_dir.mkdir(exist_ok=True)
        dest = str(logs_dir / f"mcp_log_{timestamp}.txt")
        with open(dest, "w", encoding="utf-8") as f:
            f.writelines(session_lines)

        logger.info(f"[DEV] MCP session log saved to: {dest} ({len(session_lines)} lines)")
