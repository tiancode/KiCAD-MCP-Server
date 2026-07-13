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


# ===========================================================================
# Locale normalization: kicad-cli must produce English violation text.
#
# kicad-cli reads its UI language from the KiCad config (kicad_common.json,
# system.language), NOT from LC_ALL. A user with a Chinese UI otherwise gets
# localized ERC descriptions the server's English-substring heuristics miss.
# run_erc now points the subprocess at a derived config forcing English.
# ===========================================================================


def _fake_real_config(tmp_path, language="简体中文", extra_system=None, marker=True):
    """Create a fake 'real' KiCad config home with a given UI language.

    Returns (config_home, version). Includes an extra system key and a marker
    sidecar file so tests can prove the derivation copies OTHER contents too.
    """
    version = "10.0"
    ver_dir = tmp_path / "realcfg" / version
    ver_dir.mkdir(parents=True)
    system = {"language": language, "text_editor": "/usr/bin/vi"}
    if extra_system:
        system.update(extra_system)
    (ver_dir / "kicad_common.json").write_text(
        json.dumps({"system": system, "environment": {"vars": {"MYVAR": "keepme"}}})
    )
    if marker:
        (ver_dir / "sym-lib-table").write_text("(sym_lib_table)")
    return str(tmp_path / "realcfg"), version


@pytest.mark.unit
class TestERCLocaleNormalization:
    def test_run_erc_points_subprocess_at_english_config(self, iface, tmp_path, monkeypatch):
        import utils.kicad_cli as kc

        real_home, version = _fake_real_config(tmp_path)
        kc._en_config_cache.clear()
        monkeypatch.setattr(kc, "_discover_real_config", lambda: (real_home, version))

        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")
        iface.design_rule_commands = MagicMock()
        iface.design_rule_commands._find_kicad_cli.return_value = "/usr/bin/kicad-cli"

        captured = {}

        def _capture(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            out = cmd[cmd.index("--output") + 1]
            with open(out, "w") as f:
                json.dump({"violations": []}, f)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        with patch("subprocess.run", side_effect=_capture):
            result = iface._handle_run_erc(
                {"schematicPath": str(sch), "autoRefreshLibSymbols": False}
            )

        assert result["success"] is True
        env = captured["env"]
        assert env is not None
        # LC_ALL=C kept (harmless, normalizes non-config strings).
        assert env["LC_ALL"] == "C"
        # KICAD_CONFIG_HOME points at a config whose language forces English.
        common = Path(env["KICAD_CONFIG_HOME"]) / version / "kicad_common.json"
        data = json.loads(common.read_text())
        assert data["system"]["language"] == "English"
        # And it is NOT the user's real config (only read, never written).
        assert Path(env["KICAD_CONFIG_HOME"]) != Path(real_home)

    def test_derived_config_preserves_existing_config_contents(self, tmp_path, monkeypatch):
        """The task requirement: an existing config's OTHER contents survive
        the language override (env-var defs, other files, other keys)."""
        import utils.kicad_cli as kc

        real_home, version = _fake_real_config(tmp_path)
        kc._en_config_cache.clear()
        monkeypatch.setattr(kc, "_discover_real_config", lambda: (real_home, version))

        env = kc.c_locale_env()
        cfg = Path(env["KICAD_CONFIG_HOME"]) / version
        data = json.loads((cfg / "kicad_common.json").read_text())
        assert data["system"]["language"] == "English"
        # Other keys + sidecar files copied verbatim.
        assert data["system"]["text_editor"] == "/usr/bin/vi"
        assert data["environment"]["vars"]["MYVAR"] == "keepme"
        assert (cfg / "sym-lib-table").read_text() == "(sym_lib_table)"

    def test_owned_config_home_language_overridden_in_place(self, tmp_path):
        """When a merged config home (copy of the real config) is already being
        passed for sym-lib-table reasons, override language IN PLACE and keep
        every other file — do not build a second config."""
        import utils.kicad_cli as kc

        owned = tmp_path / "merged"
        ver = owned / "10.0"
        ver.mkdir(parents=True)
        (ver / "kicad_common.json").write_text(
            json.dumps({"system": {"language": "简体中文", "file_history_size": 9}})
        )
        (ver / "sym-lib-table").write_text("(sym_lib_table (lib (name PROJ)))")

        base = {"KICAD_CONFIG_HOME": str(owned), "PATH": "/usr/bin"}
        env = kc.c_locale_env(base_env=base, owned_config_home=str(owned))

        assert env["LC_ALL"] == "C"
        assert env["KICAD_CONFIG_HOME"] == str(owned)  # reused in place
        data = json.loads((ver / "kicad_common.json").read_text())
        assert data["system"]["language"] == "English"
        assert data["system"]["file_history_size"] == 9  # untouched
        # The merged sym-lib-table survives the override.
        assert "PROJ" in (ver / "sym-lib-table").read_text()

    def test_no_real_config_still_sets_lc_all(self, monkeypatch):
        """Fresh machine: no config found → just LC_ALL=C, no KICAD_CONFIG_HOME."""
        import utils.kicad_cli as kc

        kc._en_config_cache.clear()
        monkeypatch.setattr(kc, "_discover_real_config", lambda: None)
        env = kc.c_locale_env(base_env={"PATH": "/usr/bin"})
        assert env["LC_ALL"] == "C"
        assert "KICAD_CONFIG_HOME" not in env


# ---------------------------------------------------------------------------
# Real kicad-cli integration: prove English output on a localized machine.
# ---------------------------------------------------------------------------


_ERC_LOCALE_SCH = """(kicad_sch (version 20250114) (generator "test")
  (uuid a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d)
  (paper "A4")
  (lib_symbols
    (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0)) (in_bom yes) (on_board yes)
      (property "Reference" "R" (at 2.032 0 90) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 0 90) (effects (font (size 1.27 1.27))))
      (symbol "R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "Device:R") (at 100 100 0) (unit 1)
    (in_bom yes) (on_board yes)
    (uuid 00000000-0000-0000-0000-000000000001)
    (property "Reference" "R1" (at 102 100 90) (effects (font (size 1.27 1.27))))
    (property "Value" "1k" (at 98 100 90) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid 00000000-0000-0000-0000-000000000010))
    (pin "2" (uuid 00000000-0000-0000-0000-000000000011))
  )
  (sheet_instances (path "/" (page "1")))
)
"""


@pytest.mark.integration
def test_run_erc_real_kicad_cli_forces_english(tmp_path):
    """On a machine whose KiCad UI language is non-English, the ERC handler must
    still return English violation text (config-language override + LC_ALL=C).

    Skips when kicad-cli is absent or the real config is already English, since
    the fix is only observable against a localized config.
    """
    import re as _re

    import utils.kicad_cli as kc

    # Discover kicad-cli the way production does (PATH first, then platform
    # fallbacks incl. the macOS app bundle where it is never on PATH).
    cli = kc.find_kicad_cli()
    if not cli:
        pytest.skip("kicad-cli not found")
    disc = kc._discover_real_config()
    if disc is None:
        pytest.skip("no real KiCad config found on this machine")
    real_home, version = disc
    real_common = Path(real_home) / version / "kicad_common.json"
    lang = json.loads(real_common.read_text()).get("system", {}).get("language")
    if lang in (None, "", "Default", "English"):
        pytest.skip(f"real config language is {lang!r}; English-forcing not observable")

    sch = tmp_path / "erc_locale.kicad_sch"
    sch.write_text(_ERC_LOCALE_SCH)

    iface = _make_iface()
    iface.design_rule_commands = MagicMock()
    iface.design_rule_commands._find_kicad_cli.return_value = cli

    # autoRefreshLibSymbols=False keeps the embedded Device:R differing from the
    # stock library, which yields a `lib_symbol_mismatch` violation whose
    # description kicad-cli DOES localize (unlike "Pin not connected").
    result = iface._handle_run_erc({"schematicPath": str(sch), "autoRefreshLibSymbols": False})
    assert result["success"] is True, result
    messages = [v.get("message", "") for v in result["violations"]]

    mismatch = [m for m in messages if "match" in m.lower() and "librar" in m.lower()]
    assert mismatch, f"expected a localizable lib_symbol_mismatch violation; got {messages}"
    # The whole point: no CJK (localized) text leaked into any violation.
    for m in messages:
        assert not _re.search(r"[一-鿿]", m), f"non-English violation text leaked: {m!r}"

    # The user's real config must be untouched by the run.
    assert json.loads(real_common.read_text()).get("system", {}).get("language") == lang
