"""Fix 2 regression: delete_schematic_component truthfully reports KEPT wires.

Deleting a symbol whose pin had a wire that anchors on a LIVE pin (or is a
through-path) used to say "no attached wire stubs or labels found" while leaving
the wire dangling. The wire is CORRECTLY kept (dangling-wire cleanup must not
strip live geometry), but the report must say so — ``dangling.keptWires`` with a
``reason`` plus ``dangling.danglingEndpoints`` for the now-free wire end — rather
than claim nothing was attached. A true single-segment stub with a free far end
is still removed.
"""

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from commands.pin_locator import PinLocator  # noqa: E402
from handlers.schematic_component._placement import (  # noqa: E402
    handle_delete_schematic_component,
)
from kicad_interface import KiCADInterface  # noqa: E402

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


def _iface() -> Any:
    return KiCADInterface.__new__(KiCADInterface)


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


def _wire(x1, y1, x2, y2, uid):
    return (
        f"  (wire (pts (xy {x1} {y1}) (xy {x2} {y2})) "
        f'(stroke (width 0) (type default)) (uuid "{uid}"))'
    )


def _pin(sch: Path, ref: str):
    pins = PinLocator().get_all_symbol_pins(sch, ref)
    assert pins, f"expected {ref} pins to resolve"
    return sorted(pins.items())[0][1]


@pytest.mark.integration
class TestDeleteKeptWireReport:
    def test_direct_wire_to_live_pin_reported_kept(self, tmp_path):
        """A wire from R1's pin straight to R2's pin is KEPT (anchors on R2), and
        reported as such with a truthful reason + the now-dangling endpoint."""
        sch = tmp_path / "direct.kicad_sch"
        head = _HEAD + _r2_block(120, 100)
        sch.write_text(head + ")\n", encoding="utf-8")
        p1x, p1y = _pin(sch, "R1")
        p2x, p2y = _pin(sch, "R2")
        sch.write_text(
            head + _wire(p1x, p1y, p2x, p2y, "cccccccc-0000-0000-0000-000000000001") + "\n)\n",
            encoding="utf-8",
        )

        res = handle_delete_schematic_component(
            _iface(), {"schematicPath": str(sch), "reference": "R1"}
        )
        assert res["success"], res.get("message")
        d = res["dangling"]
        # Not a removable stub — nothing in the historical dangling arrays...
        assert d["wireCount"] == 0
        # ...but truthfully reported as a KEPT wire naming the live anchor.
        assert d.get("keptWireCount") == 1
        kept = d["keptWires"][0]
        assert "R2" in kept["reason"]
        # The R1-pin endpoint is now a genuine dangling wire end.
        eps = d.get("danglingEndpoints") or []
        assert any(abs(e["x"] - p1x) < 0.05 and abs(e["y"] - p1y) < 0.05 for e in eps)
        # The message must NOT claim nothing was attached.
        assert "no attached wire" not in res["message"].lower()
        assert "Kept 1 attached wire" in res["message"]
        # The wire itself survived in the file.
        assert "(wire" in sch.read_text(encoding="utf-8")

    def test_l_wire_through_path_reported_kept(self, tmp_path):
        """An L-shaped (two-segment) wire from R1's pin whose first segment ends
        on a through-path corner is kept and reported (the connect_to_net bug)."""
        sch = tmp_path / "lwire.kicad_sch"
        head = _HEAD + _r2_block(120, 100)
        sch.write_text(head + ")\n", encoding="utf-8")
        p1x, p1y = _pin(sch, "R1")
        p2x, p2y = _pin(sch, "R2")
        cx = p1x + 5.08  # the corner of the L
        extra = [
            _wire(p1x, p1y, cx, p1y, "dddddddd-0000-0000-0000-000000000001"),  # pin → corner
            _wire(cx, p1y, cx, p2y, "dddddddd-0000-0000-0000-000000000002"),  # corner → level
            _wire(cx, p2y, p2x, p2y, "dddddddd-0000-0000-0000-000000000003"),  # → R2 pin
        ]
        sch.write_text(head + "\n".join(extra) + "\n)\n", encoding="utf-8")

        res = handle_delete_schematic_component(
            _iface(), {"schematicPath": str(sch), "reference": "R1", "removeDanglingWires": True}
        )
        assert res["success"], res.get("message")
        d = res["dangling"]
        assert d["wireCount"] == 0  # nothing removable (through-path)
        assert d.get("keptWireCount") == 1  # the pin→corner segment reported kept
        assert "through-path" in d["keptWires"][0]["reason"]
        eps = d.get("danglingEndpoints") or []
        assert any(abs(e["x"] - p1x) < 0.05 and abs(e["y"] - p1y) < 0.05 for e in eps)
        # All three L segments survived (nothing was cut).
        assert sch.read_text(encoding="utf-8").count("(wire") == 3

    def test_true_stub_still_removed(self, tmp_path):
        """A single-segment stub with a FREE far end is still removed and NOT
        reported as kept."""
        sch = tmp_path / "stub.kicad_sch"
        sch.write_text(_HEAD + ")\n", encoding="utf-8")
        p1x, p1y = _pin(sch, "R1")
        sch.write_text(
            _HEAD
            + _wire(p1x, p1y, p1x + 5.08, p1y, "eeeeeeee-0000-0000-0000-000000000001")
            + "\n)\n",
            encoding="utf-8",
        )

        res = handle_delete_schematic_component(
            _iface(), {"schematicPath": str(sch), "reference": "R1", "removeDanglingWires": True}
        )
        assert res["success"], res.get("message")
        d = res["dangling"]
        assert d["wireCount"] == 1
        assert d["wiresRemoved"] == 1
        assert not d.get("keptWires")
        assert "(wire" not in sch.read_text(encoding="utf-8")
