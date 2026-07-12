"""export_pdf must land the file at the literal requested path.

KiCAD's plotter prepends the board name to the requested base name
("led_flasher_pcb.pdf" -> "led_flasher-led_flasher_pcb.pdf"). The handler
renames the produced file back to the exact requested path.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.export import ExportCommands  # noqa: E402


@pytest.mark.unit
def test_export_pdf_renames_to_requested_path(tmp_path):
    board = MagicMock(name="board")
    board.GetFileName.return_value = "/proj/led_flasher.kicad_pcb"
    board.GetLayerID.return_value = 0  # any valid (>= 0) layer id

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    requested = out_dir / "led_flasher_pcb.pdf"

    # The plotter (mocked) doesn't actually write; simulate the board-name
    # prefixed file it would have produced.
    prefixed = out_dir / "led_flasher-led_flasher_pcb.pdf"
    prefixed.write_bytes(b"%PDF-1.5 fake\n")

    # Pass an explicit layer to avoid the range(PCB_LAYER_ID_COUNT) all-layers
    # path, which needs a real int the pcbnew stub doesn't provide.
    result = ExportCommands(board).export_pdf({"outputPath": str(requested), "layers": ["F.Cu"]})

    assert result["success"] is True, result
    # File lands at exactly the requested path; the prefixed name is gone.
    assert requested.exists()
    assert not prefixed.exists()
    # path and requestedPath are now equal and both point at the requested file.
    assert result["file"]["path"] == str(requested)
    assert result["file"]["requestedPath"] == str(requested)
    assert result["file"]["path"] == result["file"]["requestedPath"]


@pytest.mark.unit
def test_export_pdf_missing_output_path_errors():
    board = MagicMock(name="board")
    result = ExportCommands(board).export_pdf({})
    assert result["success"] is False
