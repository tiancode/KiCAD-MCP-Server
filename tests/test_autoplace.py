"""Unit tests for the connectivity-driven auto-placement heuristic.

``commands.component._autoplace`` is a pure algorithm module (no pcbnew),
operating on plain dataclasses in mm, so these tests need no board stubs.
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.component._autoplace import (  # noqa: E402
    PlaceableComponent,
    auto_place,
    detect_decoupling,
)

ORIGIN = (0.0, 0.0)
SIZE = (50.0, 50.0)


def _c(ref, w=2.0, h=2.0, nets=(), **kwargs):
    return PlaceableComponent(reference=ref, width=w, height=h, nets=frozenset(nets), **kwargs)


def _run(components, **kwargs):
    kwargs.setdefault("board_origin", ORIGIN)
    kwargs.setdefault("board_size", SIZE)
    return auto_place(components, **kwargs)


def _positions(result):
    return {p["reference"]: (p["x"], p["y"]) for p in result["placements"]}


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _separated(comp_a, comp_b, pos_a, pos_b, spacing):
    """Courtyards are clear when the gap on at least one axis >= spacing."""
    gap_x = abs(pos_a[0] - pos_b[0]) - (comp_a.width + comp_b.width) / 2.0
    gap_y = abs(pos_a[1] - pos_b[1]) - (comp_a.height + comp_b.height) / 2.0
    return gap_x >= spacing - 1e-6 or gap_y >= spacing - 1e-6


def _on_grid(value, origin, grid):
    steps = (value - origin) / grid
    return abs(steps - round(steps)) < 1e-6


@pytest.mark.unit
def test_strongly_connected_pair_ends_up_closer_than_weak():
    # U1 is a hub; R1 shares 5 nets, R2..R4 share 2 each, R5 shares 1.
    # The big partners saturate the ring around U1, so the weakest link
    # (placed last) must land strictly farther out than the strongest.
    u1 = _c(
        "U1", 2.0, 2.0, {"S1", "S2", "S3", "S4", "S5", "M1", "M2", "M3", "M4", "M5", "M6", "W1"}
    )
    r1 = _c("R1", 6.0, 6.0, {"S1", "S2", "S3", "S4", "S5"})
    r2 = _c("R2", 6.0, 6.0, {"M1", "M2"})
    r3 = _c("R3", 6.0, 6.0, {"M3", "M4"})
    r4 = _c("R4", 6.0, 6.0, {"M5", "M6"})
    r5 = _c("R5", 6.0, 6.0, {"W1"})

    result = _run([u1, r1, r2, r3, r4, r5])

    assert result["success"] is True
    assert result["unplaced"] == []
    pos = _positions(result)
    assert _dist(pos["U1"], pos["R1"]) < _dist(pos["U1"], pos["R5"])


@pytest.mark.unit
def test_courtyards_never_overlap():
    comps = []
    for i in range(10):
        comps.append(
            _c(
                f"R{i + 1}",
                2.0 + (i % 4),
                2.0 + ((i * 2) % 5),
                {f"N{i}", f"N{i + 1}", "GND"},
            )
        )
    spacing = 1.0
    result = auto_place(
        comps, board_origin=(10.0, 5.0), board_size=(60.0, 60.0), spacing_mm=spacing
    )

    assert result["unplaced"] == []
    pos = _positions(result)
    by_ref = {c.reference: c for c in comps}
    refs = sorted(pos)
    for i, ref_a in enumerate(refs):
        for ref_b in refs[i + 1 :]:
            assert _separated(
                by_ref[ref_a], by_ref[ref_b], pos[ref_a], pos[ref_b], spacing
            ), f"{ref_a} and {ref_b} closer than spacing"


@pytest.mark.unit
def test_placements_inside_area_and_on_grid():
    comps = [_c(f"R{i + 1}", 3.0, 2.0, {f"N{i}", f"N{i + 1}"}) for i in range(8)]
    origin = (10.0, 5.0)
    size = (60.0, 60.0)
    grid = 0.5
    result = auto_place(comps, board_origin=origin, board_size=size, grid_mm=grid)

    assert result["unplaced"] == []
    by_ref = {c.reference: c for c in comps}
    for ref, (x, y) in _positions(result).items():
        comp = by_ref[ref]
        assert x - comp.width / 2 >= origin[0] - 1e-6
        assert y - comp.height / 2 >= origin[1] - 1e-6
        assert x + comp.width / 2 <= origin[0] + size[0] + 1e-6
        assert y + comp.height / 2 <= origin[1] + size[1] + 1e-6
        assert _on_grid(x, origin[0], grid), f"{ref} x={x} off grid"
        assert _on_grid(y, origin[1], grid), f"{ref} y={y} off grid"


@pytest.mark.unit
def test_fixed_component_stays_put_and_attracts_partners():
    u1 = _c("U1", 4.0, 4.0, {"A", "B", "C"}, fixed=True, x=40.0, y=40.0)
    r1 = _c("R1", 2.0, 2.0, {"A", "B", "C"})
    z1 = _c("Z1", 2.0, 2.0, {"LONELY"})

    result = _run([u1, r1, z1])

    pos = _positions(result)
    assert pos["U1"] == (40.0, 40.0)
    # R1 hugs the fixed hub: within the immediate ring around it.
    limit = (4.0 + 2.0) / 2 + 1.0 + 2 * 0.5  # halfsizes + spacing + 2*grid
    assert _dist(pos["R1"], pos["U1"]) <= limit + 1e-6
    # ... and is pulled toward U1, not the board center.
    assert _dist(pos["R1"], pos["U1"]) < _dist(pos["R1"], (25.0, 25.0))
    assert "Z1" in pos


@pytest.mark.unit
def test_decoupling_cap_lands_in_immediate_ring_of_its_ic():
    u1 = _c("U1", 10.0, 10.0, {"VCC", "GND", "SIG1", "SIG2", "SIG3"})
    r1 = _c("R1", 3.0, 3.0, {"SIG1", "SIG2", "SIG3"})
    c1 = _c("C1", 1.0, 1.0, {"VCC", "GND"}, is_decoupling=True, decouples="U1")

    result = _run([u1, r1, c1])

    assert result["unplaced"] == []
    pos = _positions(result)
    limit = 10.0 / 2 + 1.0 / 2 + 1.0 + 2 * 0.5  # IC half + cap half + spacing + 2*grid
    assert _dist(pos["C1"], pos["U1"]) <= limit + 1e-6


@pytest.mark.unit
def test_zero_affinity_component_still_placed():
    comps = [
        _c("U1", nets={"A", "B"}),
        _c("R1", nets={"A", "B"}),
        _c("X1", nets=()),
    ]
    result = _run(comps)

    assert {p["reference"] for p in result["placements"]} == {"U1", "R1", "X1"}
    assert result["unplaced"] == []
    assert result["stats"]["placed"] == 3
    assert result["stats"]["unplaced"] == 0
    # Not every component had an initial position -> no "before" estimate.
    assert result["stats"]["estWirelengthBefore"] is None


@pytest.mark.unit
def test_overfull_area_yields_unplaced_with_reason():
    comps = [_c(f"U{i}", 6.0, 6.0, {"N"}) for i in (1, 2, 3)]
    result = auto_place(comps, board_origin=(0.0, 0.0), board_size=(10.0, 10.0))

    assert result["success"] is True  # never fails hard
    assert len(result["placements"]) == 1
    assert len(result["unplaced"]) == 2
    for entry in result["unplaced"]:
        assert entry["reference"].startswith("U")
        assert entry["reason"]
    assert result["stats"]["placed"] == 1
    assert result["stats"]["unplaced"] == 2


@pytest.mark.unit
def test_hpwl_improves_over_scrambled_layout():
    # Two independent clusters whose members start at opposite corners.
    comps = [
        _c("U1", 4.0, 4.0, {"A1", "A2", "A3"}, x=2.0, y=2.0),
        _c("R1", 2.0, 2.0, {"A1", "A2"}, x=58.0, y=58.0),
        _c("R2", 2.0, 2.0, {"A3"}, x=2.0, y=58.0),
        _c("U2", 4.0, 4.0, {"B1", "B2", "B3"}, x=58.0, y=2.0),
        _c("R3", 2.0, 2.0, {"B1", "B2"}, x=30.0, y=58.0),
        _c("R4", 2.0, 2.0, {"B3"}, x=58.0, y=30.0),
    ]
    result = auto_place(comps, board_origin=(0.0, 0.0), board_size=(60.0, 60.0))

    assert result["unplaced"] == []
    stats = result["stats"]
    assert stats["estWirelengthBefore"] is not None
    assert stats["estWirelengthAfter"] < stats["estWirelengthBefore"]


@pytest.mark.unit
def test_detect_decoupling_maps_caps_to_local_ic():
    comps = [
        {"reference": "U1", "value": "STM32", "nets": {"VCC", "GND", "SIG1", "SIG2"}},
        {"reference": "C1", "value": "100n", "nets": {"VCC", "GND"}},
        {"reference": "C2", "value": "104", "nets": {"VCC", "GND"}},  # EIA code -> 100 nF
        {"reference": "C5", "value": "1µF", "nets": {"NETX", "GND"}},  # no IC on NETX
        {"reference": "C9", "value": "100uF", "nets": {"VCC", "GND"}},  # bulk cap
        {"reference": "R1", "value": "10k", "nets": {"SIG1", "GND"}},
    ]
    result = detect_decoupling(comps)

    assert result["C1"] == "U1"
    assert result["C2"] == "U1"
    assert result["C5"] is None
    assert "C9" not in result  # 100 uF is bulk, not decoupling
    assert "R1" not in result  # not a cap reference
    assert "U1" not in result


@pytest.mark.unit
def test_detect_decoupling_prefers_least_shared_rail():
    # C1 sits on both VCC (everything) and VDD_MCU (local rail of U2):
    # the less-shared non-ground net wins, so C1 belongs to U2.
    comps = [
        {"reference": "U1", "value": "REG", "nets": {"VCC", "GND"}},
        {"reference": "U2", "value": "MCU", "nets": {"VCC", "VDD_MCU", "GND"}},
        {"reference": "U3", "value": "ADC", "nets": {"VCC", "GND"}},
        {"reference": "C1", "value": "0.1u", "nets": {"VCC", "VDD_MCU", "GND"}},
    ]
    result = detect_decoupling(comps)

    assert result["C1"] == "U2"
