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


_HEAD = textwrap.dedent(
    """\
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
    """
)


def _wire(x1, y1, x2, y2, uid):
    return (
        f'  (wire (pts (xy {x1} {y1}) (xy {x2} {y2})) '
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
