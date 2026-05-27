"""Regression test for ``get_pad_position`` parameter-name drift.

The TS schema names the argument ``pad`` and that's what every MCP client
sends.  The SWIG handler historically read ``padName`` / ``padNumber``,
so the documented call ``{reference: 'U1', pad: '1'}`` returned a
"Missing pad identifier" error.  The handler now accepts all three.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _make_component_commands(pad_by_number=None):
    """Construct a ComponentCommands with a fake board exposing one pad."""
    from commands.component import ComponentCommands

    pad_by_number = pad_by_number or {}

    fake_pad = MagicMock()
    fake_pad.GetNumber = MagicMock(return_value="1")
    fake_pad.GetPosition = MagicMock(
        return_value=SimpleNamespace(x=10_000_000, y=20_000_000)
    )
    fake_pad.GetSize = MagicMock(
        return_value=SimpleNamespace(x=1_000_000, y=1_000_000)
    )
    fake_pad.GetNetname = MagicMock(return_value="GND")
    fake_pad.GetNetCode = MagicMock(return_value=42)

    fake_fp = MagicMock()
    fake_fp.FindPadByNumber = MagicMock(
        side_effect=lambda n: fake_pad if str(n) == "1" else None
    )
    fake_fp.Pads = MagicMock(return_value=[fake_pad])

    fake_board = MagicMock()
    fake_board.FindFootprintByReference = MagicMock(
        side_effect=lambda ref: fake_fp if ref == "U1" else None
    )

    cc = ComponentCommands.__new__(ComponentCommands)
    cc.board = fake_board
    return cc, fake_pad


def test_pad_param_accepted_as_documented():
    """The TS schema sends ``pad``; the handler must read it."""
    cc, _ = _make_component_commands()

    out = cc.get_pad_position({"reference": "U1", "pad": "1"})

    assert out["success"] is True
    assert out["position"]["x"] == 10
    assert out["position"]["y"] == 20


def test_pad_name_legacy_alias_still_works():
    cc, _ = _make_component_commands()

    out = cc.get_pad_position({"reference": "U1", "padName": "1"})

    assert out["success"] is True


def test_pad_number_legacy_alias_still_works():
    cc, _ = _make_component_commands()

    out = cc.get_pad_position({"reference": "U1", "padNumber": "1"})

    assert out["success"] is True


def test_missing_all_three_keys_returns_actionable_error():
    """Error message must name the canonical ``pad`` (the schema-documented
    key) so callers reading the error see the right field, not just the
    legacy fallbacks."""
    cc, _ = _make_component_commands()

    out = cc.get_pad_position({"reference": "U1"})

    assert out["success"] is False
    assert "pad" in out["errorDetails"]


def test_pad_takes_precedence_when_multiple_given():
    """If a caller passes ``pad`` and ``padName`` with different values,
    the canonical ``pad`` wins (matches "the docs are authoritative")."""
    cc, _ = _make_component_commands()

    out = cc.get_pad_position(
        {"reference": "U1", "pad": "1", "padName": "DOES-NOT-EXIST"}
    )

    assert out["success"] is True
