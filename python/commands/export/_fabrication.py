"""Fabrication exports: gerber, position file, 3D model.

Split out of the former monolithic commands/export.py.
"""

import logging
import os
from typing import Any, Dict, List, Tuple

import pcbnew
from utils.responses import failed, no_board_loaded

logger = logging.getLogger("kicad_interface")

# kicad-cli's drill-map formats. gerberx2 keeps the map as a Gerber file that
# lands next to the drill/gerber set (fab-friendly); pdf/ps/dxf/svg are also
# accepted. Anything else falls back to gerberx2.
_DRILL_MAP_FORMATS = ("gerberx2", "pdf", "postscript", "dxf", "svg")
_DEFAULT_DRILL_MAP_FORMAT = "gerberx2"
# kicad-cli names drill-map files "<name>-<PTH|NPTH>-drl_map.<ext>" — the
# "drl_map" token is the stable marker we glob for regardless of format.
_DRILL_MAP_MARKER = "drl_map"


def _normalize_map_format(map_format: Any) -> str:
    fmt = str(map_format or _DEFAULT_DRILL_MAP_FORMAT).strip().lower()
    return fmt if fmt in _DRILL_MAP_FORMATS else _DEFAULT_DRILL_MAP_FORMAT


def build_drill_export_cmd(
    kicad_cli: str,
    output_dir: str,
    board_file: str,
    *,
    generate_map: bool = False,
    map_format: str = _DEFAULT_DRILL_MAP_FORMAT,
) -> List[str]:
    """Build the ``kicad-cli pcb export drill`` command.

    Isolated so tests can assert the flags without a real board/pcbnew.  When
    ``generate_map`` is set, ``--generate-map`` + ``--map-format`` are appended
    so a drill map is written alongside the ``.drl`` files (previously the
    ``generateMapFile`` flag was accepted but never forwarded, so no map
    appeared on disk).
    """
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
    ]
    if generate_map:
        cmd += ["--generate-map", "--map-format", _normalize_map_format(map_format)]
    cmd.append(board_file)
    return cmd


def build_gerber_job_cmd(kicad_cli: str, output_dir: str, board_file: str) -> List[str]:
    """Build ``kicad-cli pcb export gerbers`` — the batch export that writes the
    ``.gbrjob`` Gerber job file next to the plotted gerbers.

    ``PLOT_CONTROLLER.SetCreateGerberJobFile(True)`` does NOT emit a job file
    for the layer-by-layer ``OpenPlotfile``/``PlotLayer`` loop the SWIG export
    uses, so the promised ``.gbrjob`` never appeared.  This supplementary
    call (used only when a map/job set is requested) produces it truthfully.
    Isolated so tests can assert the flags without a real board/pcbnew.
    """
    return [
        kicad_cli,
        "pcb",
        "export",
        "gerbers",
        "--output",
        output_dir,
        board_file,
    ]


class FabricationMixin:
    def export_gerber(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Export Gerber files"""
        try:
            if not self.board:
                return no_board_loaded()

            output_dir = params.get("outputDir")
            layers = params.get("layers", [])
            use_protel_extensions = params.get("useProtelExtensions", False)
            generate_drill_files = params.get("generateDrillFiles", True)
            generate_map_file = params.get("generateMapFile", False)
            map_format = _normalize_map_format(params.get("mapFormat"))
            use_aux_origin = params.get("useAuxOrigin", False)

            if not output_dir:
                return {
                    "success": False,
                    "message": "Missing output directory",
                    "errorDetails": "outputDir parameter is required",
                }

            output_dir = os.path.abspath(os.path.expanduser(output_dir))
            os.makedirs(output_dir, exist_ok=True)

            plotter = pcbnew.PLOT_CONTROLLER(self.board)

            plot_opts = plotter.GetPlotOptions()
            plot_opts.SetOutputDirectory(output_dir)
            plot_opts.SetFormat(pcbnew.PLOT_FORMAT_GERBER)
            plot_opts.SetUseGerberProtelExtensions(use_protel_extensions)
            plot_opts.SetUseAuxOrigin(use_aux_origin)
            plot_opts.SetCreateGerberJobFile(generate_map_file)
            plot_opts.SetSubtractMaskFromSilk(True)

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

            # kicad-cli + on-disk board file are needed both for drill export
            # and for the supplementary .gbrjob generation below — resolve once.
            board_file = self.board.GetFileName()
            kicad_cli = self._find_kicad_cli()
            board_on_disk = bool(board_file and os.path.exists(board_file))

            drill_files: List[str] = []
            if generate_drill_files:
                # KiCAD 9.0: Use kicad-cli for more reliable drill file generation
                # The Python API's EXCELLON_WRITER.SetOptions() signature changed
                if kicad_cli and board_on_disk:
                    import subprocess

                    # Generate drill files (+ optional drill map) using kicad-cli
                    cmd = build_drill_export_cmd(
                        kicad_cli,
                        output_dir,
                        board_file,
                        generate_map=generate_map_file,
                        map_format=map_format,
                    )

                    try:
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                        if result.returncode == 0:
                            # Get list of generated drill files (exclude the
                            # drill-map files, which are collected separately).
                            # Return ABSOLUTE paths so files.drill matches the
                            # shape of files.gerber[].path / files.map[] (the E2E
                            # run saw bare basenames here — inconsistent response).
                            for file in os.listdir(output_dir):
                                if (
                                    file.endswith((".drl", ".cnc"))
                                    and _DRILL_MAP_MARKER not in file
                                ):
                                    drill_files.append(os.path.join(output_dir, file))
                            drill_files.sort()
                        else:
                            logger.warning(f"Drill file generation failed: {result.stderr}")
                    except Exception as drill_error:
                        logger.warning(f"Could not generate drill files: {str(drill_error)}")
                else:
                    logger.warning("kicad-cli not available for drill file generation")

            # Generate the Gerber job file (.gbrjob).  PLOT_CONTROLLER's
            # SetCreateGerberJobFile flag above does not emit a job file for the
            # per-layer plot loop, so the schema's promised .gbrjob never
            # appeared.  Produce it truthfully via a supplementary
            # `kicad-cli pcb export gerbers` when a map/job set is requested,
            # degrading gracefully (with a truthful note) if kicad-cli or the
            # on-disk board is unavailable.
            gerber_job_note: str = ""
            if generate_map_file:
                if kicad_cli and board_on_disk:
                    import subprocess

                    from utils.kicad_cli import c_locale_env

                    job_cmd = build_gerber_job_cmd(kicad_cli, output_dir, board_file)
                    try:
                        job_result = subprocess.run(
                            job_cmd,
                            capture_output=True,
                            text=True,
                            timeout=120,
                            env=c_locale_env(),
                        )
                        if job_result.returncode != 0:
                            gerber_job_note = (
                                "Gerber job file (.gbrjob) could not be generated "
                                f"(kicad-cli exit {job_result.returncode}): "
                                f"{job_result.stderr.strip() or '(no stderr)'}"
                            )
                            logger.warning(gerber_job_note)
                    except Exception as job_error:
                        gerber_job_note = (
                            f"Gerber job file (.gbrjob) generation failed: {job_error}"
                        )
                        logger.warning(gerber_job_note)
                elif not kicad_cli:
                    gerber_job_note = (
                        "Gerber job file (.gbrjob) not generated: kicad-cli not found "
                        "(install KiCAD 8.0+ or set PATH)"
                    )
                    logger.warning(gerber_job_note)
                else:
                    gerber_job_note = (
                        "Gerber job file (.gbrjob) not generated: board is not saved "
                        "to disk (save the board first)"
                    )
                    logger.warning(gerber_job_note)

            # DEV MODE: copy MCP server log into project folder for later analysis
            if os.environ.get("KICAD_MCP_DEV") == "1":
                try:
                    self._dev_copy_mcp_log(output_dir)
                except Exception as dev_err:
                    logger.warning(f"[DEV] Could not copy MCP log: {dev_err}")

            # Collect map/job files when requested: the gerber job (.gbrjob)
            # plus the drill-map files kicad-cli just wrote next to the drill
            # files ("<name>-<PTH|NPTH>-drl_map.<ext>").
            map_files: List[str] = []
            gerber_job_file: Any = None
            if generate_map_file:
                for file in os.listdir(output_dir):
                    if file.endswith(".gbrjob") or _DRILL_MAP_MARKER in file:
                        map_files.append(os.path.join(output_dir, file))
                map_files.sort()
                gerber_job_file = next((m for m in map_files if m.endswith(".gbrjob")), None)

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
                    "map": map_files,
                },
                "outputDir": output_dir,
            }
            if generate_map_file:
                # Explicit .gbrjob path (None when it couldn't be produced) so
                # callers don't have to grep files.map for it.
                payload["gerberJobFile"] = gerber_job_file
            if gerber_job_note:
                payload["note"] = gerber_job_note
            if missing_layers:
                payload["missing"] = missing_layers
            return payload

        except Exception as e:
            logger.error(f"Error exporting Gerber files: {str(e)}")
            return failed("Failed to export Gerber files", e)

    def export_3d(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Export 3D model files using kicad-cli (KiCAD 9.0 compatible)"""
        import subprocess

        try:
            if not self.board:
                return no_board_loaded()

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

            board_file = self.board.GetFileName()
            if not board_file or not os.path.exists(board_file):
                return {
                    "success": False,
                    "message": "Board file not found",
                    "errorDetails": "Board must be saved before exporting 3D models",
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
                # User error, not an internal fault: the MCP schema only offers
                # STEP/VRML now, so an unsupported format is a bad request and
                # must carry a validation errorCode (not INTERNAL_ERROR).
                return {
                    "success": False,
                    "message": "Unsupported format",
                    "errorCode": "UNSUPPORTED_FORMAT",
                    "errorDetails": f"Format {format} is not supported. Use 'STEP' or 'VRML'.",
                }

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
            return failed("Failed to export 3D model", e)

    def export_position_file(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Export a component placement / pick-and-place file via kicad-cli.

        Wraps ``kicad-cli pcb export pos``. Format CSV|ASCII, units mm/mil/inch,
        side top/bottom/both.
        """
        import subprocess

        try:
            if not self.board:
                return no_board_loaded()

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
            return failed("Failed to export position file", e)
