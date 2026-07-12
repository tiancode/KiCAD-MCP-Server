"""
Bug 2 regression: connect_to_net must not drop two different nets' labels on the
same point.

In the GD32 E2E run, connect_to_net(C1→GND) and connect_to_net(C2→+3V3) each
auto-chose a stub whose label landed at the IDENTICAL coordinate, silently
merging GND and +3V3 into one node.  The fix: before finalizing the auto-chosen
stub, check for an existing label / wire-endpoint / pin carrying a DIFFERENT net
at (or within one grid step of) that point or on the wire path.  On collision,
relocate to a free stub direction; if none is free, refuse with a structured
``label_collision`` error rather than silently shorting the nets.

Real .kicad_sch files + real WireManager (no mocking) so the written labels are
re-parsed and their coordinates verified.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
from pathlib import Path

import pytest

_PYTHON_DIR = os.path.join(os.path.dirname(__file__), "..", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

from commands.connection_schematic import ConnectionManager  # noqa: E402
from commands.pin_locator import PinLocator  # noqa: E402

_C_LIB = (
    '(symbol "Device:C" (pin_numbers hide) (pin_names (offset 0))\n'
    '  (symbol "C_1_1"\n'
    '    (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))\n'
    '    (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))))'
)


def _clear() -> None:
    PinLocator._SCHEMATIC_CACHE.clear()
    PinLocator._SEXP_CACHE.clear()
    PinLocator._PINDEF_CACHE.clear()


def _placed(ref: str, x: float, y: float, u: int) -> str:
    return (
        f'  (symbol (lib_id "Device:C") (at {x} {y} 0) (unit 1)\n'
        "    (in_bom yes) (on_board yes) (dnp no)\n"
        f'    (uuid "1111111{u}-1111-1111-1111-1111111111aa")\n'
        f'    (property "Reference" "{ref}" (at {x} {y} 0))\n'
        f'    (property "Value" "1u" (at {x} {y} 0))\n'
        "    (instances\n"
        '      (project "t"\n'
        f'        (path "/00000000-0000-0000-0000-0000000000aa" (reference "{ref}") (unit 1)))))\n'
    )


def _label(name: str, x: float, y: float) -> str:
    return (
        f'  (label "{name}" (at {x} {y} 0) '
        f"(effects (font (size 1.27 1.27)) (justify left bottom)) "
        f'(uuid "{abs(hash((name, x, y))) % 10**8:08d}-2222-2222-2222-2222222222aa"))\n'
    )


def _build(tmp_path: Path, symbols: str, extra: str = "") -> Path:
    text = (
        '(kicad_sch (version 20250114) (generator "test")\n'
        '  (uuid "00000000-0000-0000-0000-0000000000aa")\n'
        '  (paper "A4")\n'
        f"  (lib_symbols\n    {_C_LIB}\n  )\n"
        + symbols
        + extra
        + '  (sheet_instances (path "/" (page "1")))\n'
        ")\n"
    )
    p = tmp_path / "board.kicad_sch"
    p.write_text(text)
    _clear()
    return p


def _labels_in(p: Path) -> list:
    import sexpdata
    from sexpdata import Symbol

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


# ---------------------------------------------------------------------------
# Helper unit tests (pure geometry — no files)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStubCollisionHelper:
    def test_same_net_point_is_not_a_collision(self) -> None:
        pts = [(100.0, 93.65, "GND")]
        assert ConnectionManager._stub_collision([100, 96.19], [100, 93.65], "GND", pts) is None

    def test_different_net_at_stub_end_collides(self) -> None:
        pts = [(100.0, 93.65, "GND")]
        assert ConnectionManager._stub_collision([100, 96.19], [100, 93.65], "+3V3", pts) == "GND"

    def test_different_net_on_wire_segment_collides(self) -> None:
        # A label mid-path (not at the endpoint) still merges via the wire.
        pts = [(100.0, 95.0, "GND")]
        assert ConnectionManager._stub_collision([100, 96.19], [100, 90.0], "+3V3", pts) == "GND"

    def test_source_pin_own_net_ignored(self) -> None:
        # A net-point AT the source pin (e.g. a power port we're wiring) is ignored.
        pts = [(100.0, 96.19, "+5V")]
        assert ConnectionManager._stub_collision([100, 96.19], [100, 93.65], "+3V3", pts) is None

    def test_choose_stub_no_points_returns_default(self) -> None:
        cand, conflict = ConnectionManager._choose_stub([100, 96.19], 90.0, "N", [])
        assert conflict is None
        assert cand == ConnectionManager._stub_candidates([100, 96.19], 90.0)[0]


# ---------------------------------------------------------------------------
# Integration tests (real files)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_collision_relocates_to_a_free_direction(tmp_path: Path) -> None:
    # Pre-place a GND label exactly at C1's default stub end, then connect +3V3.
    p = _build(tmp_path, _placed("C1", 100, 100, 1))
    loc = PinLocator().get_pin_location(str(p), "C1", "1")
    ang = PinLocator().get_pin_angle(str(p), "C1", "1")
    default_end = ConnectionManager._stub_candidates(loc, float(ang))[0][0]

    p = _build(tmp_path, _placed("C1", 100, 100, 1), _label("GND", default_end[0], default_end[1]))
    res = ConnectionManager.connect_to_net(p, "C1", "1", "+3V3")

    assert res["success"] is True
    assert res["relocated"] is True
    # The +3V3 label did NOT land on the GND point.
    ll = res["label_location"]
    assert ll is not None
    assert ll != default_end
    assert math.dist(ll, default_end) > ConnectionManager._STUB_COLLISION_GRID
    # Both nets survive as distinct labels at distinct points.
    labels = _labels_in(p)
    gnd = [xy for n, xy in labels if n == "GND"]
    v3 = [xy for n, xy in labels if n == "+3V3"]
    assert gnd and v3
    assert gnd[0] != v3[0]


@pytest.mark.unit
def test_no_free_direction_refuses_with_structured_error(tmp_path: Path) -> None:
    # Block EVERY candidate stub end with a different-net label → must refuse.
    p = _build(tmp_path, _placed("C1", 100, 100, 1))
    loc = PinLocator().get_pin_location(str(p), "C1", "1")
    ang = PinLocator().get_pin_angle(str(p), "C1", "1")
    cands = ConnectionManager._stub_candidates(loc, float(ang))
    blockers = "".join(_label(f"OTHER{i}", e[0][0], e[0][1]) for i, e in enumerate(cands))

    p = _build(tmp_path, _placed("C1", 100, 100, 1), blockers)
    res = ConnectionManager.connect_to_net(p, "C1", "1", "MYNET")

    assert res["success"] is False
    assert "label_collision" in res
    assert res["label_collision"]["point"] == cands[0][0]
    assert str(res["label_collision"]["existing_net"]).startswith("OTHER")
    # MYNET was NEVER written (no silent placement).
    assert all(n != "MYNET" for n, _xy in _labels_in(p))


@pytest.mark.unit
def test_same_net_at_point_is_not_a_collision(tmp_path: Path) -> None:
    # A label of the SAME net at the auto-chosen point is a legitimate join, not
    # a collision — the connection uses the default stub (no relocation).
    p = _build(tmp_path, _placed("C1", 100, 100, 1))
    loc = PinLocator().get_pin_location(str(p), "C1", "1")
    ang = PinLocator().get_pin_angle(str(p), "C1", "1")
    default_end = ConnectionManager._stub_candidates(loc, float(ang))[0][0]

    p = _build(tmp_path, _placed("C1", 100, 100, 1), _label("GND", default_end[0], default_end[1]))
    res = ConnectionManager.connect_to_net(p, "C1", "1", "GND")

    assert res["success"] is True
    assert res.get("relocated") is None
    assert res["label_location"] == default_end


@pytest.mark.unit
def test_no_existing_nets_uses_default_stub(tmp_path: Path) -> None:
    # Regression: with nothing on the sheet the default stub is unchanged and the
    # final label position is reported in the success response.
    p = _build(tmp_path, _placed("C1", 100, 100, 1))
    loc = PinLocator().get_pin_location(str(p), "C1", "1")
    ang = PinLocator().get_pin_angle(str(p), "C1", "1")
    default_end = ConnectionManager._stub_candidates(loc, float(ang))[0][0]

    _clear()
    res = ConnectionManager.connect_to_net(p, "C1", "1", "SIG")
    assert res["success"] is True
    assert res.get("relocated") is None
    assert res["label_location"] == default_end  # final label position reported


@pytest.mark.unit
def test_two_caps_opposite_nets_do_not_coincide(tmp_path: Path) -> None:
    """E2E shape: connect C1→GND then C2→+3V3, with C2 positioned so its default
    stub would land exactly on C1's GND label. The two labels must not coincide."""
    # C1 at (100, 100): pin1 default stub end.
    p = _build(tmp_path, _placed("C1", 100, 100, 1))
    res1 = ConnectionManager.connect_to_net(p, "C1", "1", "GND")
    assert res1["success"] is True
    gnd_stub = res1["label_location"]

    # Place C2 so its pin2 default stub end == C1's GND stub end.
    # pin2 world = (Cx, Cy + 3.81); outward is +y; stub end = (Cx, Cy + 6.35).
    c2x, c2y = gnd_stub[0], gnd_stub[1] - 6.35
    p2 = _build(
        tmp_path,
        _placed("C1", 100, 100, 1) + _placed("C2", c2x, c2y, 2),
        _label("GND", gnd_stub[0], gnd_stub[1]),
    )
    # Sanity: confirm the collision is real before relying on the fix to dodge it.
    loc2 = PinLocator().get_pin_location(str(p2), "C2", "2")
    ang2 = PinLocator().get_pin_angle(str(p2), "C2", "2")
    assert ConnectionManager._stub_candidates(loc2, float(ang2))[0][0] == pytest.approx(
        gnd_stub, abs=1e-3
    )

    res2 = ConnectionManager.connect_to_net(p2, "C2", "2", "+3V3")
    assert res2["success"] is True
    assert res2["relocated"] is True
    assert res2["label_location"] != gnd_stub
    assert math.dist(res2["label_location"], gnd_stub) > ConnectionManager._STUB_COLLISION_GRID
