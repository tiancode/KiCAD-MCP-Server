"""
E2E regression for power-symbol / PWR_FLAG net visibility (Phase A finding F3).

The default net queries filter #PWR / #FLG pins by design (PWRFLAG_LABEL_SENTINEL
in commands/wire_connectivity). That left a placed PWR_FLAG / power symbol
impossible to confirm against a net without reading the raw .kicad_sch. These
tests pin the additive side-channels that make attachment verifiable:

  * get_net_connections gains ``power_symbols`` (power ports whose Value names
    the net) and ``power_flags`` (PWR_FLAG markers attached to the net, with the
    attachment kind: "label" | "wire" | "pin_coincident").
  * list_schematic_nets gains a cheap per-net ``has_power_flag`` boolean.

The existing ``connections`` arrays MUST stay unchanged — #PWR / #FLG pins still
excluded.

Style follows tests/test_connection_to_net_stub_direction.py: a real inline
.kicad_sch (power symbols + Device:R), wires/labels laid down with the real
WireManager, then the real query code exercised.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Module loading — bypass pcbnew (skip is real and works fine)
# ---------------------------------------------------------------------------
_PYTHON_DIR = os.path.join(os.path.dirname(__file__), "..", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

sys.modules.setdefault("pcbnew", MagicMock())

from commands.pin_locator import PinLocator  # noqa: E402
from commands.schematic import SchematicManager  # noqa: E402
from commands.wire_connectivity import (  # noqa: E402
    get_power_attachments_for_net,
    resolve_power_flags,
)
from commands.wire_manager import WireManager  # noqa: E402
from handlers.schematic_query import (  # noqa: E402
    handle_get_net_connections,
    handle_list_schematic_nets,
)

# ---------------------------------------------------------------------------
# lib_symbols: a resistor and minimal power / power-flag symbols
# ---------------------------------------------------------------------------
_R_LIB = (
    '(symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))\n'
    '  (symbol "R_1_1"\n'
    '    (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))\n'
    '    (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))))'
)
_PWR5V_LIB = (
    '(symbol "power:+5V" (power) (pin_names (offset 0) hide) (in_bom no) (on_board yes)\n'
    '  (symbol "+5V_1_1"\n'
    '    (pin power_in line (at 0 0 90) (length 0) (name "+5V") (number "1"))))'
)
_PWRGND_LIB = (
    '(symbol "power:GND" (power) (pin_names (offset 0) hide) (in_bom no) (on_board yes)\n'
    '  (symbol "GND_1_1"\n'
    '    (pin power_in line (at 0 0 270) (length 0) (name "GND") (number "1"))))'
)
_PWR12_LIB = (
    '(symbol "power:+12V" (power) (pin_names (offset 0) hide) (in_bom no) (on_board yes)\n'
    '  (symbol "+12V_1_1"\n'
    '    (pin power_in line (at 0 0 90) (length 0) (name "+12V") (number "1"))))'
)
_FLAG_LIB = (
    '(symbol "power:PWR_FLAG" (power) (pin_numbers hide) (pin_names (offset 0) hide)'
    " (in_bom no) (on_board yes)\n"
    '  (symbol "PWR_FLAG_0_0"\n'
    '    (pin power_out line (at 0 0 90) (length 0) (name "pwr") (number "1"))))'
)


def _placed(lib_id: str, ref: str, value: str, x: float, y: float, u: int) -> str:
    return (
        f'  (symbol (lib_id "{lib_id}") (at {x} {y} 0) (unit 1)\n'
        "    (in_bom yes) (on_board yes) (dnp no)\n"
        f'    (uuid "1111111{u}-1111-1111-1111-1111111111aa")\n'
        f'    (property "Reference" "{ref}" (at {x} {y} 0))\n'
        f'    (property "Value" "{value}" (at {x} {y} 0))\n'
        "    (instances\n"
        '      (project "test"\n'
        f'        (path "/00000000-0000-0000-0000-0000000000aa" (reference "{ref}") (unit 1)))))\n'
    )


def _clear_caches() -> None:
    PinLocator._SCHEMATIC_CACHE.clear()
    PinLocator._SEXP_CACHE.clear()
    PinLocator._PINDEF_CACHE.clear()


def _write_base(tmp_path: Path, placements: str, libs: str) -> Path:
    text = (
        '(kicad_sch (version 20250114) (generator "test")\n'
        '  (uuid "00000000-0000-0000-0000-0000000000aa")\n'
        '  (paper "A4")\n'
        "  (lib_symbols\n"
        f"{libs}\n"
        "  )\n"
        f"{placements}"
        '  (sheet_instances (path "/" (page "1")))\n'
        ")\n"
    )
    p = tmp_path / "board.kicad_sch"
    p.write_text(text)
    return p


@pytest.fixture()
def sch(tmp_path: Path) -> Path:
    """A schematic exercising every attachment path.

    * +5V: R1/1 wired to #PWR01 (power port); #FLG01 attached by a "+5V" label
      placed at its own pin (the canonical PWR_FLAG idiom → attachment "label").
    * GND: R1/2 wired to #PWR02 (power port), labeled "GND"; #FLG02 wired to the
      GND port (attachment "wire").
    * SIG: a plain two-pin passive net (R2/1 -- R3/1), no power at all.
    """
    libs = f"    {_R_LIB}\n    {_PWR5V_LIB}\n    {_PWRGND_LIB}\n    {_FLAG_LIB}"
    placements = (
        _placed("Device:R", "R1", "1k", 100, 100, 1)
        + _placed("Device:R", "R2", "2k", 150, 100, 2)
        + _placed("Device:R", "R3", "2k", 150, 120, 3)
        + _placed("power:+5V", "#PWR01", "+5V", 100, 90, 4)
        + _placed("power:GND", "#PWR02", "GND", 100, 112, 5)
        + _placed("power:PWR_FLAG", "#FLG01", "PWR_FLAG", 120, 90, 6)
        + _placed("power:PWR_FLAG", "#FLG02", "PWR_FLAG", 110, 112, 7)
    )
    p = _write_base(tmp_path, placements, libs)

    loc = PinLocator()
    r1p1 = loc.get_pin_location(p, "R1", "1")
    r1p2 = loc.get_pin_location(p, "R1", "2")
    pwr5 = loc.get_pin_location(p, "#PWR01", "1")
    pwrg = loc.get_pin_location(p, "#PWR02", "1")
    flg1 = loc.get_pin_location(p, "#FLG01", "1")
    flg2 = loc.get_pin_location(p, "#FLG02", "1")
    r2p1 = loc.get_pin_location(p, "R2", "1")
    r3p1 = loc.get_pin_location(p, "R3", "1")

    WireManager.add_wire(p, r1p1, pwr5)  # +5V rail
    WireManager.add_label(p, "+5V", flg1)  # flag attached via label at its pin
    WireManager.add_wire(p, r1p2, pwrg)  # GND rail
    WireManager.add_label(p, "GND", r1p2)  # GND net gets a label so it lists
    WireManager.add_wire(p, flg2, pwrg)  # GND flag attached via wire
    WireManager.add_wire(p, r2p1, r3p1)  # SIG two-pin net
    WireManager.add_label(p, "SIG", r2p1)

    _clear_caches()
    return p


def _nets_by_name(result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {n["name"]: n for n in result["nets"]}


# ---------------------------------------------------------------------------
# get_net_connections side-channels
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_net_connections_plus5v_exposes_power_symbol_and_flag(sch: Path) -> None:
    res = handle_get_net_connections(MagicMock(), {"schematicPath": str(sch), "netName": "+5V"})
    assert res["success"]

    # Default array unchanged: only the real resistor pin, no #PWR / #FLG.
    refs = {c["component"] for c in res["connections"]}
    assert {"component": "R1", "pin": "1"} in res["connections"]
    assert not any(r.startswith("#") for r in refs)

    # Side-channels make the power attachment verifiable. #PWR01 is wired to
    # R1/1, so it is NOT floating (F6).
    assert res["power_symbols"] == [
        {"ref": "#PWR01", "pin": "1", "value": "+5V", "floating": False}
    ]
    assert res["power_flags"] == [{"ref": "#FLG01", "pin": "1", "attachment": "label"}]
    # A wired power symbol raises no floating warning.
    assert "warnings" not in res


@pytest.mark.unit
def test_get_net_connections_gnd_flag_attached_by_wire(sch: Path) -> None:
    res = handle_get_net_connections(MagicMock(), {"schematicPath": str(sch), "netName": "GND"})
    assert res["success"]

    refs = {c["component"] for c in res["connections"]}
    assert {"component": "R1", "pin": "2"} in res["connections"]
    assert not any(r.startswith("#") for r in refs)

    assert res["power_symbols"] == [
        {"ref": "#PWR02", "pin": "1", "value": "GND", "floating": False}
    ]
    assert res["power_flags"] == [{"ref": "#FLG02", "pin": "1", "attachment": "wire"}]


@pytest.mark.unit
def test_get_net_connections_plain_net_has_empty_power_channels(sch: Path) -> None:
    res = handle_get_net_connections(MagicMock(), {"schematicPath": str(sch), "netName": "SIG"})
    assert res["success"]
    assert {"component": "R2", "pin": "1"} in res["connections"]
    assert {"component": "R3", "pin": "1"} in res["connections"]
    assert res["power_symbols"] == []
    assert res["power_flags"] == []


# ---------------------------------------------------------------------------
# list_schematic_nets has_power_flag boolean
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_schematic_nets_has_power_flag_boolean(sch: Path) -> None:
    res = handle_list_schematic_nets(MagicMock(), {"schematicPath": str(sch)})
    assert res["success"]
    nets = _nets_by_name(res)

    # The listing must not bloat with pin dumps — just the cheap boolean plus
    # the pre-existing keys.
    assert set(nets) == {"+5V", "GND", "SIG"}
    assert nets["+5V"]["has_power_flag"] is True
    assert nets["GND"]["has_power_flag"] is True
    assert nets["SIG"]["has_power_flag"] is False

    # "PWR_FLAG" is never itself a net.
    assert "PWR_FLAG" not in nets

    # Existing per-net contract preserved: connections still exclude #PWR/#FLG.
    for net in nets.values():
        assert not any(c["component"].startswith("#") for c in net["connections"])


# ---------------------------------------------------------------------------
# Lower-level helpers exercised directly
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_power_flags_reports_net_and_attachment(sch: Path) -> None:
    _clear_caches()
    schematic = SchematicManager.load_schematic(str(sch))
    flags = resolve_power_flags(schematic, str(sch))
    by_ref = {f["ref"]: f for f in flags}

    assert by_ref["#FLG01"]["net"] == "+5V"
    assert by_ref["#FLG01"]["attachment"] == "label"
    assert by_ref["#FLG02"]["net"] == "GND"
    assert by_ref["#FLG02"]["attachment"] == "wire"
    # "PWR_FLAG" literal never surfaces as a net name.
    assert all(f["net"] != "PWR_FLAG" for f in flags)


@pytest.mark.unit
def test_get_power_attachments_for_net_filters_by_net(sch: Path) -> None:
    _clear_caches()
    schematic = SchematicManager.load_schematic(str(sch))

    plus5 = get_power_attachments_for_net(schematic, str(sch), "+5V")
    assert plus5["power_symbols"] == [
        {"ref": "#PWR01", "pin": "1", "value": "+5V", "floating": False}
    ]
    assert plus5["power_flags"] == [{"ref": "#FLG01", "pin": "1", "attachment": "label"}]

    # A power port on a DIFFERENT rail must not leak into +5V.
    assert all(ps["ref"] != "#PWR02" for ps in plus5["power_symbols"])


# ---------------------------------------------------------------------------
# pin_coincident attachment: a PWR_FLAG placed directly on a power port pin
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_flag_coincident_with_power_port_pin(tmp_path: Path) -> None:
    """#FLG placed on a power port's pin (no wire, no label) → pin_coincident,
    resolving to the port's Value net."""
    libs = f"    {_PWR12_LIB}\n    {_FLAG_LIB}"
    placements = _placed("power:+12V", "#PWR03", "+12V", 80, 80, 8) + _placed(
        "power:PWR_FLAG", "#FLG03", "PWR_FLAG", 80, 80, 9
    )
    p = _write_base(tmp_path, placements, libs)
    _clear_caches()

    schematic = SchematicManager.load_schematic(str(p))
    flags = resolve_power_flags(schematic, str(p))
    assert flags == [{"ref": "#FLG03", "pin": "1", "net": "+12V", "attachment": "pin_coincident"}]

    attach = get_power_attachments_for_net(schematic, str(p), "+12V")
    # #PWR03 shares its pin with #FLG03 (coincident) → physically connected.
    assert attach["power_symbols"] == [
        {"ref": "#PWR03", "pin": "1", "value": "+12V", "floating": False}
    ]
    assert attach["power_flags"] == [{"ref": "#FLG03", "pin": "1", "attachment": "pin_coincident"}]
