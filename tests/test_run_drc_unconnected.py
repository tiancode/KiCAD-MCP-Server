"""run_drc must surface kicad-cli's SEPARATE ``unconnected_items`` array.

kicad-cli's ``pcb drc`` JSON reports rule ``violations`` in one array and
"Missing connection between items" (unrouted connections) in a *separate*
top-level ``unconnected_items`` array.  run_drc used to parse only the former,
so a board with 283 violations + 69 unconnected items came back reporting 283
and NO signal that 69 connections were unrouted (GD32 E2E finding P4).

These tests pin the additive fix: a ``unconnected`` count + ``unconnectedItems``
sample (same per-item shape as a violation, truncated by maxViolations, full
list always on disk), with every pre-existing violation field unchanged.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.design_rules import DesignRuleCommands  # noqa: E402

# Shaped after real `kicad-cli pcb drc --format json --units mm --severity-all`
# (KiCad 10.0.4): `violations` and `unconnected_items` are sibling top-level
# arrays; each unconnected entry mirrors a violation (type/severity/items[]).
_FIXTURE: Dict[str, Any] = {
    "$schema": "https://schemas.kicad.org/drc.v1.json",
    "coordinate_units": "mm",
    "date": "2026-07-13T00:00:00",
    "source": "board.kicad_pcb",
    "violations": [
        {
            "description": "Via is not connected or connected on only one layer",
            "severity": "error",
            "type": "via_dangling",
            "items": [
                {
                    "description": "Via [GND] (F.Cu - B.Cu)",
                    "pos": {"x": 12.3, "y": 45.6},
                    "uuid": "cccc",
                }
            ],
        },
        {
            "description": "Silkscreen clipped by solder mask",
            "severity": "warning",
            "type": "silk_over_copper",
            "items": [
                {"description": "Text 'REV A'", "pos": {"x": 1.0, "y": 2.0}, "uuid": "eeee"}
            ],
        },
    ],
    "unconnected_items": [
        {
            "description": "Missing connection between items",
            "severity": "error",
            "type": "unconnected_items",
            "items": [
                {
                    "description": "PTH pad A [/ENC_A] of RE1",
                    "pos": {"x": 30.0, "y": 47.0},
                    "uuid": "u1a",
                },
                {
                    "description": "Pad 26 [/ENC_A] of U1 on F.Cu",
                    "pos": {"x": 50.55, "y": 36.0},
                    "uuid": "u1b",
                },
            ],
        },
        {
            "description": "Missing connection between items",
            "severity": "error",
            "type": "unconnected_items",
            "items": [
                {"description": "Pad 1 [/VCC] of C3", "pos": {"x": 5.0, "y": 5.0}, "uuid": "u2a"},
                {"description": "Pad 2 [/VCC] of C4", "pos": {"x": 9.0, "y": 5.0}, "uuid": "u2b"},
            ],
        },
    ],
}


def _run_drc(tmp_path: Path, params: Dict[str, Any], fixture: Dict[str, Any] = _FIXTURE):
    """Drive run_drc with a mocked kicad-cli that writes ``fixture`` JSON."""
    board_file = tmp_path / "board.kicad_pcb"
    board_file.write_text("(kicad_pcb)")

    board = MagicMock()
    board.GetFileName.return_value = str(board_file)
    cmds = DesignRuleCommands(board=board)

    captured_cmds: List[List[str]] = []

    def _fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        if "json" in cmd:
            out = cmd[cmd.index("--output") + 1]
            Path(out).write_text(json.dumps(fixture), encoding="utf-8")
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    with (
        patch("subprocess.run", side_effect=_fake_run),
        patch.object(cmds, "_find_kicad_cli", return_value="/usr/bin/kicad-cli"),
    ):
        out = cmds.run_drc(params)
    return out, captured_cmds


@pytest.mark.unit
def test_unconnected_count_and_items_surfaced(tmp_path):
    out, _ = _run_drc(tmp_path, {})
    assert out["success"], out
    # Violations unchanged: only the 2 real violations, NOT inflated by the
    # unconnected items.
    assert out["summary"]["total"] == 2
    assert len(out["violations"]) == 2
    # The new, separate signal.
    assert out["unconnected"] == 2
    assert out["summary"]["unconnected"] == 2
    assert len(out["unconnectedItems"]) == 2


@pytest.mark.unit
def test_unconnected_items_carry_description_and_mm_pos(tmp_path):
    out, _ = _run_drc(tmp_path, {})
    first = out["unconnectedItems"][0]
    assert first["type"] == "unconnected_items"
    assert first["severity"] == "error"
    assert first["message"] == "Missing connection between items"
    assert len(first["items"]) == 2
    assert first["items"][0]["description"] == "PTH pad A [/ENC_A] of RE1"
    assert first["items"][0]["pos"] == {"x": 30.0, "y": 47.0, "unit": "mm"}
    # location points at the first item's pos (mm), like a violation.
    assert first["location"] == {"x": 30.0, "y": 47.0, "unit": "mm"}


@pytest.mark.unit
def test_message_mentions_unconnected(tmp_path):
    out, _ = _run_drc(tmp_path, {})
    assert "2 DRC violations" in out["message"]
    assert "unconnected" in out["message"].lower()


@pytest.mark.unit
def test_by_type_and_by_severity_exclude_unconnected(tmp_path):
    """Existing violation aggregates must NOT gain the unconnected entries."""
    out, _ = _run_drc(tmp_path, {})
    assert "unconnected_items" not in out["summary"]["by_type"]
    # Only the 2 real violations contribute to severity counts.
    assert out["summary"]["by_severity"] == {"error": 1, "warning": 1, "info": 0}


@pytest.mark.unit
def test_max_violations_truncates_unconnected_sample(tmp_path):
    out, _ = _run_drc(tmp_path, {"maxViolations": 1})
    assert out["unconnected"] == 2
    assert len(out["unconnectedItems"]) == 1
    assert out["unconnectedTruncated"] is True


@pytest.mark.unit
def test_max_violations_zero_returns_all_unconnected(tmp_path):
    out, _ = _run_drc(tmp_path, {"maxViolations": 0})
    assert len(out["unconnectedItems"]) == 2
    assert out["unconnectedTruncated"] is False


@pytest.mark.unit
def test_violations_file_has_full_unconnected_list(tmp_path):
    out, _ = _run_drc(tmp_path, {"maxViolations": 1})
    with open(out["violationsFile"], encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["total_unconnected"] == 2
    assert len(on_disk["unconnected_items"]) == 2
    for uc in on_disk["unconnected_items"]:
        assert uc["items"], "unconnected item lost its offenders in the file"


@pytest.mark.unit
def test_no_unconnected_key_is_zero_not_crash(tmp_path):
    """A report without an ``unconnected_items`` array (older kicad-cli, or a
    fully-routed board) yields a clean zero, not a KeyError."""
    fixture = {"violations": [_FIXTURE["violations"][0]]}
    out, _ = _run_drc(tmp_path, {}, fixture=fixture)
    assert out["success"], out
    assert out["unconnected"] == 0
    assert out["unconnectedItems"] == []
    assert "unconnected" not in out["message"].lower()
