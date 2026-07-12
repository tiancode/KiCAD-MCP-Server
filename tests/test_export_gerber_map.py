"""Regression: export_gerber generateMapFile must forward --generate-map.

Phase C E2E found generateMapFile:true produced no map file — the flag was
accepted but never forwarded to kicad-cli's drill export.  These tests pin the
drill command builder that carries the flag.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.export._fabrication import build_drill_export_cmd


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
