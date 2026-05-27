"""Regression test for ERC coordinate-unit scaling.

kicad-cli 10.0.3 emits a schematic ERC JSON whose header claims
``coordinate_units: "mm"`` but ``items[].pos`` is actually serialised
as schematic IU/10000.  A symbol at (129.84, 94.92) mm comes back as
(1.2984, 0.9492) — 100× too small.  The handler must re-scale so the
``violations[].location`` field reports millimetres consistent with
the rest of the schematic API.

Empirically captured from `kicad-cli sch erc --format json` against
KiCad 10.0.3 on Linux; revisit if upstream fixes the writer.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


# Real output captured from kicad-cli 10.0.3 for a schematic with one
# unconnected resistor placed at (129.84, 94.92) mm.  Multiple violation
# types verify the scaling fires uniformly across pin_not_connected,
# endpoint_off_grid, and lib_symbol_mismatch reports.
_KICAD_CLI_ERC_OUTPUT = {
    "$schema": "https://schemas.kicad.org/erc.v1.json",
    "coordinate_units": "mm",  # ← LIES; pos is IU/10000 not mm
    "kicad_version": "10.0.3",
    "sheets": [
        {
            "path": "/",
            "violations": [
                {
                    "description": "Pin not connected",
                    "severity": "error",
                    "type": "pin_not_connected",
                    "items": [{"pos": {"x": 1.2984, "y": 0.9111}}],
                },
                {
                    "description": "Symbol pin or wire end off connection grid",
                    "severity": "warning",
                    "type": "endpoint_off_grid",
                    "items": [{"pos": {"x": 1.2984, "y": 0.9111}}],
                },
                {
                    "description": "Symbol mismatch",
                    "severity": "warning",
                    "type": "lib_symbol_mismatch",
                    "items": [{"pos": {"x": 1.2984, "y": 0.9492}}],
                },
            ],
        }
    ],
}


@pytest.fixture
def fake_iface():
    from kicad_interface import KiCADInterface

    obj = KiCADInterface.__new__(KiCADInterface)
    obj.design_rule_commands = MagicMock()
    obj.design_rule_commands._find_kicad_cli = MagicMock(return_value="/fake/kicad-cli")
    return obj


def _run_handler(monkeypatch, tmp_path, iface, erc_data):
    """Invoke handle_run_erc with the canned kicad-cli output."""
    from handlers.schematic_io import handle_run_erc

    sch = tmp_path / "demo.kicad_sch"
    sch.write_text("(kicad_sch)\n", encoding="utf-8")

    # Capture the kicad-cli output path the handler chose, write the
    # canned JSON there before subprocess.run "returns".
    captured = {}

    def _fake_subprocess_run(cmd, **kw):
        # cmd = [cli, "sch", "erc", "--format", "json", "--output", <tmp>, <sch>]
        out_path = cmd[cmd.index("--output") + 1]
        captured["output_path"] = out_path
        Path(out_path).write_text(json.dumps(erc_data), encoding="utf-8")
        return MagicMock(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("subprocess.run", _fake_subprocess_run)
    return handle_run_erc(iface, {"schematicPath": str(sch)})


def test_erc_pos_scaled_from_iu_div_10000_to_mm(fake_iface, monkeypatch, tmp_path):
    """The user's symbol at 129.84/94.92 mm reported as 1.2984/0.9492 by
    kicad-cli; the handler must scale ×100 and tag the unit."""
    out = _run_handler(monkeypatch, tmp_path, fake_iface, _KICAD_CLI_ERC_OUTPUT)

    assert out["success"] is True
    locs = [v["location"] for v in out["violations"]]
    # First two violations: pin at (129.84, 91.11) mm
    assert locs[0]["x"] == pytest.approx(129.84)
    assert locs[0]["y"] == pytest.approx(91.11)
    assert locs[0]["unit"] == "mm"
    assert locs[1]["x"] == pytest.approx(129.84)
    assert locs[1]["y"] == pytest.approx(91.11)
    # Third: symbol body at (129.84, 94.92) mm
    assert locs[2]["x"] == pytest.approx(129.84)
    assert locs[2]["y"] == pytest.approx(94.92)


def test_erc_violation_without_items_gives_empty_location(fake_iface, monkeypatch, tmp_path):
    """ERC violations without `items` (e.g. global warnings) must not
    crash on the scaling step — they just carry an empty location dict."""
    data = {
        "sheets": [
            {
                "violations": [
                    {
                        "description": "no driver",
                        "severity": "error",
                        "type": "pin_not_driven",
                        # no items
                    },
                ]
            }
        ],
    }

    out = _run_handler(monkeypatch, tmp_path, fake_iface, data)

    assert out["success"] is True
    assert out["violations"][0]["location"] == {}


def test_erc_top_level_violations_array_also_scaled(fake_iface, monkeypatch, tmp_path):
    """KiCad 8 nests violations at the top level; KiCad 9+ nests them
    under sheets[].  The handler reads both — make sure the scaling
    applies to the top-level path too."""
    data = {
        "violations": [
            {
                "description": "Pin not connected",
                "severity": "error",
                "type": "pin_not_connected",
                "items": [{"pos": {"x": 0.5, "y": 0.5}}],
            }
        ],
        "sheets": [],
    }

    out = _run_handler(monkeypatch, tmp_path, fake_iface, data)

    assert out["violations"][0]["location"]["x"] == pytest.approx(50.0)
    assert out["violations"][0]["location"]["y"] == pytest.approx(50.0)
    assert out["violations"][0]["location"]["unit"] == "mm"
