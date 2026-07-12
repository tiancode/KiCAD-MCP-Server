"""Regression tests for finding B1: ``sync_schematic_to_board`` must not
silently drop anonymous (label-less) wire nets.

A wire joining two pins with **no label anywhere** is a real net — KiCad's
"Update PCB from Schematic" auto-names it ``Net-(D1-A)`` / ``Net-(R1-Pad2)``.
The old label/#PWR/BFS parser (``_build_hierarchical_pad_net_map``) seeded net
names only from labels/power symbols, so a label-less cluster never got a name
and its pins fell out of ``pad_net_map`` — landing on the board as ``net=""``
(netCode 0): electrically absent, unroutable, DRC-invisible.

The fix derives pad→net from the authoritative kicad-cli ``kicadxml`` netlist
(``_pad_net_map_from_netlist_root``) which names anonymous nets exactly like
KiCad, and — when kicad-cli is unavailable — the BFS fallback now synthesizes
matching ``Net-(<ref>-<pin>)`` names for label-less clusters.

Coverage:
  * TestPadNetMapFromNetlistRoot — the authoritative parser (unit, no KiCAD):
    anonymous net pad membership, labeled + power nets, ``#PWR``/``#FLG`` and
    ``PWR_FLAG`` filtering.
  * TestFallbackSynthesizesAnonymousNets — the BFS fallback (unit, real skip
    mocked like tests/test_hierarchical_pad_net_map.py): label-less clusters get
    a synthetic KiCad-style name; labeled nets still map; single-pin dangling
    clusters stay absent.
  * TestKicadCliNetlistIntegration — env-gated (real kicad-cli): the full
    export→parse path on a valid inline .kicad_sch yields the anonymous net.
"""

import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


# ===========================================================================
# TestPadNetMapFromNetlistRoot — the authoritative kicad-cli netlist parser
# ===========================================================================

# A hand-written kicadxml netlist covering every case that matters:
#   +3V3   — labeled net (2 real pads)
#   GND    — power net (real pad + a #PWR pseudo-node that must be dropped)
#   Net-(D1-A) — the anonymous LED-anode net the old parser dropped (finding B1)
#   PWR_FLAG   — an ERC-marker "net" (with a #FLG node) that must never surface
_NETLIST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<export version="E">
  <components>
    <comp ref="R4"><value>1k</value><footprint>Resistor_SMD:R_0603_1608Metric</footprint></comp>
    <comp ref="D1"><value>LED</value><footprint>LED_SMD:LED_0603_1608Metric</footprint></comp>
  </components>
  <nets>
    <net code="1" name="+3V3">
      <node ref="R4" pin="1" pintype="passive"/>
      <node ref="U1" pin="1" pintype="power_in"/>
    </net>
    <net code="2" name="GND">
      <node ref="R1" pin="2" pintype="passive"/>
      <node ref="#PWR01" pin="1" pintype="power_in"/>
    </net>
    <net code="3" name="Net-(D1-A)">
      <node ref="D1" pin="2" pintype="passive"/>
      <node ref="R4" pin="2" pintype="passive"/>
    </net>
    <net code="4" name="PWR_FLAG">
      <node ref="#FLG01" pin="1" pintype="power_out"/>
    </net>
  </nets>
</export>
"""


@pytest.mark.unit
class TestPadNetMapFromNetlistRoot:
    def _map(self):
        from kicad_interface import KiCADInterface

        root = ET.fromstring(_NETLIST_XML)
        return KiCADInterface._pad_net_map_from_netlist_root(root)

    def test_anonymous_net_pads_are_mapped(self):
        """The headline B1 fix: the label-less Net-(D1-A) reaches both pads."""
        pad_net_map, net_names = self._map()
        assert pad_net_map[("D1", "2")] == "Net-(D1-A)"
        assert pad_net_map[("R4", "2")] == "Net-(D1-A)"
        assert "Net-(D1-A)" in net_names

    def test_labeled_and_power_nets_are_mapped(self):
        pad_net_map, net_names = self._map()
        assert pad_net_map[("R4", "1")] == "+3V3"
        assert pad_net_map[("R1", "2")] == "GND"
        assert {"+3V3", "GND"} <= net_names

    def test_power_and_flag_pseudo_refs_are_dropped(self):
        """#PWR / #FLG have no PCB pad — they must never enter the pad map."""
        pad_net_map, _ = self._map()
        assert ("#PWR01", "1") not in pad_net_map
        assert ("#FLG01", "1") not in pad_net_map
        assert not any(ref.startswith("#") for ref, _ in pad_net_map)

    def test_pwr_flag_never_becomes_a_net(self):
        """PWR_FLAG is an ERC marker, not a net (project invariant)."""
        _, net_names = self._map()
        assert "PWR_FLAG" not in net_names

    def test_missing_nets_section_returns_empty(self):
        from kicad_interface import KiCADInterface

        root = ET.fromstring("<export version='E'><components/></export>")
        pad_net_map, net_names = KiCADInterface._pad_net_map_from_netlist_root(root)
        assert pad_net_map == {}
        assert net_names == set()


# ===========================================================================
# TestFallbackSynthesizesAnonymousNets — the BFS fallback (kicad-cli absent)
# ===========================================================================
#
# Mirrors tests/test_hierarchical_pad_net_map.py: mock the kicad-skip
# Schematic (wires/labels/symbol list) while a real on-disk .kicad_sch supplies
# lib_symbols so PinLocator can compute absolute pin positions *and* pin names
# (needed to synthesize KiCad-style Net-(<ref>-<pinname>) names).
#
# TestLib:R  pin "1" @ (-1.27, 0) name "~"   pin "2" @ (1.27, 0) name "~"
# TestLib:D  pin "1" @ (-1.27, 0) name "K"   pin "2" @ (1.27, 0) name "A"
# ---------------------------------------------------------------------------

_LIB_SYMBOLS = textwrap.dedent("""\
      (lib_symbols
        (symbol "TestLib:R"
          (symbol "TestLib:R_1_1"
            (pin passive line (at -1.27 0 0) (length 0)
              (name "~" (effects (font (size 1.27 1.27))))
              (number "1" (effects (font (size 1.27 1.27)))))
            (pin passive line (at 1.27 0 180) (length 0)
              (name "~" (effects (font (size 1.27 1.27))))
              (number "2" (effects (font (size 1.27 1.27)))))))
        (symbol "TestLib:D"
          (symbol "TestLib:D_1_1"
            (pin passive line (at -1.27 0 0) (length 0)
              (name "K" (effects (font (size 1.27 1.27))))
              (number "1" (effects (font (size 1.27 1.27)))))
            (pin passive line (at 1.27 0 180) (length 0)
              (name "A" (effects (font (size 1.27 1.27))))
              (number "2" (effects (font (size 1.27 1.27)))))))
      )
""")


def _write_sch_with_instances(path: Path, instances) -> None:
    """Write a .kicad_sch with the TestLib lib_symbols + symbol instances.

    ``instances`` is a list of (reference, lib_id, x, y) tuples (rotation 0).
    PinLocator reads position/rotation/lib_id from this file via sexpdata.
    """
    parts = ["(kicad_sch (version 20231120)", _LIB_SYMBOLS]
    for i, (ref, lib_id, x, y) in enumerate(instances, start=1):
        uuid = f"00000000-0000-0000-0000-{i:012d}"
        parts.append(
            f'  (symbol (lib_id "{lib_id}") (at {x} {y} 0) (unit 1)\n'
            f"    (uuid {uuid})\n"
            f'    (property "Reference" "{ref}" (at {x} {y - 2.54} 0))\n'
            f'    (property "Value" "~" (at {x} {y + 2.54} 0))\n'
            f'    (pin "1" (uuid {uuid[:-1]}a))\n'
            f'    (pin "2" (uuid {uuid[:-1]}b))\n'
            f"  )\n"
        )
    parts.append(")\n")
    path.write_text("".join(parts))


def _sym_mock(ref: str, lib_id: str, x: float, y: float) -> MagicMock:
    m = MagicMock()
    m.property.Reference.value = ref
    m.property.Value.value = "~"
    m.lib_id.value = lib_id
    m.at.value = [x, y, 0]
    return m


def _wire_mock(x1: float, y1: float, x2: float, y2: float) -> MagicMock:
    m = MagicMock()
    p1, p2 = MagicMock(), MagicMock()
    p1.value = [x1, y1]
    p2.value = [x2, y2]
    m.pts.xy = [p1, p2]
    return m


def _lbl_mock(name: str, x: float, y: float) -> MagicMock:
    m = MagicMock()
    m.value = name
    m.at.value = [x, y, 0]
    return m


def _sch_mock(symbols=(), labels=(), wires=()) -> MagicMock:
    m = MagicMock()
    m.symbol = list(symbols)
    m.label = list(labels)
    m.global_label = []
    m.hierarchical_label = []
    m.wire = list(wires)
    return m


@pytest.mark.unit
class TestFallbackSynthesizesAnonymousNets:
    """kicad-cli absent → the BFS parser synthesizes Net-(<ref>-<pin>) names."""

    def _build(self, tmp_path: Path):
        # Two resistors + one LED laid out in a column (rotation 0):
        #   R1 @ (10,10)  pins: 1=(8.73,10)  2=(11.27,10)
        #   D1 @ (10,20)  pins: K1=(8.73,20) A2=(11.27,20)
        #   R2 @ (10,30)  pins: 1=(8.73,30)  2=(11.27,30)
        # Wires (NO label): R1/2--D1/2(A) and D1/1(K)--R2/1  → two anonymous
        # nets.  A label "VOUT" drives R1/1 (a labeled net).  R2/2 dangles.
        sch = tmp_path / "chain.kicad_sch"
        _write_sch_with_instances(
            sch,
            [
                ("R1", "TestLib:R", 10.0, 10.0),
                ("D1", "TestLib:D", 10.0, 20.0),
                ("R2", "TestLib:R", 10.0, 30.0),
            ],
        )
        mock_sch = _sch_mock(
            symbols=[
                _sym_mock("R1", "TestLib:R", 10.0, 10.0),
                _sym_mock("D1", "TestLib:D", 10.0, 20.0),
                _sym_mock("R2", "TestLib:R", 10.0, 30.0),
            ],
            labels=[_lbl_mock("VOUT", 8.73, 10.0)],
            wires=[
                _wire_mock(11.27, 10.0, 11.27, 20.0),  # R1/2 -- D1/2 (anode)
                _wire_mock(8.73, 20.0, 8.73, 30.0),  # D1/1 (K) -- R2/1
            ],
        )
        with patch("kicad_interface.USE_IPC_BACKEND", False):
            from kicad_interface import KiCADInterface

            iface = KiCADInterface.__new__(KiCADInterface)
        with (
            patch("skip.Schematic", return_value=mock_sch),
            patch("commands.pin_locator.Schematic", return_value=mock_sch),
        ):
            return iface._build_hierarchical_pad_net_map(str(sch))

    def test_label_less_led_anode_cluster_gets_kicad_style_name(self, tmp_path):
        """The B1 case in the fallback: R1/2--D1/2 (no label) → Net-(D1-A)
        on BOTH pads, named after the lowest-designator pin (D1 < R1) using
        its pin name 'A' — matching kicad-cli's Net-(D1-A)."""
        pad_net_map, net_names = self._build(tmp_path)
        assert pad_net_map[("R1", "2")] == "Net-(D1-A)"
        assert pad_net_map[("D1", "2")] == "Net-(D1-A)"
        assert "Net-(D1-A)" in net_names

    def test_second_anonymous_cluster_uses_pin_name_K(self, tmp_path):
        """D1/1(K)--R2/1 (no label) → Net-(D1-K) (D1 < R2, pin name 'K')."""
        pad_net_map, net_names = self._build(tmp_path)
        assert pad_net_map[("D1", "1")] == "Net-(D1-K)"
        assert pad_net_map[("R2", "1")] == "Net-(D1-K)"
        assert "Net-(D1-K)" in net_names

    def test_labeled_net_still_maps(self, tmp_path):
        pad_net_map, net_names = self._build(tmp_path)
        assert pad_net_map[("R1", "1")] == "VOUT"
        assert "VOUT" in net_names

    def test_dangling_single_pin_cluster_is_absent(self, tmp_path):
        """R2/2 is on no wire → no net (KiCad leaves a lone pin unconnected)."""
        pad_net_map, _ = self._build(tmp_path)
        assert ("R2", "2") not in pad_net_map

    def test_no_pwr_flag_leaks(self, tmp_path):
        _, net_names = self._build(tmp_path)
        assert "PWR_FLAG" not in net_names


# ===========================================================================
# TestKicadCliNetlistIntegration — real kicad-cli export→parse (env-gated)
# ===========================================================================
#
# A valid inline .kicad_sch (2 resistors joined by a label-less wire).  KiCad
# auto-names the anonymous net Net-(R1-Pad2); the real export+parse path must
# surface it with both pads.  The lib_symbol is the full Device:R definition so
# kicad-cli recognises the instances (a stripped-down symbol yields an empty
# <components/>).
# ---------------------------------------------------------------------------

_VALID_SCH = r"""(kicad_sch (version 20250114) (generator "test") (uuid f2d44146-f4f1-4535-b022-ac7a545728ec) (paper "A4")
(lib_symbols
(symbol "Device:R"
        (pin_numbers (hide yes))
        (pin_names (offset 0))
        (exclude_from_sim no)
        (in_bom yes)
        (on_board yes)
        (property "Reference" "R" (at 2.032 0 90))
        (property "Value" "R" (at 0 0 90))
        (property "Footprint" "" (at -1.778 0 90) (hide yes))
        (property "Datasheet" "~" (at 0 0 0) (hide yes))
        (property "Description" "Resistor" (at 0 0 0) (hide yes))
        (symbol "R_0_1"
            (rectangle (start -1.016 -2.54) (end 1.016 2.54)
                (stroke (width 0.254) (type default))
                (fill (type none))))
        (symbol "R_1_1"
            (pin passive line (at 0 3.81 270) (length 1.27)
                (name "~" (effects (font (size 1.27 1.27))))
                (number "1" (effects (font (size 1.27 1.27)))))
            (pin passive line (at 0 -3.81 90) (length 1.27)
                (name "~" (effects (font (size 1.27 1.27))))
                (number "2" (effects (font (size 1.27 1.27)))))))
)
    (symbol (lib_id "Device:R") (at 100 100 0) (unit 1) (in_bom yes) (on_board yes) (dnp no)
        (uuid "10e6fb00-9294-4278-8bfe-3b0539320c51")
        (property "Reference" "R1" (at 102.87 97.46 0) (effects (font (size 1.27 1.27))))
        (property "Value" "10k" (at 102.87 102.54 0) (effects (font (size 1.27 1.27))))
        (property "Footprint" "Resistor_SMD:R_0603_1608Metric" (at 100 100 0) (effects (font (size 1.27 1.27)) (hide yes)))
        (property "Datasheet" "~" (at 100 100 0) (effects (font (size 1.27 1.27)) (hide yes)))
        (instances (project "mini" (path "/" (reference "R1") (unit 1)))))
    (symbol (lib_id "Device:R") (at 100 115 0) (unit 1) (in_bom yes) (on_board yes) (dnp no)
        (uuid "20e6fb00-9294-4278-8bfe-3b0539320c51")
        (property "Reference" "R2" (at 102.87 112.46 0) (effects (font (size 1.27 1.27))))
        (property "Value" "10k" (at 102.87 117.54 0) (effects (font (size 1.27 1.27))))
        (property "Footprint" "Resistor_SMD:R_0603_1608Metric" (at 100 115 0) (effects (font (size 1.27 1.27)) (hide yes)))
        (property "Datasheet" "~" (at 100 115 0) (effects (font (size 1.27 1.27)) (hide yes)))
        (instances (project "mini" (path "/" (reference "R2") (unit 1)))))
    (wire (pts (xy 100 103.81) (xy 100 111.19)) (stroke (width 0) (type default)) (uuid cccccccc-1111-1111-1111-111111111111))
)
"""


def _kicad_cli_available() -> bool:
    try:
        from utils.kicad_cli import find_kicad_cli

        return find_kicad_cli() is not None
    except Exception:
        return False


@pytest.mark.integration
@pytest.mark.skipif(not _kicad_cli_available(), reason="kicad-cli not installed")
def test_real_kicad_cli_export_names_anonymous_net(tmp_path: Any):
    """End-to-end with the real kicad-cli: export the netlist and parse it —
    the label-less R1/2--R2/1 net must appear as Net-(R1-Pad2) on both pads."""
    from kicad_interface import KiCADInterface

    sch = tmp_path / "mini.kicad_sch"
    sch.write_text(_VALID_SCH)

    iface = KiCADInterface.__new__(KiCADInterface)
    root = iface._export_schematic_netlist_xml(str(sch))
    assert root is not None, "kicad-cli export returned no netlist"

    pad_net_map, net_names = KiCADInterface._pad_net_map_from_netlist_root(root)
    # KiCad names the anonymous net after the lowest pin with no pin-name:
    # Net-(R1-Pad2).  Both endpoints of the label-less wire must carry it.
    assert pad_net_map.get(("R1", "2")) == "Net-(R1-Pad2)"
    assert pad_net_map.get(("R2", "1")) == "Net-(R1-Pad2)"
    assert "Net-(R1-Pad2)" in net_names
    assert "PWR_FLAG" not in net_names


# ===========================================================================
# TestSyncHonestDegradation — the sync response must not lie when nets drop
# ===========================================================================


def _pad_mock(number: str) -> MagicMock:
    pad = MagicMock(name=f"pad_{number}")
    pad.GetNumber.return_value = number
    return pad


def _fp_mock(reference: str, pad_numbers) -> MagicMock:
    fp = MagicMock(name=f"fp_{reference}")
    fp.GetReference.return_value = reference
    fp.Pads.return_value = [_pad_mock(n) for n in pad_numbers]
    return fp


@pytest.mark.unit
class TestSyncHonestDegradation:
    """When a schematic pin the netlist expects never lands on a board pad, the
    handler keeps success=True but surfaces the FULL unmatched list, a warning,
    and any nets that dropped entirely — no more silent electrical loss (B1)."""

    def _run(self, pad_net_map, net_names, board_fps):
        from handlers.schematic_io._io import handle_sync_schematic_to_board

        board = MagicMock(name="board")
        board.GetFileName.return_value = "/proj/board.kicad_pcb"
        board.GetFootprints.return_value = board_fps
        # NetsByName().has_key(...) truthy so every mapped net is assignable.
        board.GetNetInfo.return_value.NetsByName.return_value = MagicMock()

        iface = MagicMock(name="iface")
        iface.board = board
        iface._swig_writes_landed = False
        iface.ipc_board_api = None
        # kicad-cli present but the map/net_names come from our fixture.
        iface._export_schematic_netlist_xml.return_value = MagicMock(name="netlist_root")
        iface._pad_net_map_from_netlist_root.return_value = (pad_net_map, net_names)
        iface._add_missing_footprints_from_schematic.return_value = ([], [])
        iface._safe_load_board.return_value = board

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False) as tmp:
            tmp.write(b"(kicad_sch)\n")
            sch = tmp.name
        return handle_sync_schematic_to_board(iface, {"schematicPath": sch})

    def test_dropped_anonymous_net_is_reported(self):
        # Board only has R1/1; the netlist also expects the anonymous
        # Net-(D1-A) on D1/2 + R4/2 (footprints not on the board) → dropped.
        pad_net_map = {
            ("R1", "1"): "NETA",
            ("D1", "2"): "Net-(D1-A)",
            ("R4", "2"): "Net-(D1-A)",
        }
        net_names = {"NETA", "Net-(D1-A)"}
        result = self._run(pad_net_map, net_names, [_fp_mock("R1", ["1"])])

        assert result["success"] is True  # honest, not a hard failure
        assert result["pads_assigned"] == 1
        assert sorted(result["unmatched_pads"]) == ["D1/2", "R4/2"]
        assert result["dropped_nets"] == ["Net-(D1-A)"]
        assert "warning" in result
        assert "Net-(D1-A)" in result["warning"]

    def test_fully_matched_sync_has_no_warning(self):
        pad_net_map = {("R1", "1"): "NETA", ("R1", "2"): "NETB"}
        net_names = {"NETA", "NETB"}
        result = self._run(pad_net_map, net_names, [_fp_mock("R1", ["1", "2"])])

        assert result["success"] is True
        assert result["pads_assigned"] == 2
        assert result["unmatched_pads"] == []
        assert "warning" not in result
        assert "dropped_nets" not in result
        assert result["netlist_source"] == "kicad-cli"
