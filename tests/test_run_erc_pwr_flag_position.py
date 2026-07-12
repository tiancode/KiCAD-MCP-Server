"""Regression test for KiCad 10.x position-based PWR_FLAG false-positive tagging.

Bug: on KiCad 10.0.4 the ``power_pin_not_driven`` ERC JSON carries neither a
net name in the description nor a per-item ``net`` field — only the pin
position (``items[].pos``, serialised as IU/10000).  The 9.x heuristic
(``_violation_mentions_power_label``) therefore never fires, so a label-driven
power rail with no PWR_FLAG surfaced as a hard error with no recommendation,
violating the documented "PWR_FLAG issues are excluded from real_errors"
contract.

The handler now resolves the offending pin's net from its position: it walks
the wire network out from the (rescaled) pin location to the label that names
the net, then applies the same "labeled power net with no PWR_FLAG driver"
rule the 9.x path uses.  These tests pin that behavior AND the guards that keep
it from masking genuine problems.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _run(monkeypatch, tmp_path, erc_data, schematic_text):
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
    # autoRefreshLibSymbols would try to touch the real library manager; the
    # canned schematics have no lib_symbols block so skip it.
    return handle_run_erc(iface, {"schematicPath": str(sch), "autoRefreshLibSymbols": False})


def _stub(name: str, pin_x_mm: float, pin_y_mm: float, label_x_mm: float, label_y_mm: float) -> str:
    """A pin<->label wire stub as connect_to_net writes it."""
    return (
        f"(wire (pts (xy {pin_x_mm} {pin_y_mm}) (xy {label_x_mm} {label_y_mm})) "
        f'(stroke (width 0) (type default)) (uuid "w-{name}-{pin_x_mm}"))'
        f'(label "{name}" (at {label_x_mm} {label_y_mm} 0) '
        f'(effects (font (size 1.27 1.27))) (uuid "l-{name}-{label_x_mm}"))'
    )


def _pwr_not_driven(pin_x_mm: float, pin_y_mm: float, pin_name: str = "GND") -> dict:
    """A KiCad 10.x power_pin_not_driven violation: generic message, no net,
    pos serialised as IU/10000 (the handler rescales ×100 back to mm)."""
    return {
        "description": "Input Power pin not driven by any Output Power pins",
        "severity": "error",
        "type": "power_pin_not_driven",
        # Localised item description quoting the pin name in brackets — the
        # exact shape kicad-cli 10.0.4 emits (never the net name).
        "items": [
            {
                "description": f"Symbol U1 pin N [{pin_name}, power input, wire]",
                "pos": {"x": pin_x_mm / 100.0, "y": pin_y_mm / 100.0},
                "uuid": "v-uuid",
            }
        ],
    }


# ---------------------------------------------------------------------------
# The reported bug: 10.x GND/VCC violations must resolve by position.
# ---------------------------------------------------------------------------
def test_kicad10_no_net_resolved_by_position(monkeypatch, tmp_path):
    """Exact reproduction from KiCad 10.0.4: two power_pin_not_driven
    violations with no net name, each pin wired to a GND/VCC label.  Both must
    be tagged likely_false_positive, demoted out of real_errors, and drive a
    single add_pwr_flag recommendation naming GND and VCC."""
    sch = (
        "(kicad_sch "
        + _stub("GND", 100.33, 110.49, 100.33, 113.03)
        + _stub("VCC", 102.87, 90.17, 102.87, 87.63)
        + ")"
    )
    erc = {
        "coordinate_units": "mm",
        "sheets": [
            {
                "violations": [
                    _pwr_not_driven(100.33, 110.49, "GND"),
                    _pwr_not_driven(102.87, 90.17, "VCC"),
                ]
            }
        ],
    }

    out = _run(monkeypatch, tmp_path, erc, sch)

    assert out["success"] is True
    s = out["summary"]
    assert s["total"] == 2
    assert s["likely_false_positives"] == 2
    assert s["real_errors"] == 0
    assert s["raw_by_severity"]["error"] == 2  # kicad-cli original preserved

    recs = s["recommendations"]
    assert len(recs) == 1
    assert recs[0]["kind"] == "add_pwr_flag"
    assert set(recs[0]["nets"]) == {"GND", "VCC"}

    by_net = {v.get("net"): v for v in out["violations"]}
    assert set(by_net) == {"GND", "VCC"}
    for v in out["violations"]:
        assert v["likely_false_positive"] is True
        assert "resolves to power net" in v["reason"]


def test_direct_resolver_walks_wire_to_label(monkeypatch, tmp_path):
    """Unit-level check of the geometry resolver used by the handler."""
    from handlers.schematic_io._erc import _collect_net_label_geometry, _resolve_net_via_geometry

    sch = tmp_path / "geo.kicad_sch"
    sch.write_text("(kicad_sch " + _stub("GND", 100.33, 110.49, 100.33, 113.03) + ")")
    labels, wires, pwr_flags = _collect_net_label_geometry(str(sch))
    assert ("GND", 100.33, 113.03) in labels
    assert wires and pwr_flags == []
    # Pin end of the stub resolves through the wire to the GND label.
    assert _resolve_net_via_geometry(100.33, 110.49, labels, wires) == "GND"
    # A point nowhere near any wire/label stays unresolved.
    assert _resolve_net_via_geometry(5.0, 5.0, labels, wires) is None


# ---------------------------------------------------------------------------
# Guards: do NOT mask genuine problems.
# ---------------------------------------------------------------------------
def test_power_pin_on_signal_net_stays_real_error(monkeypatch, tmp_path):
    """A power-input pin wired onto a NON-power net (a signal rail) is a real
    design error — the position resolves to a non-power label, so it must NOT
    be tagged as a PWR_FLAG false positive."""
    sch = "(kicad_sch " + _stub("DATA0", 100.33, 110.49, 100.33, 113.03) + ")"
    erc = {"sheets": [{"violations": [_pwr_not_driven(100.33, 110.49, "GND")]}]}

    out = _run(monkeypatch, tmp_path, erc, sch)

    assert out["summary"]["likely_false_positives"] == 0
    assert out["summary"]["real_errors"] == 1
    assert out["summary"]["recommendations"] == []


def test_net_with_pwr_flag_stays_real_error(monkeypatch, tmp_path):
    """When the flagged net already carries a PWR_FLAG yet ERC still complains,
    something else is wrong — do not silently mask it."""
    # GND has a PWR_FLAG placed right on its label; VCC does not.
    sch = (
        "(kicad_sch "
        + _stub("GND", 100.33, 110.49, 100.33, 113.03)
        + _stub("VCC", 102.87, 90.17, 102.87, 87.63)
        + '(symbol (lib_id "power:PWR_FLAG") (at 100.33 113.03 0) '
        '(property "Reference" "#FLG01") (property "Value" "PWR_FLAG"))' + ")"
    )
    erc = {
        "sheets": [
            {
                "violations": [
                    _pwr_not_driven(100.33, 110.49, "GND"),  # has PWR_FLAG -> real
                    _pwr_not_driven(102.87, 90.17, "VCC"),  # no PWR_FLAG -> FP
                ]
            }
        ]
    }

    out = _run(monkeypatch, tmp_path, erc, sch)

    s = out["summary"]
    assert s["likely_false_positives"] == 1
    assert s["real_errors"] == 1
    tagged = [v for v in out["violations"] if v.get("likely_false_positive")]
    assert len(tagged) == 1 and tagged[0]["net"] == "VCC"


def test_floating_power_pin_no_labels_stays_real_error(monkeypatch, tmp_path):
    """A genuinely floating power pin (no nearby label, no power rails in the
    schematic at all) must remain a real error — nothing to fall back on."""
    sch = "(kicad_sch )"
    erc = {"sheets": [{"violations": [_pwr_not_driven(100.33, 110.49, "GND")]}]}

    out = _run(monkeypatch, tmp_path, erc, sch)

    assert out["summary"]["likely_false_positives"] == 0
    assert out["summary"]["real_errors"] == 1


# ---------------------------------------------------------------------------
# Fallback: position unresolved but power rails + no PWR_FLAG anywhere.
# ---------------------------------------------------------------------------
def test_fallback_when_position_unresolved_but_power_rails_present(monkeypatch, tmp_path):
    """If the pin position can't be matched to any wire/label but the schematic
    clearly has power rails and no PWR_FLAG symbols, the violation is still the
    PWR_FLAG false-positive class.  The recommendation lists the power rails
    (only the power-ish labels, not signal nets)."""
    # Labels exist but nowhere near the violation position (which is (10, 10)).
    sch = (
        "(kicad_sch "
        + _stub("GND", 100.33, 110.49, 100.33, 113.03)
        + _stub("+3V3", 102.87, 90.17, 102.87, 87.63)
        + _stub("SIGOUT", 60.0, 60.0, 60.0, 62.0)  # non-power label, must be excluded
        + ")"
    )
    erc = {"sheets": [{"violations": [_pwr_not_driven(10.0, 10.0, "GND")]}]}

    out = _run(monkeypatch, tmp_path, erc, sch)

    s = out["summary"]
    assert s["likely_false_positives"] == 1
    assert s["real_errors"] == 0
    recs = s["recommendations"]
    assert len(recs) == 1
    assert set(recs[0]["nets"]) == {"GND", "+3V3"}  # SIGOUT excluded (not power-ish)
    assert "could not be matched" in out["violations"][0]["reason"]


def test_fallback_declines_when_pwr_flag_present(monkeypatch, tmp_path):
    """The fallback must NOT fire when a PWR_FLAG already exists somewhere —
    an unresolved position with flags around is ambiguous, so keep it a real
    error rather than mask a possible bug."""
    sch = (
        "(kicad_sch "
        + _stub("GND", 100.33, 110.49, 100.33, 113.03)
        + '(symbol (lib_id "power:PWR_FLAG") (at 100.33 113.03 0) '
        '(property "Value" "PWR_FLAG"))' + ")"
    )
    erc = {"sheets": [{"violations": [_pwr_not_driven(10.0, 10.0, "GND")]}]}

    out = _run(monkeypatch, tmp_path, erc, sch)

    assert out["summary"]["likely_false_positives"] == 0
    assert out["summary"]["real_errors"] == 1
