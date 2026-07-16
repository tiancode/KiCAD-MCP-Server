"""Regression: get_design_rules must report the persisted .kicad_pro rules.

C12 + D1: the in-memory BOARD_DESIGN_SETTINGS the server reads is NOT
authoritative. On BOTH backends it can return pcbnew hard-defaults (clearance 0,
minTrack 0.2, minVia 0.5, minDrill 0.3) while the *saved project* (.kicad_pro)
holds the real minima and the Default netclass holds the real track/via
defaults. get_design_rules now overlays the persisted project values as the
source of truth, falling back to the in-memory read only for keys the project
JSON does not carry.

These tests build a fake board whose GetDesignSettings() deliberately returns the
stale hard-defaults, drop a differing .kicad_pro next to it, and assert the read
returns the project values — no real pcbnew required (deterministic / offline).
"""

import json
import sys
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))


# --- Fakes: an in-memory BDS returning stale pcbnew hard-defaults (nm ints) ---
class _StaleBDS:
    """m_* fields + GetCurrent* methods, all at pcbnew hard-defaults (nm)."""

    m_MinClearance = 0
    m_TrackMinWidth = 200_000  # 0.2 mm
    m_ViasMinSize = 500_000  # 0.5 mm
    m_MinThroughDrill = 300_000  # 0.3 mm
    m_MicroViasMinSize = 300_000
    m_MicroViasMinDrill = 100_000
    m_ViasMinAnnularWidth = 100_000
    m_HoleClearance = 250_000
    m_HoleToHoleMin = 250_000
    m_CopperEdgeClearance = 500_000
    m_SilkClearance = 0

    def GetCurrentTrackWidth(self):
        return 200_000

    def GetCurrentViaSize(self):
        return 500_000

    def GetCurrentViaDrill(self):
        return 300_000


class _FakeBoard:
    def __init__(self, board_file: str):
        self._file = board_file

    def GetFileName(self):
        return self._file

    def GetDesignSettings(self):
        return _StaleBDS()


# --- .kicad_pro fixture holding the REAL saved rules (mm floats) ---
def _write_pro(path: Path, *, rules: dict, default_class: dict) -> None:
    data = {
        "board": {"design_settings": {"rules": rules}},
        "net_settings": {
            "classes": [
                {"name": "Default", "priority": 2147483647, **default_class},
            ],
            "netclass_assignments": None,
            "netclass_patterns": [],
        },
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent="  ", ensure_ascii=False)
        f.write("\n")


def _commands_for(tmp_path: Path, *, rules: dict, default_class: dict):
    from commands.design_rules import DesignRuleCommands

    pcb = tmp_path / "proj.kicad_pcb"
    _write_pro(tmp_path / "proj.kicad_pro", rules=rules, default_class=default_class)
    return DesignRuleCommands(_FakeBoard(str(pcb)))


@pytest.mark.unit
class TestGetDesignRulesReadsProject:
    """The D1 case: stale in-memory BDS + differing .kicad_pro -> read the .kicad_pro."""

    def test_reads_persisted_minima_not_stale_bds(self, tmp_path):
        cmds = _commands_for(
            tmp_path,
            rules={
                "min_clearance": 0.2,
                "min_track_width": 0.15,
                "min_via_diameter": 0.6,
                "min_through_hole_diameter": 0.25,
            },
            default_class={"track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3},
        )

        out = cmds.get_design_rules({})
        assert out["success"] is True
        r = out["rules"]

        # Persisted minima win over the stale hard-defaults (0 / 0.2 / 0.5 / 0.3).
        assert r["clearance"] == 0.2
        assert r["minTrackWidth"] == 0.15
        assert r["minViaDiameter"] == 0.6
        assert r["minThroughDrill"] == 0.25
        # Default netclass track/via defaults come from the project too.
        assert r["trackWidth"] == 0.25
        assert r["viaDiameter"] == 0.6
        assert r["viaDrill"] == 0.3

    def test_key_absent_from_project_falls_back_to_in_memory(self, tmp_path):
        """A project that only persists min_clearance keeps the in-memory read
        for every other key (graceful, per-key fallback)."""
        cmds = _commands_for(
            tmp_path,
            rules={"min_clearance": 0.3},
            default_class={},  # no track/via overrides
        )

        r = cmds.get_design_rules({})["rules"]
        assert r["clearance"] == 0.3  # overlaid
        # minTrackWidth not in project -> stays the in-memory hard-default 0.2.
        assert r["minTrackWidth"] == 0.2
        # trackWidth not in Default class -> stays in-memory GetCurrentTrackWidth.
        assert r["trackWidth"] == 0.2

    def test_no_project_file_uses_in_memory_only(self, tmp_path):
        """No sibling .kicad_pro -> read degrades to the in-memory BDS, no crash."""
        from commands.design_rules import DesignRuleCommands

        # Board whose sibling .kicad_pro does NOT exist.
        cmds = DesignRuleCommands(_FakeBoard(str(tmp_path / "orphan.kicad_pcb")))

        out = cmds.get_design_rules({})
        assert out["success"] is True
        # Pure in-memory hard-defaults surface unchanged.
        assert out["rules"]["clearance"] == 0.0
        assert out["rules"]["minTrackWidth"] == 0.2

    def test_no_board_loaded_is_clean_failure(self):
        from commands.design_rules import DesignRuleCommands

        out = DesignRuleCommands(None).get_design_rules({})
        assert out["success"] is False
        assert "No board is loaded" in out["message"]
