"""Fix 1 regression: rotate_schematic_component carries attached net labels.

Rotating a wired component used to leave its pin labels at the OLD pin
coordinates, so the component silently dropped off its nets (kicad-cli ERC then
reported "Pin not connected" / "Label not connected").  move_schematic_component
already relocates labels; this brings rotate to parity — a label sitting on a
rotated pin travels to the pin's NEW position, and the handler reports
``labelsMoved`` (like move) plus a warning listing any attached wires whose far
end did not rotate.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sexpdata
from sexpdata import Symbol

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from commands.pin_locator import PinLocator  # noqa: E402
from handlers.schematic_component._placement import (  # noqa: E402
    handle_rotate_schematic_component,
)

_HEAD = textwrap.dedent("""\
    (kicad_sch (version 20250114) (generator "test")
      (uuid aaaaaaaa-0000-0000-0000-000000000001)
      (paper "A4")
      (lib_symbols
        (symbol "Device:R" (pin_names (offset 0.127)) (in_bom yes) (on_board yes)
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


def _label(name: str, x: float, y: float, uid: str) -> str:
    return f'  (label "{name}" (at {x} {y} 0) (effects (font (size 1.27 1.27))) (uuid "{uid}"))'


def _wire(x1: float, y1: float, x2: float, y2: float, uid: str) -> str:
    return (
        f"  (wire (pts (xy {x1} {y1}) (xy {x2} {y2})) "
        f'(stroke (width 0) (type default)) (uuid "{uid}"))'
    )


def _labels(path: Path):
    data = sexpdata.loads(path.read_text(encoding="utf-8"))
    out = []
    for item in data:
        if isinstance(item, list) and item and item[0] == Symbol("label"):
            at = next(
                (s for s in item[1:] if isinstance(s, list) and s and s[0] == Symbol("at")), None
            )
            if at is not None:
                out.append((str(item[1]), (round(float(at[1]), 2), round(float(at[2]), 2))))
    return out


@pytest.mark.integration
class TestRotateCarriesLabels:
    def _build(self, tmp_path: Path) -> Path:
        sch = tmp_path / "rot.kicad_sch"
        sch.write_text(_HEAD + ")\n", encoding="utf-8")
        pins = PinLocator().get_all_symbol_pins(sch, "R1")
        assert pins, "R1 pins should resolve"
        # A net label placed directly on each pin (the add_schematic_net_label idiom).
        extra = []
        for i, (_num, (px, py)) in enumerate(sorted(pins.items())):
            extra.append(_label(f"NET{i}", px, py, f"22222222-0000-0000-0000-00000000000{i}"))
        sch.write_text(_HEAD + "\n".join(extra) + "\n)\n", encoding="utf-8")
        return sch

    def test_labels_follow_rotated_pins(self, tmp_path: Path) -> None:
        sch = self._build(tmp_path)
        pins_before = PinLocator().get_all_symbol_pins(sch, "R1")
        old_coords = {(round(x, 2), round(y, 2)) for x, y in pins_before.values()}

        PinLocator._SCHEMATIC_CACHE.clear()
        PinLocator._SEXP_CACHE.clear()
        PinLocator._PINDEF_CACHE.clear()

        res = handle_rotate_schematic_component(
            MagicMock(), {"schematicPath": str(sch), "reference": "R1", "angle": 90}
        )
        assert res["success"], res.get("message")
        assert res["labelsMoved"] == 2

        PinLocator._SCHEMATIC_CACHE.clear()
        PinLocator._SEXP_CACHE.clear()
        PinLocator._PINDEF_CACHE.clear()
        pins_after = PinLocator().get_all_symbol_pins(sch, "R1")
        new_coords = {(round(x, 2), round(y, 2)) for x, y in pins_after.values()}
        assert new_coords != old_coords, "rotation should move the pins"

        # Every label now sits exactly on a NEW pin coordinate — connectivity kept.
        label_coords = {pos for _n, pos in _labels(sch)}
        assert label_coords == new_coords
        # No label left orphaned at an old pin coordinate.
        assert not (label_coords & (old_coords - new_coords))

    def test_no_wires_no_warning(self, tmp_path: Path) -> None:
        sch = self._build(tmp_path)
        res = handle_rotate_schematic_component(
            MagicMock(), {"schematicPath": str(sch), "reference": "R1", "angle": 90}
        )
        assert res["success"]
        # Labels-only fixture: nothing was left un-carried.
        assert "attachedWires" not in res
        assert "warning" not in res

    def test_attached_wire_reported(self, tmp_path: Path) -> None:
        sch = tmp_path / "rotw.kicad_sch"
        sch.write_text(_HEAD + ")\n", encoding="utf-8")
        pins = PinLocator().get_all_symbol_pins(sch, "R1")
        _n1, (p1x, p1y) = sorted(pins.items())[0]
        # A wire from pin 1 out to a free far point (an external attached wire).
        extra = [_wire(p1x, p1y, p1x + 10.16, p1y, "33333333-0000-0000-0000-000000000001")]
        sch.write_text(_HEAD + "\n".join(extra) + "\n)\n", encoding="utf-8")
        PinLocator._SCHEMATIC_CACHE.clear()
        PinLocator._SEXP_CACHE.clear()
        PinLocator._PINDEF_CACHE.clear()

        res = handle_rotate_schematic_component(
            MagicMock(), {"schematicPath": str(sch), "reference": "R1", "angle": 90}
        )
        assert res["success"], res.get("message")
        assert res.get("attachedWires"), "the external attached wire must be reported"
        assert "warning" in res

    def test_attached_wire_endpoint_is_post_rotation(self, tmp_path: Path) -> None:
        """Finding 9: the reported pin-side endpoint must be the NEW (post-drag)
        pin position, not the vacated OLD one — cleanup guidance has to point at
        where the wire actually is."""
        sch = tmp_path / "rotw2.kicad_sch"
        sch.write_text(_HEAD + ")\n", encoding="utf-8")
        pins_before = PinLocator().get_all_symbol_pins(sch, "R1")
        _n1, (p1x, p1y) = sorted(pins_before.items())[0]
        old_pin = (round(p1x, 2), round(p1y, 2))
        far = (round(p1x + 10.16, 2), round(p1y, 2))
        extra = [_wire(p1x, p1y, p1x + 10.16, p1y, "44444444-0000-0000-0000-000000000001")]
        sch.write_text(_HEAD + "\n".join(extra) + "\n)\n", encoding="utf-8")
        PinLocator._SCHEMATIC_CACHE.clear()
        PinLocator._SEXP_CACHE.clear()
        PinLocator._PINDEF_CACHE.clear()

        res = handle_rotate_schematic_component(
            MagicMock(), {"schematicPath": str(sch), "reference": "R1", "angle": 90}
        )
        assert res["success"], res.get("message")

        PinLocator._SCHEMATIC_CACHE.clear()
        PinLocator._SEXP_CACHE.clear()
        PinLocator._PINDEF_CACHE.clear()
        pins_after = PinLocator().get_all_symbol_pins(sch, "R1")
        nx, ny = pins_after[_n1]
        new_pin = (round(nx, 2), round(ny, 2))
        assert new_pin != old_pin, "rotation must move the pin"

        aw = res["attachedWires"][0]
        endpoints = {
            (round(aw["start"]["x"], 2), round(aw["start"]["y"], 2)),
            (round(aw["end"]["x"], 2), round(aw["end"]["y"], 2)),
        }
        # Pin-side endpoint reflects the NEW pin position; the free far end is
        # unchanged; and the vacated OLD pin coordinate is never reported.
        assert new_pin in endpoints
        assert far in endpoints
        assert old_pin not in endpoints
