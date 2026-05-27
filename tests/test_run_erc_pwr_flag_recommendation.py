"""Regression test for the PWR_FLAG ERC recommendation.

User report: ``run_erc`` returns 2 errors on a brand-new NE555 design
("Input Power pin not driven on net GND / VCC") and the MCP never says
the fix is "add power:PWR_FLAG to each rail".  The agent ends up either
ignoring real wiring bugs (because they look the same as PWR_FLAG FPs)
or thrashing through trial-and-error.

The handler now collects PWR_FLAG-fixable violations, extracts the net
names from kicad-cli's descriptions, and surfaces a single structured
``summary.recommendations[]`` entry with the affected nets and a
concrete add_schematic_component example.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _run(monkeypatch, tmp_path, erc_data, schematic_text="(kicad_sch)\n"):
    """Drive handle_run_erc with canned kicad-cli output + a schematic file."""
    from handlers.schematic_io import handle_run_erc
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.design_rule_commands = MagicMock()
    iface.design_rule_commands._find_kicad_cli = MagicMock(return_value="/fake/kicad-cli")

    sch = tmp_path / "demo.kicad_sch"
    sch.write_text(schematic_text, encoding="utf-8")

    def _fake_subprocess_run(cmd, **kw):
        out_path = cmd[cmd.index("--output") + 1]
        Path(out_path).write_text(json.dumps(erc_data), encoding="utf-8")
        return MagicMock(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("subprocess.run", _fake_subprocess_run)
    return handle_run_erc(iface, {"schematicPath": str(sch)})


def _pwr_violation(net_name: str):
    """Build the canonical kicad-cli pin_not_driven violation shape."""
    return {
        "description": (
            f"Input Power pin not driven by any Output Power pins on net {net_name}"
        ),
        "severity": "error",
        "type": "pin_not_driven",
        "items": [{"description": f"NE555 GND pin", "pos": {"x": 1.0, "y": 1.0}}],
    }


def _schematic_with_power_labels(*names: str) -> str:
    """Minimal .kicad_sch carrying the given power-symbol labels (so the
    _collect_power_label_names heuristic finds them)."""
    body = " ".join(
        f'(symbol (lib_id "power:{n}") (property "Value" "{n}"))' for n in names
    )
    return f"(kicad_sch {body})"


# ---------------------------------------------------------------------------
# Recommendation is emitted with the affected nets
# ---------------------------------------------------------------------------
def test_recommendation_lists_nets_from_violation_messages(monkeypatch, tmp_path):
    """User's exact reproduction: NE555 GND and VCC, both flagged.  The
    response must include a single add_pwr_flag recommendation listing
    both nets and a concrete tool-call hint."""
    erc = {
        "coordinate_units": "mm",
        "sheets": [
            {
                "violations": [
                    _pwr_violation("GND"),
                    _pwr_violation("VCC"),
                ]
            }
        ],
    }

    out = _run(
        monkeypatch,
        tmp_path,
        erc,
        schematic_text=_schematic_with_power_labels("GND", "VCC"),
    )

    assert out["success"] is True
    summary = out["summary"]
    # Both violations were demoted out of the real-error bucket.
    assert summary["likely_false_positives"] == 2
    assert summary["real_errors"] == 0

    recs = summary["recommendations"]
    assert len(recs) == 1
    rec = recs[0]
    assert rec["kind"] == "add_pwr_flag"
    assert set(rec["nets"]) == {"GND", "VCC"}
    # The action text names the concrete tool the agent should call.
    assert "add_schematic_component" in rec["action"]
    assert "PWR_FLAG" in rec["action"]
    assert "library:'power'" in rec["action"]


def test_recommendation_absent_when_no_pwrflag_fps(monkeypatch, tmp_path):
    """A schematic that triggers OTHER kinds of violations (e.g. real
    wiring bugs) must NOT receive the PWR_FLAG recommendation."""
    erc = {
        "sheets": [
            {
                "violations": [
                    {
                        "description": "Wires not connected",
                        "severity": "error",
                        "type": "wires_not_connected",
                        "items": [{"pos": {"x": 1.0, "y": 1.0}}],
                    }
                ]
            }
        ],
    }

    out = _run(monkeypatch, tmp_path, erc)

    assert out["success"] is True
    assert out["summary"]["likely_false_positives"] == 0
    assert out["summary"]["recommendations"] == []


def test_recommendation_falls_back_to_schematic_labels_when_extraction_fails(
    monkeypatch, tmp_path
):
    """When the violation message lacks a clear 'on net X' phrasing but
    still names a power-keyword somewhere (so _violation_mentions_power_label
    still tags it as a likely FP), the recommendation falls back to the
    set of power labels found in the schematic itself."""
    erc = {
        "sheets": [
            {
                "violations": [
                    {
                        # No "on net X" — but the message still contains
                        # "GND" so the FP heuristic tags it.  Net
                        # extraction returns None; the recommendation
                        # then takes its nets from power_label_names.
                        "description": (
                            "Input Power pin not driven; check GND wiring"
                        ),
                        "severity": "error",
                        "type": "pin_not_driven",
                        "items": [{"pos": {"x": 1.0, "y": 1.0}}],
                    }
                ]
            }
        ],
    }
    sch_text = _schematic_with_power_labels("GND", "+3V3")

    out = _run(monkeypatch, tmp_path, erc, schematic_text=sch_text)

    recs = out["summary"]["recommendations"]
    assert len(recs) == 1
    # Fallback uses the schematic's power labels (no specific net from msg).
    assert set(recs[0]["nets"]) == {"GND", "+3V3"}


def test_violation_carries_net_field_when_extracted(monkeypatch, tmp_path):
    """The per-violation entry gets a structured ``net`` field too, so
    agents that walk violations individually still see which rail each
    one belongs to without re-parsing the description."""
    erc = {
        "sheets": [
            {
                "violations": [_pwr_violation("VBUS")],
            }
        ],
    }

    out = _run(monkeypatch, tmp_path, erc, schematic_text=_schematic_with_power_labels("VBUS"))

    [v] = out["violations"]
    assert v["likely_false_positive"] is True
    assert v["net"] == "VBUS"


def test_net_extraction_handles_plus_minus_and_underscore_names(monkeypatch, tmp_path):
    """Power nets often have non-alphanumeric chars (+3V3, +VBATT, USB-D+).
    The regex must capture them intact."""
    from handlers.schematic_io import _extract_net_from_violation

    cases = [
        ("Input Power pin not driven by any Output Power pins on net +3V3", "+3V3"),
        ("Input Power pin not driven by any Output Power pins on net GND", "GND"),
        ("Input Power pin not driven by any Output Power pins on net VCC_3V3", "VCC_3V3"),
        ("Input Power pin not driven by any Output Power pins on net -12V", "-12V"),
    ]
    for msg, expected in cases:
        assert _extract_net_from_violation(msg) == expected, msg
