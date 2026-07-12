"""
Tests for run_erc handler.

Covers:
  - Non-zero exit code acceptance (kicad-cli returns non-zero when violations exist)
  - KiCad 9 sheets[].violations JSON structure parsing
  - KiCad 8 top-level violations[] JSON structure (backward compat)
  - Missing/empty output file handling
"""

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


# ---------------------------------------------------------------------------
# Shared fixture: KiCADInterface instance (no __init__, avoids pcbnew/IPC)
# ---------------------------------------------------------------------------


def _make_iface() -> Any:
    with patch("kicad_interface.USE_IPC_BACKEND", False):
        from kicad_interface import KiCADInterface

        iface = KiCADInterface.__new__(KiCADInterface)
    return iface


@pytest.fixture()
def iface():
    return _make_iface()


# ---------------------------------------------------------------------------
# Sample ERC JSON outputs
# ---------------------------------------------------------------------------

# KiCad 8 style: violations at top level
_ERC_KICAD8_JSON = {
    "violations": [
        {
            "type": "pin_not_connected",
            "severity": "error",
            "description": "Pin not connected",
            "items": [{"pos": {"x": 100.0, "y": 50.0}}],
        },
        {
            "type": "wire_dangling",
            "severity": "warning",
            "description": "Wire end not connected",
            "items": [{"pos": {"x": 200.0, "y": 75.0}}],
        },
    ]
}

# KiCad 9 style: violations nested under sheets[]
_ERC_KICAD9_JSON = {
    "violations": [],
    "sheets": [
        {
            "path": "/",
            "violations": [
                {
                    "type": "pin_not_connected",
                    "severity": "error",
                    "description": "Pin not connected",
                    "items": [{"pos": {"x": 10.0, "y": 20.0}}],
                },
            ],
        },
        {
            "path": "/sub-sheet-1",
            "violations": [
                {
                    "type": "label_dangling",
                    "severity": "error",
                    "description": "Label not connected to anything",
                    "items": [{"pos": {"x": 30.0, "y": 40.0}}],
                },
                {
                    "type": "wire_dangling",
                    "severity": "warning",
                    "description": "Wire end not connected",
                    "items": [{"pos": {"x": 50.0, "y": 60.0}}],
                },
            ],
        },
    ],
}

# KiCad 9 with violations in both top-level and sheets (edge case)
_ERC_MIXED_JSON = {
    "violations": [
        {
            "type": "power_pin_not_driven",
            "severity": "error",
            "description": "Power pin not driven",
            "items": [{"pos": {"x": 1.0, "y": 2.0}}],
        },
    ],
    "sheets": [
        {
            "path": "/sub",
            "violations": [
                {
                    "type": "pin_not_connected",
                    "severity": "error",
                    "description": "Pin not connected",
                    "items": [{"pos": {"x": 3.0, "y": 4.0}}],
                },
            ],
        },
    ],
}


def _mock_erc_run(erc_json: dict, returncode: int = 1):
    """Create a mock subprocess.run that writes ERC JSON to the output file."""

    def _side_effect(cmd, **kwargs):
        # Find the output path from the command args (--output <path>)
        output_idx = cmd.index("--output") + 1
        output_path = cmd[output_idx]
        with open(output_path, "w") as f:
            json.dump(erc_json, f)
        result = MagicMock()
        result.returncode = returncode
        result.stderr = ""
        return result

    return _side_effect


def _mock_erc_no_output(returncode: int = 2):
    """Create a mock subprocess.run that produces no output file."""

    def _side_effect(cmd, **kwargs):
        result = MagicMock()
        result.returncode = returncode
        result.stderr = "kicad-cli: error: schematic not found"
        return result

    return _side_effect


# ===========================================================================
# Tests
# ===========================================================================


@pytest.mark.unit
class TestERCNonZeroExitCode:
    """kicad-cli returns non-zero when violations exist — this is not an error."""

    def test_nonzero_returncode_with_valid_json_succeeds(self, iface, tmp_path):
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")

        iface.design_rule_commands = MagicMock()
        iface.design_rule_commands._find_kicad_cli.return_value = "/usr/bin/kicad-cli"

        with patch("subprocess.run", side_effect=_mock_erc_run(_ERC_KICAD8_JSON, returncode=1)):
            result = iface._handle_run_erc({"schematicPath": str(sch)})

        assert result["success"] is True
        assert "2 violation" in result["message"]

    def test_zero_returncode_no_violations(self, iface, tmp_path):
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")

        iface.design_rule_commands = MagicMock()
        iface.design_rule_commands._find_kicad_cli.return_value = "/usr/bin/kicad-cli"

        with patch("subprocess.run", side_effect=_mock_erc_run({"violations": []}, returncode=0)):
            result = iface._handle_run_erc({"schematicPath": str(sch)})

        assert result["success"] is True
        assert "0 violation" in result["message"]

    def test_no_output_file_fails(self, iface, tmp_path):
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")

        iface.design_rule_commands = MagicMock()
        iface.design_rule_commands._find_kicad_cli.return_value = "/usr/bin/kicad-cli"

        with patch("subprocess.run", side_effect=_mock_erc_no_output()):
            result = iface._handle_run_erc({"schematicPath": str(sch)})

        assert result["success"] is False
        assert "no output" in result["message"].lower()


@pytest.mark.unit
class TestERCKicad9SheetsViolations:
    """KiCad 9 nests violations under sheets[].violations."""

    def test_kicad9_sheets_violations_collected(self, iface, tmp_path):
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")

        iface.design_rule_commands = MagicMock()
        iface.design_rule_commands._find_kicad_cli.return_value = "/usr/bin/kicad-cli"

        with patch("subprocess.run", side_effect=_mock_erc_run(_ERC_KICAD9_JSON)):
            result = iface._handle_run_erc({"schematicPath": str(sch)})

        assert result["success"] is True
        assert "3 violation" in result["message"]
        assert result["summary"]["by_severity"]["error"] == 2
        assert result["summary"]["by_severity"]["warning"] == 1

    def test_kicad8_top_level_violations_still_work(self, iface, tmp_path):
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")

        iface.design_rule_commands = MagicMock()
        iface.design_rule_commands._find_kicad_cli.return_value = "/usr/bin/kicad-cli"

        with patch("subprocess.run", side_effect=_mock_erc_run(_ERC_KICAD8_JSON)):
            result = iface._handle_run_erc({"schematicPath": str(sch)})

        assert result["success"] is True
        assert "2 violation" in result["message"]
        assert result["summary"]["by_severity"]["error"] == 1
        assert result["summary"]["by_severity"]["warning"] == 1

    def test_mixed_top_level_and_sheets_violations(self, iface, tmp_path):
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")

        iface.design_rule_commands = MagicMock()
        iface.design_rule_commands._find_kicad_cli.return_value = "/usr/bin/kicad-cli"

        with patch("subprocess.run", side_effect=_mock_erc_run(_ERC_MIXED_JSON)):
            result = iface._handle_run_erc({"schematicPath": str(sch)})

        assert result["success"] is True
        # 1 top-level + 1 from sheets = 2 total
        assert "2 violation" in result["message"]
        assert result["summary"]["by_severity"]["error"] == 2


# ---------------------------------------------------------------------------
# Power-input-not-driven false-positive tagging + demotion + sort.
# ---------------------------------------------------------------------------


# Schematic with a VCC power symbol so _collect_power_label_names returns
# {"VCC"}.  That's the trigger for `_violation_mentions_power_label` to
# match — the violation's items[].net field below references the same.
_SCH_WITH_VCC_POWER = """\
(kicad_sch
  (symbol
    (lib_id "power:VCC")
    (property "Reference" "#PWR01")
    (property "Value" "VCC")
  )
)
"""

# Two violations: one power_pin_not_driven false-positive (VCC label
# exists in the schematic, the items[].net says VCC) and one genuine
# pin_not_connected error that must remain in the error count.
_ERC_POWER_FP_AND_REAL = {
    "violations": [
        {
            "type": "power_pin_not_driven",
            "severity": "error",
            "description": "Input Power pin not driven by any Power Source",
            "items": [{"pos": {"x": 100.0, "y": 50.0}, "net": "VCC", "component_ref": "U1"}],
        },
        {
            "type": "pin_not_connected",
            "severity": "error",
            "description": "Pin not connected",
            "items": [{"pos": {"x": 200.0, "y": 75.0}}],
        },
    ]
}


@pytest.mark.unit
class TestPowerNotDrivenFalsePositiveDemotion:
    """The user's complaint: ERC reports power_pin_not_driven as ERRORS
    even when the netlist is correct (sync produced clean pad↔net map).
    We tag those as likely_false_positive AND demote them out of the
    by_severity error bucket so the headline count reflects real
    problems."""

    def test_tagged_fps_are_subtracted_from_error_count(self, iface, tmp_path):
        sch = tmp_path / "test.kicad_sch"
        sch.write_text(_SCH_WITH_VCC_POWER)

        iface.design_rule_commands = MagicMock()
        iface.design_rule_commands._find_kicad_cli.return_value = "/usr/bin/kicad-cli"

        with patch("subprocess.run", side_effect=_mock_erc_run(_ERC_POWER_FP_AND_REAL)):
            result = iface._handle_run_erc({"schematicPath": str(sch)})

        assert result["success"] is True
        s = result["summary"]
        assert s["total"] == 2
        assert s["likely_false_positives"] == 1
        # The VCC power_pin_not_driven is tagged → demoted out of error.
        assert s["by_severity"]["error"] == 1, (
            "Only the pin_not_connected is a real error; the power_pin_not_driven "
            "must NOT bloat the headline error count once tagged"
        )
        # Raw count preserved for callers that want the kicad-cli original.
        assert s["raw_by_severity"]["error"] == 2
        assert s["real_errors"] == 1

    def test_real_errors_sort_before_false_positives(self, iface, tmp_path):
        """Agent scans violations top-down — the actionable ones must
        come first so the agent doesn't tune out before reaching them."""
        sch = tmp_path / "test.kicad_sch"
        sch.write_text(_SCH_WITH_VCC_POWER)

        iface.design_rule_commands = MagicMock()
        iface.design_rule_commands._find_kicad_cli.return_value = "/usr/bin/kicad-cli"

        with patch("subprocess.run", side_effect=_mock_erc_run(_ERC_POWER_FP_AND_REAL)):
            result = iface._handle_run_erc({"schematicPath": str(sch)})

        violations = result["violations"]
        assert violations[0]["type"] == "pin_not_connected"
        assert "likely_false_positive" not in violations[0]
        assert violations[1]["type"] == "power_pin_not_driven"
        assert violations[1].get("likely_false_positive") is True

    def test_item_level_net_field_triggers_fp_tag(self, iface, tmp_path):
        """kicad-cli sometimes puts the net name in items[].net rather
        than in the description.  The tagger must check both — the
        previous description-only check missed this case and let
        every items-only-net violation through as a hard error."""
        sch = tmp_path / "test.kicad_sch"
        sch.write_text(_SCH_WITH_VCC_POWER)

        # Description does NOT mention VCC; only items[].net does.
        erc = {
            "violations": [
                {
                    "type": "power_pin_not_driven",
                    "severity": "error",
                    "description": "Input Power pin not driven",
                    "items": [{"pos": {"x": 1.0, "y": 2.0}, "net": "VCC"}],
                },
            ]
        }
        iface.design_rule_commands = MagicMock()
        iface.design_rule_commands._find_kicad_cli.return_value = "/usr/bin/kicad-cli"

        with patch("subprocess.run", side_effect=_mock_erc_run(erc)):
            result = iface._handle_run_erc({"schematicPath": str(sch)})

        assert result["summary"]["likely_false_positives"] == 1
        assert result["summary"]["by_severity"]["error"] == 0

    def test_no_power_labels_means_no_tagging(self, iface, tmp_path):
        """Defensive: if the schematic has no power labels/symbols at
        all, a power_pin_not_driven IS a real error — don't suppress."""
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")  # no power symbols

        # No VCC/GND in description or items.
        erc = {
            "violations": [
                {
                    "type": "power_pin_not_driven",
                    "severity": "error",
                    "description": "Input Power pin not driven",
                    "items": [{"pos": {"x": 1.0, "y": 2.0}}],
                },
            ]
        }
        iface.design_rule_commands = MagicMock()
        iface.design_rule_commands._find_kicad_cli.return_value = "/usr/bin/kicad-cli"

        with patch("subprocess.run", side_effect=_mock_erc_run(erc)):
            result = iface._handle_run_erc({"schematicPath": str(sch)})

        assert result["summary"]["likely_false_positives"] == 0
        assert result["summary"]["by_severity"]["error"] == 1


# ---------------------------------------------------------------------------
# Response ergonomics: explicit truncation, maxViolations, real_errors first,
# and a stable-English (C) locale for the kicad-cli subprocess.
# ---------------------------------------------------------------------------


def _erc_many_errors(n: int) -> dict:
    """N genuine (non-false-positive) pin_not_connected error violations."""
    return {
        "violations": [
            {
                "type": "pin_not_connected",
                "severity": "error",
                "description": f"Pin not connected #{i}",
                "items": [{"pos": {"x": float(i), "y": float(i)}}],
            }
            for i in range(n)
        ]
    }


@pytest.mark.unit
class TestERCTruncationAndErgonomics:
    """run_erc must cap the returned list explicitly (showing N of M), honor
    a maxViolations param end-to-end, and surface real_errors first."""

    def test_default_truncation_marks_showing_30_of_total(self, iface, tmp_path):
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")
        iface.design_rule_commands = MagicMock()
        iface.design_rule_commands._find_kicad_cli.return_value = "/usr/bin/kicad-cli"

        with patch("subprocess.run", side_effect=_mock_erc_run(_erc_many_errors(35))):
            result = iface._handle_run_erc({"schematicPath": str(sch)})

        s = result["summary"]
        assert s["total"] == 35, "total must be the FULL count, not the page slice"
        assert s["shown"] == 30, "default cap is 30"
        assert s["truncated"] is True
        assert len(result["violations"]) == 30, "returned list is capped to shown"

    def test_max_violations_honored_smaller(self, iface, tmp_path):
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")
        iface.design_rule_commands = MagicMock()
        iface.design_rule_commands._find_kicad_cli.return_value = "/usr/bin/kicad-cli"

        with patch("subprocess.run", side_effect=_mock_erc_run(_erc_many_errors(35))):
            result = iface._handle_run_erc({"schematicPath": str(sch), "maxViolations": 5})

        s = result["summary"]
        assert s["total"] == 35
        assert s["shown"] == 5
        assert s["truncated"] is True
        assert s["max_violations"] == 5
        assert len(result["violations"]) == 5

    def test_max_violations_zero_returns_all(self, iface, tmp_path):
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")
        iface.design_rule_commands = MagicMock()
        iface.design_rule_commands._find_kicad_cli.return_value = "/usr/bin/kicad-cli"

        with patch("subprocess.run", side_effect=_mock_erc_run(_erc_many_errors(35))):
            result = iface._handle_run_erc({"schematicPath": str(sch), "maxViolations": 0})

        s = result["summary"]
        assert s["total"] == 35
        assert s["shown"] == 35
        assert s["truncated"] is False
        assert len(result["violations"]) == 35

    def test_real_errors_is_first_summary_field(self, iface, tmp_path):
        """real_errors is the single most important field — it must lead the
        summary payload so a client scanning top-down sees it immediately."""
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")
        iface.design_rule_commands = MagicMock()
        iface.design_rule_commands._find_kicad_cli.return_value = "/usr/bin/kicad-cli"

        with patch("subprocess.run", side_effect=_mock_erc_run(_erc_many_errors(3))):
            result = iface._handle_run_erc({"schematicPath": str(sch)})

        keys = list(result["summary"].keys())
        assert keys[0] == "real_errors", f"real_errors must lead summary, got {keys[:3]}"
        assert result["summary"]["real_errors"] == 3
        # errors/warnings totals sit right at the top too.
        assert "errors" in keys[:5]
        assert "warnings" in keys[:5]

    def test_no_truncation_when_under_cap(self, iface, tmp_path):
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")
        iface.design_rule_commands = MagicMock()
        iface.design_rule_commands._find_kicad_cli.return_value = "/usr/bin/kicad-cli"

        with patch("subprocess.run", side_effect=_mock_erc_run(_erc_many_errors(4))):
            result = iface._handle_run_erc({"schematicPath": str(sch)})

        s = result["summary"]
        assert s["total"] == 4
        assert s["shown"] == 4
        assert s["truncated"] is False

    def test_kicad_cli_runs_under_c_locale(self, iface, tmp_path):
        """kicad-cli ERC must run with LC_ALL=C / LANG=C so its violation
        descriptions come back in stable English regardless of the user's UI
        locale (else downstream pattern-matching breaks)."""
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")
        iface.design_rule_commands = MagicMock()
        iface.design_rule_commands._find_kicad_cli.return_value = "/usr/bin/kicad-cli"

        captured: dict = {}

        def _capturing_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            output_path = cmd[cmd.index("--output") + 1]
            with open(output_path, "w") as f:
                json.dump({"violations": []}, f)
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=_capturing_run):
            result = iface._handle_run_erc({"schematicPath": str(sch)})

        assert result["success"] is True
        env = captured["env"]
        assert env is not None, "ERC subprocess must receive an explicit env"
        assert env.get("LC_ALL") == "C"
        assert env.get("LANG") == "C"
        # The rest of the environment must survive intact.
        assert "PATH" in env
