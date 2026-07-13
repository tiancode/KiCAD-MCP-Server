"""Regression tests for the mid-span "T" short in the net-connectivity queries (E2E round-6 S4).

Background
----------
``get_net_connections(netName="OSC_OUT")`` on the round-6 FM-radio project
returned the entire ~54-pin GND supernet instead of OSC_OUT's real three pins,
even though ``generate_netlist`` (authoritative — kicad-cli) reported
``/OSC_OUT = {C11/1, U1/13, Y1/2}``. Similar small nets (OSC_IN, NRST, ...) were
reported correctly, so it looked like fuzzy name matching between "OSC_OUT" and
"OSC32_OUT" — but the real mechanism is geometric:

``_build_adjacency`` treated *any* wire endpoint that lands on the interior of
another wire (a "T") as an electrical connection. KiCad does **not** do that: a
bare mid-span touch is only a connection when a junction dot is placed there.
In the radio schematic a GND wire ended mid-span on OSC_OUT's vertical wire with
no junction, so the wire-graph BFS bridged OSC_OUT straight into GND (and, via
the GND label jump, into the whole supernet). kicad-cli's netlister kept them
separate; our query did not.

The fix makes ``_build_adjacency`` junction-aware: a mid-span T is bridged only
when a junction dot sits on it. Wires that share an exact endpoint still connect
unconditionally (unchanged), and callers that cannot read junction data (mock
schematics, direct unit tests) keep the old permissive behaviour by passing
``junctions=None``.

These tests synthesise a minimal .kicad_sch that reproduces the exact geometry:
a small ``OSC_OUT`` net whose wire a ``GND`` wire T's into mid-span with **no**
junction, a similarly-named ``OSC32_OUT`` net kept separate, the ``GND``
supernet, and a positive-control ``TJUNC`` net whose mid-span T *does* carry a
junction (so it must stay connected — proving the fix does not over-restrict).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Set, Tuple
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.schematic import SchematicManager  # noqa: E402
from commands.wire_connectivity import (  # noqa: E402
    _build_adjacency,
    _parse_junctions_sexp,
    _to_iu,
    get_connections_for_net,
)

# ---------------------------------------------------------------------------
# Minimal fixture builder
# ---------------------------------------------------------------------------

# Self-contained Device:R definition: pin "1" at (0, +3.81), pin "2" at
# (0, -3.81) in symbol space. Placed at (X, Y, 0) with no mirror/rotation, the
# schematic y-negation puts pin 1 at (X, Y-3.81) and pin 2 at (X, Y+3.81).
_LIB_SYMBOLS = """  (lib_symbols
    (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0)) (in_bom yes) (on_board yes)
      (property "Reference" "R" (at 2.032 0 90) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 0 90) (effects (font (size 1.27 1.27))))
      (symbol "R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27))))))))
"""


def _resistor(ref: str, x: float, y: float) -> str:
    return (
        f'  (symbol (lib_id "Device:R") (at {x} {y} 0) (unit 1) (in_bom yes) (on_board yes)'
        f' (dnp no) (uuid "00000000-0000-0000-0000-0000000{ref}")\n'
        f'    (property "Reference" "{ref}" (at {x} {y} 0) (effects (font (size 1.27 1.27))))\n'
        f'    (property "Value" "R" (at {x} {y} 0) (effects (font (size 1.27 1.27))))\n'
        f'    (property "Footprint" "" (at {x} {y} 0) (effects (font (size 1.27 1.27)) hide))\n'
        f'    (instances (project "t" (path "/" (reference "{ref}") (unit 1)))))\n'
    )


def _wire(x1: float, y1: float, x2: float, y2: float) -> str:
    return (
        f"  (wire (pts (xy {x1} {y1}) (xy {x2} {y2})) (stroke (width 0) (type default))"
        f' (uuid "00000000-0000-0000-0001-{x1:03.0f}{y1:03.0f}{x2:03.0f}{y2:03.0f}"))\n'
    )


def _label(name: str, x: float, y: float) -> str:
    return (
        f'  (label "{name}" (at {x} {y} 0) (effects (font (size 1.27 1.27)))'
        f' (uuid "00000000-0000-0000-0002-{x:04.0f}{y:04.0f}00"))\n'
    )


def _junction(x: float, y: float) -> str:
    return (
        f"  (junction (at {x} {y}) (diameter 0) (color 0 0 0 0)"
        f' (uuid "00000000-0000-0000-0003-{x:04.0f}{y:04.0f}00"))\n'
    )


def _write_fixture(tmp_path: Path) -> Path:
    """Write the minimal schematic and return its path.

    Geometry (mm):

      OSC_OUT (small net, must stay {R1/1, R7/1}):
        vertical wire (100,100)-(100,110), horizontal wire (100,100)-(90,100),
        label "OSC_OUT" at (100,100); R1 pin1 -> (100,110), R7 pin1 -> (90,100).

      GND (supernet, must stay {R3/1, R4/2, R5/1}):
        wire (100,105)-(120,105) whose LEFT end (100,105) lands mid-span on
        OSC_OUT's vertical wire with **no junction** -> the bug bridge.
        label "GND" at (120,105); wires (120,105)-(120,120), (120,120)-(130,120);
        R4 pin2 -> (120,105), R3 pin1 -> (120,120), R5 pin1 -> (130,120).

      OSC32_OUT (similarly named, separate, must stay {R20/1}):
        wire (50,50)-(60,50), label "OSC32_OUT" at (50,50); R20 pin1 -> (60,50).

      TJUNC (positive control — mid-span T WITH a junction, must stay connected):
        vertical wire (30,30)-(30,40), label "TJUNC" at (30,30);
        horizontal wire (30,35)-(40,35) whose left end lands mid-span WITH a
        junction dot at (30,35); R10 pin2 -> (30,30), R11 pin1 -> (40,35).
    """
    body = [
        # OSC_OUT
        _wire(100, 100, 100, 110),
        _wire(100, 100, 90, 100),
        _label("OSC_OUT", 100, 100),
        _resistor("R1", 100, 113.81),
        _resistor("R7", 90, 103.81),
        # GND supernet — G1 left end T's mid-span onto OSC_OUT's wire, NO junction
        _wire(100, 105, 120, 105),
        _label("GND", 120, 105),
        _wire(120, 105, 120, 120),
        _wire(120, 120, 130, 120),
        _resistor("R4", 120, 101.19),
        _resistor("R3", 120, 123.81),
        _resistor("R5", 130, 123.81),
        # OSC32_OUT — similar name, separate
        _wire(50, 50, 60, 50),
        _label("OSC32_OUT", 50, 50),
        _resistor("R20", 60, 53.81),
        # TJUNC — mid-span T WITH junction (positive control)
        _wire(30, 30, 30, 40),
        _label("TJUNC", 30, 30),
        _wire(30, 35, 40, 35),
        _junction(30, 35),
        _resistor("R10", 30, 26.19),
        _resistor("R11", 40, 38.81),
    ]
    content = (
        '(kicad_sch (version 20250114) (generator "test")'
        ' (uuid 00000000-0000-0000-0000-000000000000) (paper "A4")\n'
        + _LIB_SYMBOLS
        + "".join(body)
        + '  (sheet_instances (path "/" (page "1")))\n)\n'
    )
    sch_path = tmp_path / "mid_span_short.kicad_sch"
    sch_path.write_text(content, encoding="utf-8")
    return sch_path


def _pins(sch, path: str, net: str) -> Set[str]:
    return {f"{c['component']}/{c['pin']}" for c in get_connections_for_net(sch, path, net)}


# ---------------------------------------------------------------------------
# Unit tests: _build_adjacency junction gating (the root-cause mechanism)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildAdjacencyJunctionGating:
    """A wire endpoint on another wire's interior only bridges with a junction."""

    def _t_wires(self):
        # Vertical wire (100,100)-(100,110); horizontal wire whose left end
        # (100,105) lands strictly on the vertical wire's interior.
        vertical = [_to_iu(100, 100), _to_iu(100, 110)]
        horizontal = [_to_iu(100, 105), _to_iu(110, 105)]
        return [vertical, horizontal]

    def test_mid_span_touch_without_junction_is_not_adjacent(self) -> None:
        adjacency, _ = _build_adjacency(self._t_wires(), junctions=set())
        assert adjacency[0] == set()
        assert adjacency[1] == set()

    def test_mid_span_touch_with_junction_is_adjacent(self) -> None:
        junctions: Set[Tuple[int, int]] = {_to_iu(100, 105)}
        adjacency, _ = _build_adjacency(self._t_wires(), junctions=junctions)
        assert adjacency[0] == {1}
        assert adjacency[1] == {0}

    def test_none_junctions_keeps_legacy_permissive_behaviour(self) -> None:
        # Backward-compat: callers that pass no junction info still get the old
        # (permissive) mid-span bridging, so mock-based unit tests are unaffected.
        adjacency, _ = _build_adjacency(self._t_wires())
        assert adjacency[0] == {1}
        assert adjacency[1] == {0}

    def test_shared_endpoint_still_bridges_without_junction(self) -> None:
        # Exact-endpoint adjacency must never depend on a junction dot.
        wires = [[_to_iu(0, 0), _to_iu(10, 0)], [_to_iu(10, 0), _to_iu(20, 0)]]
        adjacency, _ = _build_adjacency(wires, junctions=set())
        assert adjacency[0] == {1}
        assert adjacency[1] == {0}


@pytest.mark.unit
class TestParseJunctionsSexp:
    """_parse_junctions_sexp returns junction-dot positions as IU tuples."""

    def test_parses_junction_positions(self) -> None:
        import sexpdata

        sexp = sexpdata.loads(
            '(kicad_sch (junction (at 30 35) (diameter 0)) '
            "(junction (at 12.7 25.4)) (wire (pts (xy 0 0) (xy 1 0))))"
        )
        junctions = _parse_junctions_sexp(sexp)
        assert junctions == {_to_iu(30, 35), _to_iu(12.7, 25.4)}

    def test_no_junctions_returns_empty_set(self) -> None:
        import sexpdata

        sexp = sexpdata.loads('(kicad_sch (wire (pts (xy 0 0) (xy 1 0))))')
        assert _parse_junctions_sexp(sexp) == set()


# ---------------------------------------------------------------------------
# Integration tests: real fixture through get_connections_for_net + handler
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestMidSpanShortNetConnections:
    """End-to-end on a synthesised schematic: queries must agree with KiCad."""

    def test_osc_out_is_not_merged_into_gnd_supernet(self, tmp_path: Path) -> None:
        sch_path = _write_fixture(tmp_path)
        sch = SchematicManager.load_schematic(str(sch_path))
        assert sch is not None

        osc_out = _pins(sch, str(sch_path), "OSC_OUT")
        # OSC_OUT is exactly its own two pins — never the GND supernet's pins.
        assert osc_out == {"R1/1", "R7/1"}
        for gnd_pin in ("R3/1", "R4/2", "R5/1"):
            assert gnd_pin not in osc_out

    def test_gnd_supernet_excludes_osc_out(self, tmp_path: Path) -> None:
        sch_path = _write_fixture(tmp_path)
        sch = SchematicManager.load_schematic(str(sch_path))

        gnd = _pins(sch, str(sch_path), "GND")
        assert gnd == {"R3/1", "R4/2", "R5/1"}
        assert "R1/1" not in gnd
        assert "R7/1" not in gnd

    def test_similarly_named_osc32_out_stays_separate(self, tmp_path: Path) -> None:
        sch_path = _write_fixture(tmp_path)
        sch = SchematicManager.load_schematic(str(sch_path))

        assert _pins(sch, str(sch_path), "OSC32_OUT") == {"R20/1"}

    def test_junction_backed_t_still_connects(self, tmp_path: Path) -> None:
        # Positive control: the fix must NOT drop a mid-span T that carries a
        # junction dot — R11/1 (across the junction) stays on TJUNC.
        sch_path = _write_fixture(tmp_path)
        sch = SchematicManager.load_schematic(str(sch_path))

        assert _pins(sch, str(sch_path), "TJUNC") == {"R10/2", "R11/1"}

    def test_handler_get_net_connections_agrees(self, tmp_path: Path) -> None:
        # Exercise the actual MCP tool surface (handle_get_net_connections).
        sch_path = _write_fixture(tmp_path)

        with patch("kicad_interface.USE_IPC_BACKEND", False):
            from kicad_interface import KiCADInterface

            iface = KiCADInterface.__new__(KiCADInterface)
        result = iface._handle_get_net_connections(
            {"schematicPath": str(sch_path), "netName": "OSC_OUT"}
        )

        assert result["success"] is True
        got = {f"{c['component']}/{c['pin']}" for c in result["connections"]}
        assert got == {"R1/1", "R7/1"}

    def test_regression_reproduces_under_old_permissive_behaviour(self, tmp_path: Path) -> None:
        # Guard the guard: confirm this fixture actually exercises the bug. With
        # junction info suppressed (the pre-fix permissive path), OSC_OUT bleeds
        # into the GND supernet — the exact S4 symptom.
        sch_path = _write_fixture(tmp_path)
        sch = SchematicManager.load_schematic(str(sch_path))

        with patch(
            "commands.wire_connectivity._traversal._parse_junctions_sexp",
            return_value=None,
        ):
            osc_out = _pins(sch, str(sch_path), "OSC_OUT")

        # Pre-fix: the mid-span T with no junction merged OSC_OUT into GND.
        assert {"R3/1", "R4/2", "R5/1"}.issubset(osc_out)
