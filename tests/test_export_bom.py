"""Tests for export_bom: mounting-hole filtering and clean reference strings."""

import csv
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.export import ExportCommands  # noqa: E402


def _make_fp(ref: str, value: str, fpid: str) -> MagicMock:
    fp = MagicMock(name=f"fp_{ref}")
    fp.GetReference.return_value = ref
    fp.GetValue.return_value = value
    fpid_obj = MagicMock()
    fpid_obj.GetUniStringLibId.return_value = fpid
    fp.GetFPID.return_value = fpid_obj
    fp.GetLayer.return_value = 0
    return fp


def _make_board(fps):
    board = MagicMock(name="board")
    board.GetFootprints.return_value = fps
    board.GetLayerName.return_value = "F.Cu"
    return board


def _read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


@pytest.mark.unit
class TestExportBomMountingHoles:
    def test_mounting_holes_excluded_by_default(self, tmp_path):
        board = _make_board(
            [
                _make_fp("R1", "10k", "Resistor_SMD:R_0603_1608Metric"),
                _make_fp("MH1", "MountingHole", "MountingHole:MountingHole_3.2mm"),
                _make_fp("MH2", "MountingHole", "MountingHole:MountingHole_3.2mm"),
            ]
        )
        out = tmp_path / "bom.csv"
        result = ExportCommands(board).export_bom({"outputPath": str(out), "format": "CSV"})

        assert result["success"] is True
        assert result["excludedMountingHoles"] == 2
        assert "2 mounting hole(s) excluded" in result["message"]
        assert result["file"]["componentCount"] == 1  # only R1 survives

        rows = _read_csv(out)
        assert [r["references"] for r in rows] == ["R1"]

    def test_include_flag_keeps_them_and_reference_string_is_clean(self, tmp_path):
        # Out-of-order refs to prove natural sort (MH1, MH2, MH10 — not MH1, MH10, MH2).
        board = _make_board(
            [
                _make_fp("MH2", "MountingHole", "MountingHole:MountingHole_3.2mm"),
                _make_fp("MH10", "MountingHole", "MountingHole:MountingHole_3.2mm"),
                _make_fp("MH1", "MountingHole", "MountingHole:MountingHole_3.2mm"),
            ]
        )
        out = tmp_path / "bom.csv"
        result = ExportCommands(board).export_bom(
            {"outputPath": str(out), "format": "CSV", "includeMountingHoles": True}
        )

        assert result["success"] is True
        assert result["excludedMountingHoles"] == 0

        (row,) = _read_csv(out)
        # Clean, naturally-sorted, comma-separated — NOT a Python list repr.
        assert row["references"] == "MH1, MH2, MH10"
        assert "[" not in row["references"] and "'" not in row["references"]
        assert row["quantity"] == "3"

    def test_regular_refs_grouped_as_clean_string(self, tmp_path):
        board = _make_board(
            [
                _make_fp("R2", "10k", "Resistor_SMD:R_0603_1608Metric"),
                _make_fp("R1", "10k", "Resistor_SMD:R_0603_1608Metric"),
            ]
        )
        out = tmp_path / "bom.csv"
        result = ExportCommands(board).export_bom({"outputPath": str(out), "format": "CSV"})

        (row,) = _read_csv(out)
        assert row["references"] == "R1, R2"
        assert result["excludedMountingHoles"] == 0
