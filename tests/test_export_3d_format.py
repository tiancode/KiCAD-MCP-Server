"""Regression (C4): export_3d must not advertise formats it can't produce, and
an unsupported-format request is user error (UNSUPPORTED_FORMAT), not
INTERNAL_ERROR.

Phase C E2E found the MCP schema enum offered STEP/STL/VRML/OBJ but the backend
supports only STEP/VRML; STL/OBJ fell through to the else-branch and returned
success:false with NO errorCode, which the failure classifier then bucketed as
INTERNAL_ERROR.  The TS enum is now narrowed to ["STEP","VRML"] and the backend
else-branch carries errorCode UNSUPPORTED_FORMAT.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

import subprocess  # noqa: E402

from commands.export._fabrication import FabricationMixin  # noqa: E402


class _Export3DHost(FabricationMixin):
    def __init__(self, board_file: str, cli="kicad-cli"):
        self.board = MagicMock(name="board")
        self.board.GetFileName.return_value = board_file
        self._cli = cli

    def _find_kicad_cli(self):
        return self._cli


def _saved_board(tmp_path: Path) -> str:
    board = tmp_path / "mini.kicad_pcb"
    board.write_text("(kicad_pcb)")
    return str(board)


@pytest.mark.unit
@pytest.mark.parametrize("fmt", ["STL", "OBJ"])
def test_unsupported_format_returns_unsupported_format_errorcode(tmp_path, fmt):
    host = _Export3DHost(_saved_board(tmp_path))
    result = host.export_3d({"outputPath": str(tmp_path / f"out.{fmt.lower()}"), "format": fmt})
    assert result["success"] is False
    # The key regression: a user-error format must NOT surface as INTERNAL_ERROR.
    assert result["errorCode"] == "UNSUPPORTED_FORMAT"
    assert "STEP" in result["errorDetails"] and "VRML" in result["errorDetails"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "fmt,subcommand",
    [("STEP", "step"), ("VRML", "vrml")],
)
def test_supported_format_builds_command_and_succeeds(tmp_path, monkeypatch, fmt, subcommand):
    out = tmp_path / f"out.{fmt.lower()}"
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        Path(out).write_text("model-bytes")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    host = _Export3DHost(_saved_board(tmp_path))
    result = host.export_3d({"outputPath": str(out), "format": fmt})

    assert result["success"] is True, result
    assert result["file"]["format"] == fmt
    # The right kicad-cli subcommand was chosen for the format.
    assert captured["cmd"][:4] == ["kicad-cli", "pcb", "export", subcommand]


@pytest.mark.unit
def test_case_insensitive_format_still_unsupported(tmp_path):
    """A lowercase 'stl' is still user error with the same errorCode."""
    host = _Export3DHost(_saved_board(tmp_path))
    result = host.export_3d({"outputPath": str(tmp_path / "out.stl"), "format": "stl"})
    assert result["success"] is False
    assert result["errorCode"] == "UNSUPPORTED_FORMAT"
