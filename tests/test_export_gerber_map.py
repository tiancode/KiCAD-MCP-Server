"""Regression: export_gerber generateMapFile must forward --generate-map, must
produce the promised .gbrjob Gerber job file, and must return drill paths as
absolute paths (C3).

Phase C E2E found: (1) generateMapFile:true produced no map file — the flag was
accepted but never forwarded to kicad-cli's drill export; (2) the promised
.gbrjob was never emitted (PLOT_CONTROLLER's SetCreateGerberJobFile is a no-op
for the per-layer plot loop); (3) files.drill came back as bare basenames while
files.gerber/files.map were absolute — an inconsistent response shape.
"""

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

import pcbnew  # noqa: E402  (stubbed by tests/conftest.py)
from commands.export._fabrication import (  # noqa: E402
    FabricationMixin,
    build_drill_export_cmd,
    build_gerber_job_cmd,
)


@pytest.mark.unit
def test_drill_cmd_omits_map_by_default():
    cmd = build_drill_export_cmd("kicad-cli", "/out", "/b.kicad_pcb")
    assert "--generate-map" not in cmd
    assert "--map-format" not in cmd
    # board file stays last so kicad-cli parses it as the positional arg.
    assert cmd[-1] == "/b.kicad_pcb"
    assert cmd[:4] == ["kicad-cli", "pcb", "export", "drill"]


@pytest.mark.unit
def test_drill_cmd_forwards_generate_map_with_default_format():
    cmd = build_drill_export_cmd("kicad-cli", "/out", "/b.kicad_pcb", generate_map=True)
    assert "--generate-map" in cmd
    assert cmd[cmd.index("--map-format") + 1] == "gerberx2"
    assert cmd[-1] == "/b.kicad_pcb"


@pytest.mark.unit
def test_drill_cmd_honors_map_format_override():
    cmd = build_drill_export_cmd(
        "kicad-cli", "/out", "/b.kicad_pcb", generate_map=True, map_format="pdf"
    )
    assert cmd[cmd.index("--map-format") + 1] == "pdf"


@pytest.mark.unit
def test_drill_cmd_falls_back_on_bad_map_format():
    cmd = build_drill_export_cmd(
        "kicad-cli", "/out", "/b.kicad_pcb", generate_map=True, map_format="bogus"
    )
    assert cmd[cmd.index("--map-format") + 1] == "gerberx2"


# ---------------------------------------------------------------------------
# Gerber job (.gbrjob) command builder
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_gerber_job_cmd_uses_plural_gerbers_export():
    cmd = build_gerber_job_cmd("kicad-cli", "/out", "/b.kicad_pcb")
    # `gerbers` (plural) is the batch export that writes the .gbrjob.
    assert cmd[:4] == ["kicad-cli", "pcb", "export", "gerbers"]
    assert cmd[cmd.index("--output") + 1] == "/out"
    assert cmd[-1] == "/b.kicad_pcb"


# ---------------------------------------------------------------------------
# Full export_gerber: absolute drill paths + .gbrjob generation + degradation
# ---------------------------------------------------------------------------


class _GerberHost(FabricationMixin):
    def __init__(self, board_file: str, cli="kicad-cli"):
        self.board = MagicMock(name="board")
        self.board.GetFileName.return_value = board_file
        self.board.GetLayerID.side_effect = lambda n: {"F.Cu": 0}.get(n, -1)
        self._cli = cli

    def _find_kicad_cli(self):
        return self._cli


def _install_plotter(monkeypatch, output_dir: Path):
    """Stub PLOT_CONTROLLER so one requested layer writes one gerber on disk."""

    def _plot_name():
        output_dir.mkdir(parents=True, exist_ok=True)
        p = output_dir / "mini-F_Cu.gbr"
        p.write_text("G04 stub*")
        return str(p)

    plotter = MagicMock(name="plotter")
    plotter.GetPlotOptions.return_value = MagicMock(name="plot_opts")
    plotter.OpenPlotfile.return_value = True
    plotter.PlotLayer.return_value = True
    plotter.GetPlotFileName.side_effect = _plot_name
    monkeypatch.setattr(pcbnew, "PLOT_CONTROLLER", MagicMock(return_value=plotter))
    return plotter


@pytest.mark.unit
def test_export_gerber_absolute_drill_paths_and_gbrjob(tmp_path, monkeypatch):
    board_file = tmp_path / "mini.kicad_pcb"
    board_file.write_text("(kicad_pcb)")
    output_dir = tmp_path / "gerber"

    _install_plotter(monkeypatch, output_dir)

    import subprocess

    import utils.kicad_cli as kcli

    monkeypatch.setattr(kcli, "c_locale_env", lambda *a, **k: {})

    def _fake_run(cmd, **kwargs):
        if "drill" in cmd:
            (output_dir / "mini-PTH.drl").write_text("M48")
            (output_dir / "mini-NPTH.drl").write_text("M48")
        elif "gerbers" in cmd:
            (output_dir / "mini-job.gbrjob").write_text("{}")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    host = _GerberHost(str(board_file))
    result = host.export_gerber(
        {
            "outputDir": str(output_dir),
            "layers": ["F.Cu"],
            "generateDrillFiles": True,
            "generateMapFile": True,
        }
    )

    assert result["success"] is True, result

    # (C3.3) drill paths are absolute, matching files.gerber / files.map shape.
    drill = result["files"]["drill"]
    assert drill, "expected drill files"
    assert all(os.path.isabs(p) for p in drill)
    assert any(p.endswith("mini-PTH.drl") for p in drill)

    # (C3.1) the promised .gbrjob was produced and surfaced explicitly.
    assert result["gerberJobFile"] is not None
    assert result["gerberJobFile"].endswith(".gbrjob")
    assert os.path.isabs(result["gerberJobFile"])
    assert any(p.endswith(".gbrjob") for p in result["files"]["map"])

    # Produced cleanly — no degradation note.
    assert "note" not in result


@pytest.mark.unit
def test_export_gerber_gbrjob_degrades_without_cli(tmp_path, monkeypatch):
    board_file = tmp_path / "mini.kicad_pcb"
    board_file.write_text("(kicad_pcb)")
    output_dir = tmp_path / "gerber"

    _install_plotter(monkeypatch, output_dir)

    host = _GerberHost(str(board_file), cli=None)  # kicad-cli unavailable
    result = host.export_gerber(
        {
            "outputDir": str(output_dir),
            "layers": ["F.Cu"],
            "generateDrillFiles": True,
            "generateMapFile": True,
        }
    )

    # Gerbers still plotted (SWIG path), but the .gbrjob truthfully degrades.
    assert result["success"] is True, result
    assert result["gerberJobFile"] is None
    assert "note" in result
    assert "kicad-cli" in result["note"]
    # No cli means no drill files either — reported honestly (empty list).
    assert result["files"]["drill"] == []
