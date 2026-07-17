"""S6 regression: delete_schematic_component reports (and optionally removes)
the wire stubs + net labels left orphaned by a deleted symbol.

A GUI delete leaves attached wire stubs and net labels behind; the handler must
ALWAYS report them (counts + coordinates) and remove them only when
removeDanglingWires=true.
"""

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from commands.pin_locator import PinLocator  # noqa: E402
from handlers.schematic_component._placement import (  # noqa: E402
    handle_delete_schematic_component,
)
from kicad_interface import KiCADInterface  # noqa: E402


def _iface():
    return KiCADInterface.__new__(KiCADInterface)


_HEAD = textwrap.dedent("""\
    (kicad_sch (version 20250114) (generator "test")
      (uuid aaaaaaaa-0000-0000-0000-000000000001)
      (paper "A4")
      (lib_symbols
        (symbol "Device:R" (pin_names (offset 0.127)) (in_bom yes) (on_board yes)
          (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
          (property "Value" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
          (symbol "R_1_1"
            (pin passive line (at 0 3.81 270) (length 1.27)
              (name "~" (effects (font (size 1.27 1.27))))
              (number "1" (effects (font (size 1.27 1.27)))))
            (pin passive line (at 0 -3.81 90) (length 1.27)
              (name "~" (effects (font (size 1.27 1.27))))
              (number "2" (effects (font (size 1.27 1.27)))))
          )
        )
      )
      (symbol (lib_id "Device:R") (at 100 100 0) (unit 1)
        (uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        (property "Reference" "R1" (at 102 98 0) (effects (font (size 1.27 1.27))))
        (property "Value" "10k" (at 102 102 0) (effects (font (size 1.27 1.27))))
      )
    """)


def _wire(x1, y1, x2, y2, uid):
    return (
        f"  (wire (pts (xy {x1} {y1}) (xy {x2} {y2})) "
        f'(stroke (width 0) (type default)) (uuid "{uid}"))'
    )


def _label(name, x, y, uid):
    return f'  (label "{name}" (at {x} {y} 0) (effects (font (size 1.27 1.27))) (uuid "{uid}"))'


def _build_schematic(tmp_path) -> Path:
    """R1 with a stub wire + net label hanging off each of its two pins."""
    sch = tmp_path / "danglers.kicad_sch"
    # First write R1 alone so we can discover its real pin world coordinates.
    sch.write_text(_HEAD + ")\n", encoding="utf-8")
    pins = PinLocator().get_all_symbol_pins(sch, "R1")
    assert pins, "expected R1 pins to resolve"

    extra = []
    for i, (_num, (px, py)) in enumerate(sorted(pins.items())):
        fx, fy = px + 5.08, py  # stub extends 5.08 mm horizontally
        extra.append(_wire(px, py, fx, fy, f"11111111-0000-0000-0000-00000000000{i}"))
        extra.append(_label(f"NET{i}", fx, fy, f"22222222-0000-0000-0000-00000000000{i}"))

    sch.write_text(_HEAD + "\n".join(extra) + "\n)\n", encoding="utf-8")
    return sch


@pytest.mark.unit
class TestDeleteDanglingReport:
    def test_reports_dangling_without_removing_by_default(self, tmp_path):
        sch = _build_schematic(tmp_path)
        res = handle_delete_schematic_component(
            _iface(), {"schematicPath": str(sch), "reference": "R1"}
        )
        assert res["success"] is True
        d = res["dangling"]
        assert d["removed"] is False
        assert d["wireCount"] == 2
        assert d["labelCount"] == 2
        # Coordinates are reported.
        assert len(d["wires"]) == 2 and "start" in d["wires"][0]
        assert {lab["name"] for lab in d["labels"]} == {"NET0", "NET1"}
        # Default parity with the GUI: the stubs + labels are STILL in the file.
        content = sch.read_text(encoding="utf-8")
        assert "(wire" in content
        assert '(label "NET0"' in content
        # Symbol itself is gone.
        assert '"R1"' not in content

    def test_removes_dangling_when_requested(self, tmp_path):
        sch = _build_schematic(tmp_path)
        res = handle_delete_schematic_component(
            _iface(),
            {"schematicPath": str(sch), "reference": "R1", "removeDanglingWires": True},
        )
        assert res["success"] is True
        d = res["dangling"]
        assert d["removed"] is True
        assert d["wiresRemoved"] == 2
        assert d["labelsRemoved"] == 2
        content = sch.read_text(encoding="utf-8")
        assert "(wire" not in content
        assert "(label" not in content
        assert '"R1"' not in content

    def test_no_danglers_reports_zero(self, tmp_path):
        sch = tmp_path / "clean.kicad_sch"
        sch.write_text(_HEAD + ")\n", encoding="utf-8")
        res = handle_delete_schematic_component(
            _iface(), {"schematicPath": str(sch), "reference": "R1"}
        )
        assert res["success"] is True
        assert res["dangling"]["wireCount"] == 0
        assert res["dangling"]["labelCount"] == 0


@pytest.mark.unit
class TestDeleteThroughPathPreserved:
    """A wire touching a deleted pin is NOT necessarily a stub: when its far
    end is shared with another wire's endpoint it is a through-path (e.g. half
    of a rail that a mid-wire pin split produced) and must be KEPT — deleting
    it would silently cut the net. Only free-ended or mid-wire-teed spurs are
    removed."""

    def _pins(self, sch: Path):
        pins = PinLocator().get_all_symbol_pins(sch, "R1")
        assert pins, "expected R1 pins to resolve"
        return sorted(pins.items())

    def test_through_path_wire_and_label_kept(self, tmp_path):
        sch = tmp_path / "through.kicad_sch"
        sch.write_text(_HEAD + ")\n", encoding="utf-8")
        _num, (px, py) = self._pins(sch)[0]
        jx = px + 5.08  # junction point continuing the net

        extra = [
            _wire(px, py, jx, py, "33333333-0000-0000-0000-000000000001"),  # pin → J
            _wire(
                jx, py, jx + 10.16, py, "33333333-0000-0000-0000-000000000002"
            ),  # J → rest of net
            _label("VCC", jx, py, "33333333-0000-0000-0000-000000000003"),
        ]
        sch.write_text(_HEAD + "\n".join(extra) + "\n)\n", encoding="utf-8")

        res = handle_delete_schematic_component(
            _iface(),
            {"schematicPath": str(sch), "reference": "R1", "removeDanglingWires": True},
        )
        assert res["success"] is True
        d = res["dangling"]
        # The pin→J wire is a through-path: reported dangling count must be 0
        # and both wires + the label at J must survive.
        assert d["wireCount"] == 0
        assert d["labelCount"] == 0
        content = sch.read_text(encoding="utf-8")
        assert content.count("(wire") == 2
        assert '(label "VCC"' in content
        assert '"R1"' not in content

    def test_spur_teed_onto_rail_removed_rail_kept(self, tmp_path):
        sch = tmp_path / "spur.kicad_sch"
        sch.write_text(_HEAD + ")\n", encoding="utf-8")
        _num, (px, py) = self._pins(sch)[0]
        # A vertical rail wire passes through the stub's tee point (mid-span).
        tx = px + 5.08
        extra = [
            _wire(px, py, tx, py, "44444444-0000-0000-0000-000000000001"),  # pin → tee pt
            _wire(tx, py - 5.08, tx, py + 5.08, "44444444-0000-0000-0000-000000000002"),  # rail
        ]
        sch.write_text(_HEAD + "\n".join(extra) + "\n)\n", encoding="utf-8")

        res = handle_delete_schematic_component(
            _iface(),
            {"schematicPath": str(sch), "reference": "R1", "removeDanglingWires": True},
        )
        assert res["success"] is True
        d = res["dangling"]
        # The spur (pin → tee point mid-rail) is removed; the rail survives.
        assert d["wiresRemoved"] == 1
        content = sch.read_text(encoding="utf-8")
        assert content.count("(wire") == 1
        assert f"(xy {tx} {py - 5.08})" in content


def _r2_block(x: float, y: float) -> str:
    return (
        f'  (symbol (lib_id "Device:R") (at {x} {y} 0) (unit 1)\n'
        f'    (uuid "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee")\n'
        f'    (property "Reference" "R2" (at {x + 2} {y - 2} 0) '
        f"(effects (font (size 1.27 1.27))))\n"
        f'    (property "Value" "10k" (at {x + 2} {y + 2} 0) '
        f"(effects (font (size 1.27 1.27))))\n"
        f"  )\n"
    )


@pytest.mark.unit
class TestDeleteLivePinAndLabelAnchors:
    """Live geometry must survive removeDanglingWires: an endpoint on a
    surviving component's pin anchors the wire, and a label whose point still
    touches a surviving wire or live pin keeps naming that net."""

    def _pin(self, sch: Path, ref: str = "R1"):
        pins = PinLocator().get_all_symbol_pins(sch, ref)
        assert pins, f"expected {ref} pins to resolve"
        return sorted(pins.items())[0][1]

    def test_label_at_tee_of_removed_spur_survives(self, tmp_path):
        """The spur is removed, but the label at its tee point also names the
        surviving rail passing through — deleting it would rename the rail."""
        sch = tmp_path / "tee_label.kicad_sch"
        sch.write_text(_HEAD + ")\n", encoding="utf-8")
        px, py = self._pin(sch)
        tx = px + 5.08
        extra = [
            _wire(px, py, tx, py, "77777777-0000-0000-0000-000000000001"),  # spur
            _wire(tx, py - 5.08, tx, py + 5.08, "77777777-0000-0000-0000-000000000002"),  # rail
            _label("GND", tx, py, "77777777-0000-0000-0000-000000000003"),  # names the rail
        ]
        sch.write_text(_HEAD + "\n".join(extra) + "\n)\n", encoding="utf-8")

        res = handle_delete_schematic_component(
            _iface(),
            {"schematicPath": str(sch), "reference": "R1", "removeDanglingWires": True},
        )
        assert res["success"] is True
        assert res["dangling"]["wiresRemoved"] == 1
        assert res["dangling"]["labelCount"] == 0
        content = sch.read_text(encoding="utf-8")
        assert content.count("(wire") == 1  # rail survives
        assert '(label "GND"' in content  # rail keeps its net name

    def test_wire_to_live_pin_kept_with_its_label(self, tmp_path):
        """A wire whose far end sits on a SURVIVING component's pin is anchored
        (not a stub into nothing); removing it would strand that pin — and
        delete the label that ties the pin to its net."""
        sch = tmp_path / "live_pin.kicad_sch"
        head = _HEAD + _r2_block(120, 100)
        sch.write_text(head + ")\n", encoding="utf-8")
        p1x, p1y = self._pin(sch, "R1")
        p2x, p2y = self._pin(sch, "R2")
        extra = [
            _wire(p1x, p1y, p2x, p2y, "88888888-0000-0000-0000-000000000001"),
            _label("SDA", p2x, p2y, "88888888-0000-0000-0000-000000000002"),
        ]
        sch.write_text(head + "\n".join(extra) + "\n)\n", encoding="utf-8")

        res = handle_delete_schematic_component(
            _iface(),
            {"schematicPath": str(sch), "reference": "R1", "removeDanglingWires": True},
        )
        assert res["success"] is True
        assert res["dangling"]["wireCount"] == 0
        assert res["dangling"]["labelCount"] == 0
        content = sch.read_text(encoding="utf-8")
        assert content.count("(wire") == 1  # wire to R2's pin survives
        assert '(label "SDA"' in content  # R2's net membership survives
        assert '"R2"' in content

    def test_stacked_live_pin_keeps_wire(self, tmp_path):
        """R2 stacked exactly on R1: R1's pin points coincide with live R2
        pins, so wires hanging off those points still feed R2 and are kept."""
        sch = tmp_path / "stacked.kicad_sch"
        head = _HEAD + _r2_block(100, 100)  # same position as R1
        sch.write_text(head + ")\n", encoding="utf-8")
        px, py = self._pin(sch, "R1")
        extra = [
            _wire(px, py, px - 7.62, py, "99999999-0000-0000-0000-000000000001"),
        ]
        sch.write_text(head + "\n".join(extra) + "\n)\n", encoding="utf-8")

        res = handle_delete_schematic_component(
            _iface(),
            {"schematicPath": str(sch), "reference": "R1", "removeDanglingWires": True},
        )
        assert res["success"] is True
        assert res["dangling"]["wireCount"] == 0
        content = sch.read_text(encoding="utf-8")
        assert content.count("(wire") == 1  # still feeds R2's stacked pin

    def test_l_shaped_stub_chain_kept_whole(self, tmp_path):
        """Documented single-level limitation: a two-segment (L-shaped) stub
        chain is kept WHOLE — the first link's far end is anchored by the
        second link's endpoint. Pinned so the docs stay truthful."""
        sch = tmp_path / "l_chain.kicad_sch"
        sch.write_text(_HEAD + ")\n", encoding="utf-8")
        px, py = self._pin(sch)
        cx = px + 5.08
        extra = [
            _wire(px, py, cx, py, "55555555-0000-0000-0000-000000000001"),
            _wire(cx, py, cx, py - 5.08, "55555555-0000-0000-0000-000000000002"),
            _label("VCC", cx, py - 5.08, "55555555-0000-0000-0000-000000000003"),
        ]
        sch.write_text(_HEAD + "\n".join(extra) + "\n)\n", encoding="utf-8")

        res = handle_delete_schematic_component(
            _iface(),
            {"schematicPath": str(sch), "reference": "R1", "removeDanglingWires": True},
        )
        assert res["success"] is True
        assert res["dangling"]["wireCount"] == 0
        assert res["dangling"]["labelCount"] == 0
        content = sch.read_text(encoding="utf-8")
        assert content.count("(wire") == 2
        assert '(label "VCC"' in content
        # The message must not claim nothing was attached.
        assert "no removable" in res["message"]
