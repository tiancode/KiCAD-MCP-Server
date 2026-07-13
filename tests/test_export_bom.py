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


def _make_fp_fields(ref: str, value: str, fpid: str, fields: dict) -> MagicMock:
    """Footprint whose GetFieldsText() returns a real dict of custom fields
    (MPN, "LCSC Part", …) — the KiCad 8+/10 name→text shape export_bom reads."""
    fp = _make_fp(ref, value, fpid)
    fp.GetFieldsText.return_value = dict(fields)
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


@pytest.mark.unit
class TestExportBomSourcingAttributes:
    """Requested sourcing columns (MPN/LCSC/Manufacturer) must be populated
    from footprint fields, with an alias for the space-bearing "LCSC Part"
    and an explicit warning for attributes missing everywhere."""

    def _board_with_sourcing(self):
        return _make_board(
            [
                _make_fp_fields(
                    "U1",
                    "GD32F103VET6",
                    "Package_QFP:LQFP-100",
                    {
                        "MPN": "GD32F103VET6",
                        "Manufacturer": "GigaDevice",
                        "LCSC Part": "C80215",
                        "Datasheet": "https://example/u1",
                    },
                ),
                _make_fp_fields(
                    "R1",
                    "10k",
                    "Resistor_SMD:R_0603_1608Metric",
                    {"MPN": "RC0603FR-0710KL", "Manufacturer": "Yageo", "LCSC Part": "C25804"},
                ),
            ]
        )

    def test_requested_attributes_become_populated_columns(self, tmp_path):
        board = self._board_with_sourcing()
        out = tmp_path / "bom.csv"
        result = ExportCommands(board).export_bom(
            {
                "outputPath": str(out),
                "format": "CSV",
                "groupByValue": False,
                "includeAttributes": ["LCSC", "MPN", "Manufacturer"],
            }
        )

        assert result["success"] is True
        assert result["attributesMissing"] == []
        rows = {r["reference"]: r for r in _read_csv(out)}
        # "LCSC" alias resolves to the "LCSC Part" field.
        assert rows["U1"]["LCSC"] == "C80215"
        assert rows["U1"]["MPN"] == "GD32F103VET6"
        assert rows["U1"]["Manufacturer"] == "GigaDevice"
        assert rows["R1"]["LCSC"] == "C25804"
        # The alias resolution is reported.
        resolved = {r["requested"]: r["field"] for r in result["attributesResolved"]}
        assert resolved["LCSC"] == "LCSC Part"

    def test_missing_attribute_produces_explicit_warning(self, tmp_path):
        board = self._board_with_sourcing()
        out = tmp_path / "bom.csv"
        result = ExportCommands(board).export_bom(
            {
                "outputPath": str(out),
                "format": "CSV",
                "groupByValue": False,
                "includeAttributes": ["MPN", "Tolerance"],
            }
        )

        assert result["success"] is True
        assert result["attributesMissing"] == ["Tolerance"]
        assert "Tolerance" in result["warning"]
        # Near-miss hint names the fields that ARE present.
        assert "LCSC Part" in result["warning"]
        # The present one still becomes a column; the missing one does not.
        header = _read_csv(out)[0].keys()
        assert "MPN" in header
        assert "Tolerance" not in header

    def test_attributes_alias_param_name(self, tmp_path):
        """The shorthand ``attributes`` param works like ``includeAttributes``."""
        board = self._board_with_sourcing()
        out = tmp_path / "bom.csv"
        result = ExportCommands(board).export_bom(
            {
                "outputPath": str(out),
                "format": "CSV",
                "groupByValue": False,
                "attributes": ["LCSC"],
            }
        )
        assert result["success"] is True
        rows = {r["reference"]: r for r in _read_csv(out)}
        assert rows["U1"]["LCSC"] == "C80215"

    def test_grouped_disagreeing_attribute_values_are_joined(self, tmp_path):
        """Two same-value/footprint parts with different MPNs collapse into one
        grouped row whose MPN cell joins the distinct values (no data lost,
        group not split)."""
        board = _make_board(
            [
                _make_fp_fields(
                    "R1", "10k", "Resistor_SMD:R_0603_1608Metric", {"MPN": "AAA", "LCSC Part": "C1"}
                ),
                _make_fp_fields(
                    "R2", "10k", "Resistor_SMD:R_0603_1608Metric", {"MPN": "BBB", "LCSC Part": "C1"}
                ),
            ]
        )
        out = tmp_path / "bom.csv"
        result = ExportCommands(board).export_bom(
            {
                "outputPath": str(out),
                "format": "CSV",
                "groupByValue": True,
                "includeAttributes": ["MPN", "LCSC"],
            }
        )
        assert result["success"] is True
        (row,) = _read_csv(out)
        assert row["quantity"] == "2"
        assert row["references"] == "R1, R2"
        # Distinct MPNs joined; agreeing LCSC left as the single value.
        assert row["MPN"] == "AAA; BBB"
        assert row["LCSC"] == "C1"

    def test_no_custom_fields_anywhere_warns_with_sync_hint(self, tmp_path):
        """A board with zero sourcing fields (never synced) warns explicitly
        instead of silently emitting a header-only BOM."""
        board = _make_board([_make_fp("R1", "10k", "Resistor_SMD:R_0603_1608Metric")])
        out = tmp_path / "bom.csv"
        result = ExportCommands(board).export_bom(
            {
                "outputPath": str(out),
                "format": "CSV",
                "includeAttributes": ["MPN", "LCSC"],
            }
        )
        assert result["success"] is True
        assert sorted(result["attributesMissing"]) == ["LCSC", "MPN"]
        assert "sync_schematic_to_board" in result["warning"]
