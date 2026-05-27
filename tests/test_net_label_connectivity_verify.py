"""Regression tests for add_schematic_net_label's connectivity verification.

User concern: KiCad requires the label coord to match a pin endpoint
exactly — even a 0.01 mm offset breaks the connection.  The docs hint at
a snapTolerance but the previous implementation ignored it for net
labels, so a near-miss placement silently produced an electrically-
disconnected label.  The handler now:

  1. Auto-snaps raw ``position`` onto the nearest pin within
     ``snapTolerance`` mm (default 0.05 mm).
  2. Always reports ``connected_to_pin = {ref, pin} | null`` so the
     caller can verify the electrical connection without ERC.
  3. Honours ``snapTolerance: 0`` as an explicit opt-out.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


@pytest.fixture(autouse=True)
def _stub_skip_module():
    """tests run without a real ``skip`` install."""
    if "skip" not in sys.modules or not hasattr(sys.modules["skip"], "Schematic"):
        skip_mod = types.ModuleType("skip")

        class _StubSch:
            def __init__(self, path):
                self.path = path
                self.symbol = []

        skip_mod.Schematic = _StubSch
        sys.modules["skip"] = skip_mod


def _stub_pins(monkeypatch, pins):
    """Stub ``_scan_all_pin_positions`` to return the given pin list."""
    from handlers import schematic_wire

    monkeypatch.setattr(
        schematic_wire,
        "_scan_all_pin_positions",
        lambda path: pins,
    )


@patch("commands.wire_manager.WireManager.add_label", return_value=True)
def test_connected_to_pin_populated_on_exact_endpoint(_add, monkeypatch):
    """Label lands exactly on a pin endpoint → ``connected_to_pin`` is
    populated with that pin's reference and number."""
    from handlers.schematic_wire import handle_add_schematic_net_label

    _stub_pins(
        monkeypatch,
        [{"ref": "U1", "pin": "1", "coords": [100.0, 50.0]}],
    )

    out = handle_add_schematic_net_label(
        iface=None,
        params={
            "schematicPath": "/tmp/fake.kicad_sch",
            "netName": "CLK",
            "position": [100.0, 50.0],
        },
    )

    assert out["success"] is True
    assert out["connected_to_pin"] == {"ref": "U1", "pin": "1"}
    assert out["actual_position"] == [100.0, 50.0]
    # No auto-snap fired (already on endpoint), so no snapped_to_pin.
    assert "snapped_to_pin" not in out


@patch("commands.wire_manager.WireManager.add_label", return_value=True)
def test_near_miss_auto_snaps_to_pin(_add, monkeypatch):
    """A 0.03 mm offset (within default 0.05 mm tolerance) should
    auto-snap onto the pin — KiCad would have treated 0.03 mm as
    disconnected without intervention."""
    from handlers.schematic_wire import handle_add_schematic_net_label

    _stub_pins(
        monkeypatch,
        [{"ref": "U1", "pin": "2", "coords": [100.0, 50.0]}],
    )

    out = handle_add_schematic_net_label(
        iface=None,
        params={
            "schematicPath": "/tmp/fake.kicad_sch",
            "netName": "DATA",
            "position": [100.03, 50.0],
        },
    )

    assert out["success"] is True
    # Coordinates were rewritten to the pin endpoint.
    assert out["actual_position"] == [100.0, 50.0]
    # The agent gets the original request + the delta in snapped_to_pin.
    assert out["requested_position"] == [100.03, 50.0]
    assert out["snapped_to_pin"]["component"] == "U1"
    assert out["snapped_to_pin"]["pin"] == "2"
    assert out["snapped_to_pin"]["snap_distance_mm"] == pytest.approx(0.03, abs=1e-6)
    # And connectivity is confirmed at the final coordinates.
    assert out["connected_to_pin"] == {"ref": "U1", "pin": "2"}


@patch("commands.wire_manager.WireManager.add_label", return_value=True)
def test_far_offset_leaves_position_unchanged(_add, monkeypatch):
    """A 5 mm offset is intentional (label between pins).  No snap
    should fire and connected_to_pin must be None."""
    from handlers.schematic_wire import handle_add_schematic_net_label

    _stub_pins(
        monkeypatch,
        [{"ref": "U1", "pin": "1", "coords": [100.0, 50.0]}],
    )

    out = handle_add_schematic_net_label(
        iface=None,
        params={
            "schematicPath": "/tmp/fake.kicad_sch",
            "netName": "BUS",
            "position": [105.0, 50.0],
        },
    )

    assert out["success"] is True
    assert out["actual_position"] == [105.0, 50.0]
    assert "snapped_to_pin" not in out
    assert out["connected_to_pin"] is None


@patch("commands.wire_manager.WireManager.add_label", return_value=True)
def test_snap_tolerance_zero_opts_out(_add, monkeypatch):
    """``snapTolerance: 0`` must skip auto-snap entirely — even a
    near-miss is preserved as-is for callers that want sub-grid placement."""
    from handlers.schematic_wire import handle_add_schematic_net_label

    _stub_pins(
        monkeypatch,
        [{"ref": "U1", "pin": "1", "coords": [100.0, 50.0]}],
    )

    out = handle_add_schematic_net_label(
        iface=None,
        params={
            "schematicPath": "/tmp/fake.kicad_sch",
            "netName": "RAW",
            "position": [100.03, 50.0],
            "snapTolerance": 0,
        },
    )

    assert out["success"] is True
    # Coordinates preserved exactly — no auto-snap.
    assert out["actual_position"] == [100.03, 50.0]
    assert "snapped_to_pin" not in out
    # And connectivity reports the truth: 0.03 mm away from the pin =
    # NOT connected.
    assert out["connected_to_pin"] is None


@patch("commands.wire_manager.WireManager.add_label", return_value=True)
def test_custom_snap_tolerance(_add, monkeypatch):
    """A wider snapTolerance catches a near-miss that the default would miss."""
    from handlers.schematic_wire import handle_add_schematic_net_label

    _stub_pins(
        monkeypatch,
        [{"ref": "U1", "pin": "1", "coords": [100.0, 50.0]}],
    )

    out = handle_add_schematic_net_label(
        iface=None,
        params={
            "schematicPath": "/tmp/fake.kicad_sch",
            "netName": "BIG",
            "position": [100.5, 50.0],  # 0.5 mm — exceeds default 0.05
            "snapTolerance": 1.0,
        },
    )

    assert out["snapped_to_pin"]["component"] == "U1"
    assert out["snapped_to_pin"]["snap_distance_mm"] == pytest.approx(0.5, abs=1e-6)
    assert out["actual_position"] == [100.0, 50.0]


@patch("commands.wire_manager.WireManager.add_label", return_value=True)
def test_componentref_pin_path_also_reports_connected_to_pin(_add, monkeypatch):
    """The preferred componentRef+pinNumber path also surfaces
    connected_to_pin so the caller can verify with a single field
    regardless of which placement mode was used."""
    from handlers.schematic_wire import handle_add_schematic_net_label

    with patch(
        "commands.pin_locator.PinLocator.get_pin_location",
        return_value=[200.0, 75.0],
    ):
        _stub_pins(
            monkeypatch,
            [{"ref": "U2", "pin": "3", "coords": [200.0, 75.0]}],
        )

        out = handle_add_schematic_net_label(
            iface=None,
            params={
                "schematicPath": "/tmp/fake.kicad_sch",
                "netName": "RST",
                "componentRef": "U2",
                "pinNumber": "3",
            },
        )

    assert out["success"] is True
    assert out["actual_position"] == [200.0, 75.0]
    assert out["snapped_to_pin"] == {"component": "U2", "pin": "3"}
    assert out["connected_to_pin"] == {"ref": "U2", "pin": "3"}
