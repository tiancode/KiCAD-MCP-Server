"""
Regression: connect_to_net must wire a FLOATING power-symbol pin to REAL
connectivity, not a stub into empty space.

The earlier F3 fix drew a fixed-length outward stub from the floating power pin.
Its far end lands on nothing, so KiCad sees a dangling wire — the pin is never
electrically joined to the net's real elements. A live kicad-cli 10.0.x ERC run
shows the stub leaves an ``unconnected_wire_endpoint`` on the pin (and the pin
is not on the net). Drawing a wire from the same pin to a REAL pin on the net
clears it.

The fix routes an L-shaped (H-then-V) wire from the power pin to the nearest
real component pin already on the net (or an existing net wire/label point),
avoiding other pins (which it would otherwise silently short) and different-net
labels.

Two layers of coverage:
  1. Pure-geometry unit tests of the router (no files).
  2. connect_to_net on a real .kicad_sch (WireManager.add_polyline_wire mocked to
     capture the drawn path) — asserts BOTH endpoints land on the two pins with
     an L-shaped, junction-sharing route.
  3. A real kicad-cli ERC integration test proving no pin_not_connected /
     unconnected_wire_endpoint survives at the power pin (nearby AND far cases).
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PYTHON_DIR = os.path.join(os.path.dirname(__file__), "..", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

sys.modules.setdefault("pcbnew", MagicMock())

from commands.connection_schematic import ConnectionManager  # noqa: E402
from commands.pin_locator import PinLocator  # noqa: E402

# ---------------------------------------------------------------------------
# Minimal library symbols
# ---------------------------------------------------------------------------
_R_LIB = (
    '(symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))\n'
    '  (symbol "R_1_1"\n'
    '    (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))\n'
    '    (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))))'
)
# A GND-style power symbol: one power_in pin at the origin (world pin == placement).
_GND_LIB = (
    '(symbol "power:GND" (power) (pin_names (offset 0) hide) (in_bom no) (on_board yes)\n'
    '  (symbol "GND_0_1"\n'
    "    (polyline (pts (xy 0 0) (xy 0 -1.27)) (stroke (width 0)) (fill (type none))))\n"
    '  (symbol "GND_1_1"\n'
    '    (pin power_in line (at 0 0 270) (length 0) (name "GND") (number "1"))))'
)


def _clear() -> None:
    PinLocator._SCHEMATIC_CACHE.clear()
    PinLocator._SEXP_CACHE.clear()
    PinLocator._PINDEF_CACHE.clear()


def _placed(lib_id: str, ref: str, value: str, x: float, y: float, u: int) -> str:
    return (
        f'  (symbol (lib_id "{lib_id}") (at {x} {y} 0) (unit 1)\n'
        "    (in_bom yes) (on_board yes) (dnp no)\n"
        f'    (uuid "1111111{u}-1111-1111-1111-1111111111aa")\n'
        f'    (property "Reference" "{ref}" (at {x} {y} 0))\n'
        f'    (property "Value" "{value}" (at {x} {y} 0))\n'
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


def _build(tmp_path: Path, body: str) -> Path:
    text = (
        '(kicad_sch (version 20250114) (generator "test")\n'
        '  (uuid "00000000-0000-0000-0000-0000000000aa")\n'
        '  (paper "A4")\n'
        f"  (lib_symbols\n    {_R_LIB}\n    {_GND_LIB}\n  )\n"
        + body
        + '  (sheet_instances (path "/" (page "1")))\n'
        ")\n"
    )
    p = tmp_path / "board.kicad_sch"
    p.write_text(text)
    _clear()
    return p


# ===========================================================================
# 1. Pure-geometry router unit tests
# ===========================================================================


@pytest.mark.unit
class TestPowerWireRouter:
    def test_diagonal_target_gives_l_route_endpoints_on_pins(self) -> None:
        pin = [112.7, 90.17]
        target = [100.0, 96.19]
        path, blocker = ConnectionManager._route_power_wire(pin, target, [], "GND", [])
        assert blocker is None
        # Endpoints land exactly on the two pins.
        assert path[0] == pin
        assert path[-1] == target
        # L-shaped: a single corner, sharing a coordinate with each endpoint.
        assert len(path) == 3
        corner = path[1]
        # H-first: corner shares y with start and x with end.
        assert corner[1] == pytest.approx(pin[1]) and corner[0] == pytest.approx(target[0])
        # Consecutive segments share their junction vertex (a connected polyline).
        for i in range(len(path) - 1):
            assert path[i] != path[i + 1]

    def test_far_target_still_lands_endpoint_on_pin_both_ends(self) -> None:
        pin = [120.0, 76.2]
        target = [100.0, 96.19]  # ~28 mm away
        assert math.dist(pin, target) > 20.0
        path, blocker = ConnectionManager._route_power_wire(pin, target, [], "GND", [])
        assert blocker is None
        assert path[0] == pin
        assert path[-1] == target
        # Only horizontal/vertical segments (no diagonals).
        for a, b in zip(path, path[1:]):
            assert a[0] == pytest.approx(b[0]) or a[1] == pytest.approx(b[1])

    def test_collinear_obstacle_forces_dogleg_around_pin(self) -> None:
        # Power pin and target share x; another pin sits BETWEEN them on that line.
        pin = [100.0, 120.0]
        target = [100.0, 96.19]
        obstacle = (100.0, 103.81)  # strictly between 96.19 and 120 on x=100
        path, blocker = ConnectionManager._route_power_wire(pin, target, [obstacle], "GND", [])
        assert blocker is None, "router should dogleg around the collinear pin"
        assert path[0] == pin and path[-1] == target
        # The obstacle must NOT lie on any drawn segment.
        for a, b in zip(path, path[1:]):
            assert not ConnectionManager._point_on_segment_mm(obstacle, a, b)

    def test_different_net_label_on_every_route_refuses(self) -> None:
        # A different-net label sits on the target pin's own approach so every
        # candidate crosses it → refuse (never silently short the nets).
        pin = [100.0, 120.0]
        target = [100.0, 96.19]
        # Block both x=100 column and the horizontal approaches by planting +3V3
        # labels at each corner the L/dogleg routes would use.
        net_points = [
            (100.0, 103.81, "+3V3"),
            (102.54, 120.0, "+3V3"),
            (102.54, 96.19, "+3V3"),
            (97.46, 120.0, "+3V3"),
            (97.46, 96.19, "+3V3"),
            (105.08, 120.0, "+3V3"),
            (105.08, 96.19, "+3V3"),
            (94.92, 120.0, "+3V3"),
            (94.92, 96.19, "+3V3"),
        ]
        path, blocker = ConnectionManager._route_power_wire(pin, target, [], "GND", net_points)
        assert path is None
        assert blocker is not None
        assert blocker["kind"] == "label"
        assert blocker["net"] == "+3V3"

    def test_dedupe_collapses_zero_length_segments(self) -> None:
        # Aligned endpoints collapse the L to a single straight segment.
        pin = [100.0, 120.0]
        target = [100.0, 96.19]
        path, blocker = ConnectionManager._route_power_wire(pin, target, [], "GND", [])
        assert blocker is None
        assert path == [[100.0, 120.0], [100.0, 96.19]]


# ===========================================================================
# 2. connect_to_net on a real schematic (WireManager mocked to capture path)
# ===========================================================================


def _connect_floating_and_capture(sch: Path, power_ref: str):
    captured: dict = {}

    def fake_polyline(path, points, *a, **kw):
        captured["points"] = [list(p) for p in points]
        return True

    def fail_add_wire(*a, **kw):  # the outward-stub fallback must NOT be used
        captured["stub_add_wire"] = True
        return True

    _clear()
    with (
        patch("commands.wire_manager.WireManager.add_polyline_wire", side_effect=fake_polyline),
        patch("commands.wire_manager.WireManager.add_wire", side_effect=fail_add_wire),
        patch("commands.wire_manager.WireManager.add_label", return_value=True),
    ):
        res = ConnectionManager.connect_to_net(sch, power_ref, "1", "GND")
    return res, captured


@pytest.mark.unit
@pytest.mark.parametrize(
    "pwr_x,pwr_y,tag",
    [
        (112.7, 90.17, "near"),  # ~14 mm from R1 pin1
        (120.0, 76.2, "far"),  # ~28 mm from R1 pin1
    ],
)
def test_connect_to_net_floating_power_draws_wire_to_real_pin(
    tmp_path: Path, pwr_x: float, pwr_y: float, tag: str
) -> None:
    # R1 pin1 (100, 96.19) carries net GND via a label placed on it → the anchor.
    body = (
        _placed("Device:R", "R1", "10k", 100, 100, 1)
        + _placed("power:GND", "#PWR01", "GND", pwr_x, pwr_y, 2)
        + _label("GND", 100.0, 96.19)
    )
    sch = _build(tmp_path, body)

    loc = PinLocator()
    pwr_pin = loc.get_pin_location(sch, "#PWR01", "1")
    r1_pin1 = loc.get_pin_location(sch, "R1", "1")
    assert pwr_pin == [pwr_x, pwr_y]
    assert r1_pin1 == pytest.approx([100.0, 96.19])

    res, captured = _connect_floating_and_capture(sch, "#PWR01")

    assert res["success"] is True
    assert res["drew_wire"] is True
    assert res["drew_stub_wire"] is True  # backward-compat key preserved
    assert res["label_location"] is None
    assert res["connected_to"]["ref"] == "R1"
    assert res["connected_to"]["kind"] == "pin"
    # No outward stub fallback fired.
    assert "stub_add_wire" not in captured

    pts = captured["points"]
    # One end on the power pin, the other on the source (R1 pin1).
    assert pts[0] == pytest.approx(pwr_pin)
    assert pts[-1] == pytest.approx(r1_pin1)
    # L-shaped, sharing junction points, orthogonal only (no diagonals).
    assert len(pts) >= 3
    for a, b in zip(pts, pts[1:]):
        assert a != b
        assert a[0] == pytest.approx(b[0]) or a[1] == pytest.approx(b[1])


@pytest.mark.unit
def test_connect_to_net_floating_power_no_anchor_falls_back_to_stub(tmp_path: Path) -> None:
    # No other member on net GND → the router has nothing to reach, so the old
    # outward stub behaviour (single add_wire, no label) is preserved.
    body = _placed("power:GND", "#PWR01", "GND", 120.0, 100.0, 2)
    sch = _build(tmp_path, body)

    _clear()
    with (
        patch("commands.wire_manager.WireManager.add_wire", return_value=True) as add_wire,
        patch("commands.wire_manager.WireManager.add_polyline_wire", return_value=True) as add_poly,
        patch("commands.wire_manager.WireManager.add_label", return_value=True) as add_label,
    ):
        res = ConnectionManager.connect_to_net(sch, "#PWR01", "1", "GND")

    assert res["success"] is True
    assert res["drew_stub_wire"] is True
    assert res["label_location"] is None
    add_wire.assert_called_once()
    add_poly.assert_not_called()
    add_label.assert_not_called()


# ===========================================================================
# 3. Real kicad-cli ERC integration test
# ===========================================================================

_KICAD_CLI = shutil.which("kicad-cli")
_STOCK_SYMBOLS = Path("/usr/share/kicad/symbols")


def _run_erc(sch: Path):
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as t:
        out = t.name
    try:
        subprocess.run(
            [_KICAD_CLI, "sch", "erc", "--format", "json", "--output", out, str(sch)],
            capture_output=True,
            text=True,
            timeout=120,
            env=dict(os.environ, LC_ALL="C"),
        )
        with open(out) as f:
            data = json.load(f)
    finally:
        if os.path.exists(out):
            os.unlink(out)
    viols = list(data.get("violations", []))
    for sheet in data.get("sheets", []):
        viols.extend(sheet.get("violations", []))
    return viols


def _violation_at(v: dict, x100: float, y100: float, tol: float = 1.0) -> bool:
    items = v.get("items", [])
    if not items or "pos" not in items[0]:
        return False
    p = items[0]["pos"]
    return abs(p.get("x", 0) * 100 - x100) < tol and abs(p.get("y", 0) * 100 - y100) < tol


@pytest.mark.integration
@pytest.mark.skipif(_KICAD_CLI is None, reason="kicad-cli not installed")
@pytest.mark.skipif(
    not (_STOCK_SYMBOLS / "power.kicad_sym").exists(),
    reason="stock KiCad symbol libraries not installed",
)
@pytest.mark.parametrize(
    "pwr_x,pwr_y,tag",
    [
        (137.16, 92.71, "near"),  # ~9 mm from R20 pin1, clean L
        (152.4, 90.17, "far"),  # ~26 mm from R20 pin1, clean L
    ],
)
def test_erc_floating_power_pin_is_really_connected(
    tmp_path: Path, pwr_x: float, pwr_y: float, tag: str
) -> None:
    """A floating power:GND wired by connect_to_net leaves NO pin_not_connected
    and NO unconnected_wire_endpoint at the power pin — proving the drawn wire
    actually joins the pin to the net (the stub-into-empty-space did not)."""
    from commands.dynamic_symbol_loader import DynamicSymbolLoader

    empty = Path(_PYTHON_DIR) / "templates" / "empty.kicad_sch"
    sch = tmp_path / "demo.kicad_sch"
    shutil.copy(empty, sch)

    loader = DynamicSymbolLoader()
    placed_r = loader.add_component(
        sch, "Device", "R", reference="R20", value="10k", x=127.0, y=101.6
    )
    placed_p = loader.add_component(
        sch, "power", "GND", reference="#PWR01", value="GND", x=pwr_x, y=pwr_y
    )
    _clear()
    loc = PinLocator()
    if not (placed_r and placed_p) or not loc.get_pin_location(sch, "#PWR01", "1"):
        pytest.skip("stock Device/power libraries did not resolve on this host")

    # R20 pin1 → GND (normal stub+label path): gives net GND a real member pin.
    r_res = ConnectionManager.connect_to_net(sch, "R20", "1", "GND")
    assert r_res["success"] is True
    _clear()

    pwr_pin = loc.get_pin_location(sch, "#PWR01", "1")
    r20_pin1 = loc.get_pin_location(sch, "R20", "1")

    # The unfixed stub-into-empty-space leaves an unconnected_wire_endpoint; the
    # fix routes to R20 pin1.
    res = ConnectionManager.connect_to_net(sch, "#PWR01", "1", "GND")
    assert res["success"] is True
    assert res.get("drew_wire") is True
    assert res["wire_path"][0] == pytest.approx(pwr_pin)
    assert res["wire_path"][-1] == pytest.approx(r20_pin1)
    _clear()

    viols = _run_erc(sch)
    x100, y100 = round(pwr_pin[0] * 100, 2), round(pwr_pin[1] * 100, 2)

    pnc_at_power = [
        v for v in viols if v.get("type") == "pin_not_connected" and _violation_at(v, x100, y100)
    ]
    uwe_any = [v for v in viols if v.get("type") == "unconnected_wire_endpoint"]

    assert not pnc_at_power, (
        f"[{tag}] pin_not_connected still reported at the power pin "
        f"({x100/100}, {y100/100}): {pnc_at_power}"
    )
    assert not uwe_any, (
        f"[{tag}] the drawn wire left a dangling endpoint (unconnected_wire_endpoint): "
        f"{[v.get('items') for v in uwe_any]}"
    )
