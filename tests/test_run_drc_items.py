"""run_drc must preserve per-violation items from the kicad-cli JSON report.

kicad-cli's DRC JSON carries, for every violation, an ``items`` list with the
offending objects' descriptions and positions.  run_drc used to keep only the
first item's pos (as ``location``) and drop the rest, so users had to grep the
.kicad_pcb to locate offenders (GD32 E2E finding).  These tests drive run_drc
against a mocked kicad-cli that writes a real-shaped JSON fixture and pin:

  - items[] preserved with description + x/y (mm) + layer where present
  - the maxViolations truncation contract (default 30, 0 = all), mirroring
    run_erc, with the FULL list always written to the violations file
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

# Shaped after real `kicad-cli pcb drc --format json --units mm` output
# (KiCad 10.0.4): violations[].items[] carry description/pos/uuid; pos is
# already mm because of --units mm.
_FIXTURE: Dict[str, Any] = {
    "$schema": "https://schemas.kicad.org/drc.v1.json",
    "coordinate_units": "mm",
    "date": "2026-07-13T00:00:00",
    "source": "board.kicad_pcb",
    "violations": [
        {
            "description": "Board edge clearance violation (board setup constraint "
            "clearance 0.5000 mm; actual 0.2000 mm)",
            "severity": "error",
            "type": "copper_edge_clearance",
            "items": [
                {
                    "description": "Line on Edge.Cuts",
                    "pos": {"x": 0.0, "y": 20.0},
                    "uuid": "aaaa",
                },
                {
                    "description": "Via [GND] (F.Cu - B.Cu)",
                    "pos": {"x": 0.5, "y": 10.0},
                    "uuid": "bbbb",
                },
            ],
        },
        {
            "description": "Via is not connected or connected on only one layer",
            "severity": "error",
            "type": "via_dangling",
            "items": [
                {
                    "description": "Via [GND] (F.Cu - B.Cu)",
                    "pos": {"x": 12.3, "y": 45.6},
                    "uuid": "cccc",
                    "layer": "F.Cu",
                }
            ],
        },
        {
            "description": "Track has unconnected end",
            "severity": "warning",
            "type": "track_dangling",
            "items": [
                {
                    "description": "Track [NET1] (F.Cu), length: 10.0000 mm",
                    "pos": {"x": 5.0, "y": 5.05},
                    "uuid": "dddd",
                }
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
        # Only the JSON invocation writes the fixture; the optional report
        # invocation ("--format report") is a no-op here.
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
def test_violations_carry_items_with_description_and_pos_mm(tmp_path):
    out, _ = _run_drc(tmp_path, {})
    assert out["success"], out
    violations = out["violations"]
    assert len(violations) == 3

    edge = violations[0]
    assert edge["type"] == "copper_edge_clearance"
    assert len(edge["items"]) == 2
    assert edge["items"][0]["description"] == "Line on Edge.Cuts"
    assert edge["items"][0]["pos"] == {"x": 0.0, "y": 20.0, "unit": "mm"}
    assert edge["items"][1]["description"] == "Via [GND] (F.Cu - B.Cu)"
    assert edge["items"][1]["pos"] == {"x": 0.5, "y": 10.0, "unit": "mm"}
    # location keeps pointing at the first item
    assert edge["location"] == {"x": 0.0, "y": 20.0, "unit": "mm"}


@pytest.mark.unit
def test_item_layer_preserved_when_present(tmp_path):
    out, _ = _run_drc(tmp_path, {})
    dangling = out["violations"][1]
    assert dangling["type"] == "via_dangling"
    assert dangling["items"][0]["layer"] == "F.Cu"
    # Items without layer info simply omit the key
    assert "layer" not in out["violations"][2]["items"][0]


@pytest.mark.unit
def test_max_violations_truncates_inline_list(tmp_path):
    out, _ = _run_drc(tmp_path, {"maxViolations": 2})
    assert out["summary"]["total"] == 3
    assert out["summary"]["shown"] == 2
    assert out["summary"]["truncated"] is True
    assert len(out["violations"]) == 2
    assert "showing 2 of 3" in out["message"]


@pytest.mark.unit
def test_max_violations_zero_returns_all(tmp_path):
    out, _ = _run_drc(tmp_path, {"maxViolations": 0})
    assert out["summary"]["shown"] == 3
    assert out["summary"]["truncated"] is False
    assert len(out["violations"]) == 3


@pytest.mark.unit
def test_violations_file_always_has_full_list_with_items(tmp_path):
    out, _ = _run_drc(tmp_path, {"maxViolations": 1})
    assert len(out["violations"]) == 1
    with open(out["violationsFile"], encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["total_violations"] == 3
    assert len(on_disk["violations"]) == 3
    for v in on_disk["violations"]:
        assert v["items"], f"violation {v['type']} lost its items in the file"


@pytest.mark.unit
def test_malformed_items_do_not_crash(tmp_path):
    fixture = {
        "violations": [
            {
                "description": "weird",
                "severity": "error",
                "type": "strange",
                "items": ["not-a-dict", {"description": "ok, no pos"}],
            }
        ]
    }
    out, _ = _run_drc(tmp_path, {}, fixture=fixture)
    assert out["success"], out
    v = out["violations"][0]
    assert v["items"] == [{"description": "ok, no pos"}]
    assert v["location"] == {"x": 0, "y": 0, "unit": "mm"}
