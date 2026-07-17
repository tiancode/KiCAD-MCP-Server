"""
Tests for mid-wire pin connection splitting.

kicad-cli ERC/netlist does NOT electrically join a symbol pin that sits on a
strict wire midpoint — only wire endpoints join (a power symbol dropped onto
a rail reads as pin_not_connected). The KiCad GUI connects such a pin by
breaking the segment under it; add_schematic_component and
move_schematic_component now do the same via
WireManager.break_wires_at_points.
"""

import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, List, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "python" / "templates"
EMPTY_SCH = TEMPLATES_DIR / "empty.kicad_sch"

_WIRE_RE = re.compile(r"\(wire \(pts \(xy ([-0-9.]+) ([-0-9.]+)\) \(xy ([-0-9.]+) ([-0-9.]+)\)\)")


def _wires(path: Path) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    content = path.read_text(encoding="utf-8")
    out = []
    for m in _WIRE_RE.finditer(content):
        x1, y1, x2, y2 = (float(g) for g in m.groups())
        out.append(((round(x1, 2), round(y1, 2)), (round(x2, 2), round(y2, 2))))
    return out


def _add_resistor(sch: Path, ref: str = "R1", x: float = 50.0, y: float = 50.0) -> dict:
    from handlers.schematic_component import handle_add_schematic_component

    return handle_add_schematic_component(
        iface=None,
        params={
            "schematicPath": str(sch),
            "snapToGrid": False,
            "component": {
                "library": "Device",
                "type": "R",
                "reference": ref,
                "value": "1k",
                "x": x,
                "y": y,
                "unit": 1,
            },
        },
    )


@pytest.mark.unit
class TestMidWirePinSplitOnAdd:
    def test_pin_landing_midwire_splits_segment(self, tmp_path: Any) -> None:
        """R at (50,50) has pin 1 at (50,46.19); a wire (40,46.19)->(60,46.19)
        passes strictly through it. Placement must split the wire so the pin
        sits on a real endpoint."""
        from commands.wire_manager import WireManager

        sch = tmp_path / "t.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        assert WireManager.add_wire(sch, [40.0, 46.19], [60.0, 46.19])

        res = _add_resistor(sch)
        assert res["success"] is True
        assert res.get("wiresSplit", 0) >= 1

        wires = _wires(sch)
        assert ((40.0, 46.19), (50.0, 46.19)) in wires
        assert ((50.0, 46.19), (60.0, 46.19)) in wires
        # The original unsplit segment is gone.
        assert ((40.0, 46.19), (60.0, 46.19)) not in wires

    def test_pin_not_on_wire_no_split(self, tmp_path: Any) -> None:
        from commands.wire_manager import WireManager

        sch = tmp_path / "t.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        assert WireManager.add_wire(sch, [40.0, 46.19], [60.0, 46.19])

        res = _add_resistor(sch, ref="R1", x=100.0, y=100.0)
        assert res["success"] is True
        assert "wiresSplit" not in res
        # Wire untouched.
        assert _wires(sch) == [((40.0, 46.19), (60.0, 46.19))]

    def test_pin_at_wire_endpoint_not_split(self, tmp_path: Any) -> None:
        """A pin already on a wire ENDPOINT connects without a split."""
        from commands.wire_manager import WireManager

        sch = tmp_path / "t.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        # Wire ends exactly at the future pin 1 location (50,46.19).
        assert WireManager.add_wire(sch, [40.0, 46.19], [50.0, 46.19])

        res = _add_resistor(sch)
        assert res["success"] is True
        assert "wiresSplit" not in res
        assert _wires(sch) == [((40.0, 46.19), (50.0, 46.19))]


@pytest.mark.integration
class TestMidWirePinSplitOnMove:
    def _make_schematic(self) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "test.kicad_sch"
        shutil.copy(EMPTY_SCH, tmp)
        return tmp

    def _insert(self, path: Path, sexp_text: str) -> None:
        content = path.read_text(encoding="utf-8")
        idx = content.rfind(")")
        path.write_text(content[:idx] + "\n" + sexp_text + "\n)", encoding="utf-8")

    def _add_resistor_instance(self, path: Path, ref: str, x: float, y: float) -> None:
        self._insert(
            path,
            f"""
  (symbol (lib_id "Device:R") (at {x} {y} 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "{uuid.uuid4()}")
    (property "Reference" "{ref}" (at {x + 2.032} {y} 90) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at {x} {y} 90) (effects (font (size 1.27 1.27))))
    (instances (project "test" (path "/" (reference "{ref}") (unit 1))))
  )""",
        )

    def _add_wire(self, path: Path, x1: float, y1: float, x2: float, y2: float) -> None:
        self._insert(
            path,
            f"  (wire (pts (xy {x1} {y1}) (xy {x2} {y2})) "
            f'(stroke (width 0) (type default)) (uuid "{uuid.uuid4()}"))',
        )

    def test_move_onto_midwire_splits_segment(self) -> None:
        from handlers.schematic_component._placement import handle_move_schematic_component

        sch = self._make_schematic()
        self._add_resistor_instance(sch, "R1", 70.0, 50.0)
        self._add_wire(sch, 40.0, 46.19, 60.0, 46.19)

        res = handle_move_schematic_component(
            iface=None,
            params={
                "schematicPath": str(sch),
                "reference": "R1",
                "position": {"x": 50.0, "y": 50.0},
                "snapToGrid": False,
            },
        )
        assert res["success"] is True
        assert res.get("wiresSplit", 0) >= 1

        wires = _wires(sch)
        assert ((40.0, 46.19), (50.0, 46.19)) in wires
        assert ((50.0, 46.19), (60.0, 46.19)) in wires

    def test_move_off_wire_no_split(self) -> None:
        from handlers.schematic_component._placement import handle_move_schematic_component

        sch = self._make_schematic()
        self._add_resistor_instance(sch, "R1", 70.0, 50.0)
        self._add_wire(sch, 40.0, 46.19, 60.0, 46.19)

        res = handle_move_schematic_component(
            iface=None,
            params={
                "schematicPath": str(sch),
                "reference": "R1",
                "position": {"x": 80.0, "y": 80.0},
                "snapToGrid": False,
            },
        )
        assert res["success"] is True
        assert "wiresSplit" not in res
        assert _wires(sch) == [((40.0, 46.19), (60.0, 46.19))]
