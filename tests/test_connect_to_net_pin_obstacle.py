"""
E2E round-6 regressions for the connect_to_net family.

S3 (critical): connect_to_net must NEVER merge the requested net into a
neighbour's net. The pre-fix collision check only looked at existing net labels
/ power pins, so a stub/label dropped exactly on another component's *bare* pin
(no net yet) silently shorted that pin onto the net. With two facing pins
<=2.54 mm apart the default outward stub lands right on the neighbour's pin.
The fix adds every placed component pin as an obstacle: the stub is relocated to
a free direction, or the call refuses with the documented label_collision
payload — it never shorts.

S5: connect_to_net must not emit two overlapping (coincident) wire objects for
one stub.

S10: a missing COMPONENT and a missing PIN produce distinct error messages.

Real .kicad_sch files + real WireManager (no mocking) so the written geometry is
re-parsed and net membership verified via the project's own connectivity code.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import sexpdata
from sexpdata import Symbol

_PYTHON_DIR = os.path.join(os.path.dirname(__file__), "..", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

from commands.connection_schematic import ConnectionManager  # noqa: E402
from commands.pin_locator import PinLocator  # noqa: E402

_R_LIB = (
    '(symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))\n'
    '  (symbol "R_1_1"\n'
    '    (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))\n'
    '    (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))))'
)


def _clear() -> None:
    PinLocator._SCHEMATIC_CACHE.clear()
    PinLocator._SEXP_CACHE.clear()
    PinLocator._PINDEF_CACHE.clear()


def _placed(ref: str, x: float, y: float, u: int) -> str:
    return (
        f'  (symbol (lib_id "Device:R") (at {x} {y} 0) (unit 1)\n'
        "    (in_bom yes) (on_board yes) (dnp no)\n"
        f'    (uuid "1111111{u}-1111-1111-1111-1111111111aa")\n'
        f'    (property "Reference" "{ref}" (at {x} {y} 0))\n'
        f'    (property "Value" "10k" (at {x} {y} 0))\n'
        "    (instances\n"
        '      (project "t"\n'
        f'        (path "/00000000-0000-0000-0000-0000000000aa" (reference "{ref}") (unit 1)))))\n'
    )


def _build(tmp_path: Path, body: str) -> Path:
    text = (
        '(kicad_sch (version 20250114) (generator "test")\n'
        '  (uuid "00000000-0000-0000-0000-0000000000aa")\n'
        '  (paper "A4")\n'
        f"  (lib_symbols\n    {_R_LIB}\n  )\n"
        + body
        + '  (sheet_instances (path "/" (page "1")))\n'
        ")\n"
    )
    p = tmp_path / "board.kicad_sch"
    p.write_text(text)
    _clear()
    return p


def _labels(p: Path) -> list:
    data = sexpdata.loads(p.read_text())
    out = []
    for item in data:
        if isinstance(item, list) and item and item[0] == Symbol("label"):
            at = next(
                (s for s in item[1:] if isinstance(s, list) and s and s[0] == Symbol("at")), None
            )
            if at is not None:
                out.append((str(item[1]), (round(float(at[1]), 3), round(float(at[2]), 3))))
    return out


def _wires(p: Path) -> list:
    data = sexpdata.loads(p.read_text())
    out = []
    for item in data:
        if not (isinstance(item, list) and item and item[0] == Symbol("wire")):
            continue
        pts = next(
            (s for s in item[1:] if isinstance(s, list) and s and s[0] == Symbol("pts")), None
        )
        if pts is None:
            continue
        xy = [q for q in pts[1:] if isinstance(q, list) and len(q) >= 3 and q[0] == Symbol("xy")]
        if len(xy) >= 2:
            out.append(
                (
                    (round(float(xy[0][1]), 3), round(float(xy[0][2]), 3)),
                    (round(float(xy[-1][1]), 3), round(float(xy[-1][2]), 3)),
                )
            )
    return out


def _net(p: Path, net_name: str) -> set:
    """Component pins the project's connectivity code sees on ``net_name``."""
    from skip import Schematic

    _clear()
    sch = Schematic(str(p))
    conns = ConnectionManager.get_net_connections(sch, net_name, p)
    return {(c["component"], c["pin"]) for c in conns}


# ===========================================================================
# S3 — integration: facing pins never short
# ===========================================================================


@pytest.mark.unit
def test_s3_facing_pins_2p54mm_apart_do_not_short(tmp_path: Path) -> None:
    """The exact E2E repro: R7/pin2 (11.43,43.18) and R8/pin1 (11.43,45.72) are
    2.54 mm apart. connect(R7/2,GND) then connect(R8/1,CC2) must leave GND and
    CC2 as SEPARATE nets (the pre-fix code merged both into one)."""
    # R7 at (11.43, 39.37): pin2 world (11.43, 43.18).
    # R8 at (11.43, 49.53): pin1 world (11.43, 45.72) — 2.54 mm below.
    p = _build(tmp_path, _placed("R7", 11.43, 39.37, 7) + _placed("R8", 11.43, 49.53, 8))
    loc = PinLocator()
    assert loc.get_pin_location(p, "R7", "2") == pytest.approx([11.43, 43.18])
    assert loc.get_pin_location(p, "R8", "1") == pytest.approx([11.43, 45.72])

    r1 = ConnectionManager.connect_to_net(p, "R7", "2", "GND")
    _clear()
    r2 = ConnectionManager.connect_to_net(p, "R8", "1", "CC2")

    assert r1["success"] is True
    assert r2["success"] is True
    # Neither label landed on the neighbour's pin.
    assert r1["label_location"] != [11.43, 45.72]
    assert r2["label_location"] != [11.43, 43.18]

    # The decisive assertion: the two nets are disjoint, each carrying only its
    # own resistor pin (pre-fix each net contained BOTH pins).
    gnd = _net(p, "GND")
    cc2 = _net(p, "CC2")
    assert gnd == {("R7", "2")}, gnd
    assert cc2 == {("R8", "1")}, cc2
    assert gnd.isdisjoint(cc2)


@pytest.mark.unit
def test_s3_facing_pins_1p27mm_variant_do_not_short(tmp_path: Path) -> None:
    """1.27 mm-spacing variant (the C12/C13 shape): pins one grid step apart."""
    # R_A at (20, 39.37): pin2 world (20, 43.18).
    # R_B at (20, 48.26): pin1 world (20, 44.45) — 1.27 mm below.
    p = _build(tmp_path, _placed("RA", 20, 39.37, 3) + _placed("RB", 20, 48.26, 4))
    loc = PinLocator()
    assert loc.get_pin_location(p, "RA", "2") == pytest.approx([20.0, 43.18])
    assert loc.get_pin_location(p, "RB", "1") == pytest.approx([20.0, 44.45])

    assert ConnectionManager.connect_to_net(p, "RA", "2", "OSC_OUT")["success"] is True
    _clear()
    assert ConnectionManager.connect_to_net(p, "RB", "1", "GND")["success"] is True

    osc = _net(p, "OSC_OUT")
    gnd = _net(p, "GND")
    assert osc == {("RA", "2")}, osc
    assert gnd == {("RB", "1")}, gnd
    assert osc.isdisjoint(gnd)


@pytest.mark.unit
def test_s3_first_stub_relocates_off_neighbour_pin(tmp_path: Path) -> None:
    """The very first connect (before any label exists) must already dodge the
    neighbour's bare pin — proving the fix keys on pin POSITIONS, not labels."""
    p = _build(tmp_path, _placed("R7", 11.43, 39.37, 7) + _placed("R8", 11.43, 49.53, 8))
    res = ConnectionManager.connect_to_net(p, "R7", "2", "GND")
    assert res["success"] is True
    assert res["relocated"] is True
    # The GND label is NOT on R8/pin1 and no wire runs onto that pin.
    assert res["label_location"] != [11.43, 45.72]
    for a, b in _wires(p):
        assert (11.43, 45.72) not in (a, b)


# ===========================================================================
# S3 — pin-obstacle helper unit tests (pure geometry)
# ===========================================================================


@pytest.mark.unit
class TestStubBlocker:
    def test_foreign_pin_at_stub_end_blocks(self) -> None:
        blk = ConnectionManager._stub_blocker(
            [11.43, 43.18], [11.43, 45.72], "GND", [], [(11.43, 45.72)]
        )
        assert blk is not None
        assert blk["kind"] == "pin"
        assert blk["point"] == [11.43, 45.72]

    def test_foreign_pin_on_wire_segment_blocks(self) -> None:
        # Stub 43.18 -> 48.26 passes straight through a pin at 45.72.
        blk = ConnectionManager._stub_blocker(
            [11.43, 43.18], [11.43, 48.26], "GND", [], [(11.43, 45.72)]
        )
        assert blk is not None and blk["kind"] == "pin"

    def test_source_pin_never_blocks_itself(self) -> None:
        blk = ConnectionManager._stub_blocker(
            [11.43, 43.18], [13.97, 43.18], "GND", [], [(11.43, 43.18)]
        )
        assert blk is None

    def test_clear_stub_has_no_blocker(self) -> None:
        blk = ConnectionManager._stub_blocker(
            [11.43, 43.18], [13.97, 43.18], "GND", [], [(11.43, 45.72)]
        )
        assert blk is None

    def test_label_collision_reported_in_preference_to_pin(self) -> None:
        blk = ConnectionManager._stub_blocker(
            [11.43, 43.18],
            [11.43, 45.72],
            "GND",
            [(11.43, 45.72, "+3V3")],
            [(11.43, 45.72)],
        )
        assert blk is not None and blk["kind"] == "label" and blk["net"] == "+3V3"


@pytest.mark.unit
class TestChooseStubWithPins:
    def test_all_directions_blocked_by_pins_refuses(self) -> None:
        pin = [100.0, 100.0]
        cands = ConnectionManager._stub_candidates(pin, 90.0)
        obstacles = [(e[0][0], e[0][1]) for e in cands]  # a pin on every candidate end
        chosen, blocker = ConnectionManager._choose_stub(pin, 90.0, "N", [], obstacles)
        assert chosen is None
        assert blocker is not None and blocker["kind"] == "pin"

    def test_default_used_when_no_obstacle_or_net(self) -> None:
        pin = [100.0, 100.0]
        chosen, blocker = ConnectionManager._choose_stub(pin, 90.0, "N", [], [])
        assert blocker is None
        assert chosen == ConnectionManager._stub_candidates(pin, 90.0)[0]


# ===========================================================================
# S3 — connect_to_net refuses (success:False) when every direction is blocked
# ===========================================================================


@pytest.mark.unit
def test_s3_connect_refuses_with_label_collision_when_boxed_in(tmp_path: Path) -> None:
    """When every candidate stub point is occupied by a foreign pin, connect_to_net
    refuses (success:False) with the documented label_collision payload instead of
    shorting. A pin collision additionally carries ``colliding_pin``."""
    p = _build(tmp_path, _placed("R1", 100, 100, 1))
    loc = PinLocator()
    pin_loc = loc.get_pin_location(p, "R1", "1")
    ang = loc.get_pin_angle(p, "R1", "1")
    cands = ConnectionManager._stub_candidates(pin_loc, float(ang))
    all_ends = [(e[0][0], e[0][1]) for e in cands]

    with (
        patch.object(ConnectionManager, "_component_pin_obstacles", return_value=all_ends),
        patch.object(ConnectionManager, "_existing_net_points", return_value=[]),
    ):
        res = ConnectionManager.connect_to_net(p, "R1", "1", "MYNET")

    assert res["success"] is False
    assert "label_collision" in res
    assert res["label_collision"]["point"] == cands[0][0]
    assert res["label_collision"].get("colliding_pin") is not None
    # MYNET was never written.
    assert all(n != "MYNET" for n, _ in _labels(p))


# ===========================================================================
# S5 — no duplicate overlapping wires
# ===========================================================================


@pytest.mark.unit
def test_s5_repeat_connect_same_pin_net_makes_one_wire(tmp_path: Path) -> None:
    """Connecting the same pin to the same net twice must not leave two coincident
    wire objects (which delete_schematic_wire could only half-remove)."""
    p = _build(tmp_path, _placed("R1", 100, 100, 1))
    assert ConnectionManager.connect_to_net(p, "R1", "1", "SIG")["success"] is True
    _clear()
    assert ConnectionManager.connect_to_net(p, "R1", "1", "SIG")["success"] is True

    ws = _wires(p)
    assert len(ws) == 1, f"expected a single stub wire, got {ws}"


@pytest.mark.unit
def test_s5_each_of_two_relocated_stubs_is_a_single_wire(tmp_path: Path) -> None:
    """The S3 repro leaves exactly one (non-duplicated) wire per connection."""
    p = _build(tmp_path, _placed("R7", 11.43, 39.37, 7) + _placed("R8", 11.43, 49.53, 8))
    ConnectionManager.connect_to_net(p, "R7", "2", "GND")
    _clear()
    ConnectionManager.connect_to_net(p, "R8", "1", "CC2")

    ws = _wires(p)
    assert len(ws) == 2  # one stub each, no coincident duplicate
    # No two wires share the same endpoint pair (in either direction).
    seen = set()
    for a, b in ws:
        key = frozenset((a, b))
        assert key not in seen, f"duplicate overlapping wire {a}->{b}"
        seen.add(key)


@pytest.mark.unit
def test_s5_delete_wires_sweeps_coincident_duplicates(tmp_path: Path) -> None:
    """WireManager.delete_wires removes BOTH coincident wires in one call and
    reports the count; delete_wire keeps its boolean contract."""
    from commands.wire_manager import WireManager

    body = (
        '  (wire (pts (xy 11.43 43.18) (xy 11.43 45.72)) '
        '(stroke (width 0) (type default)) (uuid "aaaaaaaa-1111-1111-1111-111111111111"))\n'
        '  (wire (pts (xy 11.43 45.72) (xy 11.43 43.18)) '
        '(stroke (width 0) (type default)) (uuid "bbbbbbbb-2222-2222-2222-222222222222"))\n'
    )
    p = _build(tmp_path, body)
    removed = WireManager.delete_wires(p, [11.43, 43.18], [11.43, 45.72])
    assert removed == 2
    assert _wires(p) == []


# ===========================================================================
# S10 — missing component vs missing pin
# ===========================================================================


@pytest.mark.unit
def test_s10_missing_component_message(tmp_path: Path) -> None:
    p = _build(tmp_path, _placed("R1", 100, 100, 1))
    res = ConnectionManager.connect_to_net(p, "U99", "1", "NET")
    assert res["success"] is False
    assert res.get("component_not_found") is True
    assert "U99 not found" in res["message"]
    assert "pin" not in res["message"].lower().split("not found")[0]


@pytest.mark.unit
def test_s10_missing_pin_message_lists_valid_pins(tmp_path: Path) -> None:
    p = _build(tmp_path, _placed("R1", 100, 100, 1))
    res = ConnectionManager.connect_to_net(p, "R1", "999", "NET")
    assert res["success"] is False
    assert res.get("pin_not_found") is True
    assert "R1 exists but has no pin '999'" in res["message"]
    assert set(res.get("valid_pins", [])) == {"1", "2"}
    assert "1" in res["message"] and "2" in res["message"]


@pytest.mark.unit
def test_s10_messages_are_distinct(tmp_path: Path) -> None:
    p = _build(tmp_path, _placed("R1", 100, 100, 1))
    m_comp = ConnectionManager.connect_to_net(p, "U99", "1", "NET")["message"]
    m_pin = ConnectionManager.connect_to_net(p, "R1", "999", "NET")["message"]
    assert m_comp != m_pin


@pytest.mark.unit
class TestFormatMissingPinError:
    def test_no_symbol(self) -> None:
        msg = PinLocator.format_missing_pin_error("U99", "1", {"reason": "no_symbol"})
        assert "U99 not found" in msg

    def test_not_found_lists_pins(self) -> None:
        diag = {"reason": "not_found", "valid_pins": ["1", "2", "3"], "valid_pin_names": ["A", "~", "C"]}
        msg = PinLocator.format_missing_pin_error("U1", "9", diag)
        assert "U1 exists but has no pin '9'" in msg
        assert "1, 2, 3" in msg
        # "~" placeholder names are filtered out of the names hint.
        assert "A" in msg and "C" in msg


# ===========================================================================
# _wire_exists helper
# ===========================================================================


@pytest.mark.unit
def test_wire_exists_matches_either_direction(tmp_path: Path) -> None:
    body = (
        '  (wire (pts (xy 10 20) (xy 30 20)) '
        '(stroke (width 0) (type default)) (uuid "cccccccc-3333-3333-3333-333333333333"))\n'
    )
    p = _build(tmp_path, body)
    assert ConnectionManager._wire_exists(p, [10, 20], [30, 20]) is True
    assert ConnectionManager._wire_exists(p, [30, 20], [10, 20]) is True  # reversed
    assert ConnectionManager._wire_exists(p, [10, 20], [40, 20]) is False
