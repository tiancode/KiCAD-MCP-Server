"""
Bug 1 regression: move_schematic_component moves connect_to_net stubs + labels.

A ``connect_to_net`` connection is a short wire stub from the pin plus a net
label at the stub's far end.  Before the fix, moving the component dragged ONLY
the pin-side endpoint of the stub — the far endpoint (and its label) stayed at
the old absolute coordinate, producing a long stretched diagonal wire that kept
the moved pin electrically tied to the OLD location (in the GD32 E2E run this
preserved a +3V3↔GND short).

The fix: a *stub* (one endpoint on a moved pin, the other endpoint free / holding
only a label) moves rigidly — both endpoints and the far-end label translate by
the same delta as the pin.  A wire whose far endpoint is genuinely anchored keeps
stretch behavior, but a label sitting on the MOVED endpoint still travels with it.

Unit tests drive the WireDragger helpers on synthetic sexpdata (no disk I/O).
Integration tests build real .kicad_sch files and exercise the move handler.
"""

from __future__ import annotations

import math
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import sexpdata
from sexpdata import Symbol

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from commands.wire_dragger import EPS, WireDragger  # noqa: E402

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "python" / "templates" / "empty.kicad_sch"


# ---------------------------------------------------------------------------
# Synthetic sexpdata helpers (mirror test_move_with_wire_preservation.py)
# ---------------------------------------------------------------------------


def _sym(name: str) -> Symbol:
    return Symbol(name)


def _make_wire(x1: Any, y1: Any, x2: Any, y2: Any) -> Any:
    return [
        _sym("wire"),
        [_sym("pts"), [_sym("xy"), x1, y1], [_sym("xy"), x2, y2]],
        [_sym("stroke"), [_sym("width"), 0], [_sym("type"), _sym("default")]],
        [_sym("uuid"), str(uuid.uuid4())],
    ]


def _make_label(name: str, x: Any, y: Any, kind: str = "label") -> Any:
    return [
        _sym(kind),
        name,
        [_sym("at"), x, y, 0],
        [_sym("effects"), [_sym("font"), [_sym("size"), 1.27, 1.27]]],
        [_sym("uuid"), str(uuid.uuid4())],
    ]


def _make_symbol(ref: str, x: Any, y: Any, rotation: Any = 0, lib_id: str = "Device:R") -> Any:
    return [
        _sym("symbol"),
        [_sym("lib_id"), lib_id],
        [_sym("at"), x, y, rotation],
        [_sym("unit"), 1],
        [_sym("property"), "Reference", ref, [_sym("at"), x + 2, y, 0]],
        [_sym("property"), "Value", "10k", [_sym("at"), x, y, 0]],
    ]


def _make_lib_symbol_r() -> Any:
    """Device:R lib entry — pins at (0, 3.81) and (0, -3.81)."""
    return [
        _sym("symbol"),
        "Device:R",
        [
            _sym("symbol"),
            "R_1_1",
            [
                _sym("pin"),
                _sym("passive"),
                _sym("line"),
                [_sym("at"), 0, 3.81, 270],
                [_sym("length"), 1.27],
                [_sym("name"), "~", [_sym("effects"), [_sym("font"), [_sym("size"), 1.27, 1.27]]]],
                [
                    _sym("number"),
                    "1",
                    [_sym("effects"), [_sym("font"), [_sym("size"), 1.27, 1.27]]],
                ],
            ],
            [
                _sym("pin"),
                _sym("passive"),
                _sym("line"),
                [_sym("at"), 0, -3.81, 90],
                [_sym("length"), 1.27],
                [_sym("name"), "~", [_sym("effects"), [_sym("font"), [_sym("size"), 1.27, 1.27]]]],
                [
                    _sym("number"),
                    "2",
                    [_sym("effects"), [_sym("font"), [_sym("size"), 1.27, 1.27]]],
                ],
            ],
        ],
    ]


def _make_sch_data(extra_items: Any = None) -> Any:
    data = [
        _sym("kicad_sch"),
        [_sym("lib_symbols"), _make_lib_symbol_r()],
        [_sym("sheet_instances"), [_sym("path"), "/", [_sym("page"), "1"]]],
    ]
    if extra_items:
        for item in extra_items:
            data.insert(len(data) - 1, item)
    return data


# ---------------------------------------------------------------------------
# Unit tests — WireDragger.collect_stub_far_endpoints
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCollectStubFarEndpoints:
    """R1 pin2 (lib y=-3.81) lands at world y=+3.81 (Y-flip). See pin_locator tests."""

    def test_free_stub_far_endpoint_included(self) -> None:
        # Stub from R1 pin2 (0, 3.81) to a free point (0, 7.62).
        sch = _make_sch_data([_make_symbol("R1", 0, 0), _make_wire(0, 3.81, 0, 7.62)])
        pin_positions = WireDragger.compute_pin_positions(sch, "R1", 10, 0)  # delta (10, 0)
        far = WireDragger.collect_stub_far_endpoints(sch, "R1", pin_positions)
        assert (0.0, 7.62) in far
        nx, ny = far[(0.0, 7.62)]
        assert abs(nx - 10.0) < 1e-4 and abs(ny - 7.62) < 1e-4

    def test_far_endpoint_shared_with_another_wire_is_anchored(self) -> None:
        # Wire A: pin2 (0, 3.81) -> (0, 7.62). Wire B shares (0, 7.62) => anchored.
        sch = _make_sch_data(
            [
                _make_symbol("R1", 0, 0),
                _make_wire(0, 3.81, 0, 7.62),
                _make_wire(0, 7.62, 5, 7.62),
            ]
        )
        pin_positions = WireDragger.compute_pin_positions(sch, "R1", 10, 0)
        far = WireDragger.collect_stub_far_endpoints(sch, "R1", pin_positions)
        assert far == {}  # far end anchored → keep stretch behavior

    def test_far_endpoint_on_other_component_pin_is_anchored(self) -> None:
        # R2 placed so its pin1 (world 0, 3.81 - 7.62 = -3.81)... instead put R2 so a
        # pin lands at (0, 7.62): R2 at (0, 11.43) has pin2 world (0, 11.43-3.81=7.62).
        sch = _make_sch_data(
            [
                _make_symbol("R1", 0, 0),
                _make_symbol("R2", 0, 11.43),
                _make_wire(0, 3.81, 0, 7.62),
            ]
        )
        pin_positions = WireDragger.compute_pin_positions(sch, "R1", 10, 0)
        far = WireDragger.collect_stub_far_endpoints(sch, "R1", pin_positions)
        assert far == {}  # far end is a real pin → not a free stub

    def test_wire_between_two_pins_of_moved_component_not_a_stub(self) -> None:
        # Wire pin1 (0,-3.81) -> pin2 (0,3.81): BOTH ends are moved pins.
        sch = _make_sch_data([_make_symbol("R1", 0, 0), _make_wire(0, -3.81, 0, 3.81)])
        pin_positions = WireDragger.compute_pin_positions(sch, "R1", 10, 0)
        far = WireDragger.collect_stub_far_endpoints(sch, "R1", pin_positions)
        assert far == {}

    def test_empty_pin_positions(self) -> None:
        sch = _make_sch_data([_make_symbol("R1", 0, 0)])
        assert WireDragger.collect_stub_far_endpoints(sch, "R1", {}) == {}


# ---------------------------------------------------------------------------
# Unit tests — WireDragger.move_labels_at_points
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMoveLabelsAtPoints:
    def test_label_at_mapped_point_moves(self) -> None:
        sch = _make_sch_data([_make_label("GND", 0.0, 7.62)])
        moved = WireDragger.move_labels_at_points(sch, {(0.0, 7.62): (10.0, 7.62)})
        assert moved == 1
        lbl = next(i for i in sch if isinstance(i, list) and i and i[0] == Symbol("label"))
        at = next(s for s in lbl[1:] if isinstance(s, list) and s and s[0] == Symbol("at"))
        assert abs(at[1] - 10.0) < EPS and abs(at[2] - 7.62) < EPS

    def test_label_not_at_mapped_point_untouched(self) -> None:
        sch = _make_sch_data([_make_label("GND", 50.0, 50.0)])
        moved = WireDragger.move_labels_at_points(sch, {(0.0, 7.62): (10.0, 7.62)})
        assert moved == 0

    def test_empty_map_moves_nothing(self) -> None:
        sch = _make_sch_data([_make_label("GND", 0.0, 7.62)])
        assert WireDragger.move_labels_at_points(sch, {}) == 0


# ---------------------------------------------------------------------------
# Integration tests — the move handler end-to-end on real .kicad_sch files
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestMoveStubLabelsIntegration:
    def _handler(self) -> Any:
        from handlers.schematic_component._placement import handle_move_schematic_component

        return handle_move_schematic_component

    def _make_schematic(self) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "test.kicad_sch"
        shutil.copy(TEMPLATE_PATH, tmp)
        return tmp

    def _insert(self, path: Path, sexp_text: str) -> None:
        content = path.read_text(encoding="utf-8")
        idx = content.rfind(")")
        path.write_text(content[:idx] + "\n" + sexp_text + "\n)", encoding="utf-8")

    def _add_resistor(self, path: Path, ref: str, x: float, y: float) -> None:
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

    def _add_label(self, path: Path, name: str, x: float, y: float) -> None:
        self._insert(
            path,
            f'  (label "{name}" (at {x} {y} 0) '
            f'(effects (font (size 1.27 1.27)) (justify left bottom)) (uuid "{uuid.uuid4()}"))',
        )

    def _parse_wires(self, path: Path) -> Any:
        data = sexpdata.loads(path.read_text(encoding="utf-8"))
        wires = []
        for item in data:
            if not (isinstance(item, list) and item and item[0] == Symbol("wire")):
                continue
            pts = next(
                (s for s in item[1:] if isinstance(s, list) and s and s[0] == Symbol("pts")), None
            )
            if pts is None:
                continue
            xys = [
                p for p in pts[1:] if isinstance(p, list) and len(p) >= 3 and p[0] == Symbol("xy")
            ]
            if len(xys) >= 2:
                wires.append(
                    ((float(xys[0][1]), float(xys[0][2])), (float(xys[-1][1]), float(xys[-1][2])))
                )
        return wires

    def _parse_labels(self, path: Path) -> Any:
        data = sexpdata.loads(path.read_text(encoding="utf-8"))
        out = []
        for item in data:
            if not (isinstance(item, list) and item and item[0] == Symbol("label")):
                continue
            at = next(
                (s for s in item[1:] if isinstance(s, list) and s and s[0] == Symbol("at")), None
            )
            if at is not None:
                out.append((str(item[1]), (float(at[1]), float(at[2]))))
        return out

    def test_connect_to_net_stub_and_label_move_rigidly(self) -> None:
        """The GD32 E2E repro: a pin-stub + label must translate with the part,
        never leave a stretched diagonal wire or an orphaned label behind."""
        sch = self._make_schematic()
        # R1 at (100, 100): pin1 world = (100, 96.19). Stub up to (100, 93.65) + label.
        self._add_resistor(sch, "R1", 100, 100)
        self._add_wire(sch, 100, 96.19, 100, 93.65)
        self._add_label(sch, "GND", 100, 93.65)

        result = self._handler()(
            MagicMock(),
            {
                "schematicPath": str(sch),
                "reference": "R1",
                "position": {"x": 130, "y": 80},  # delta (30, -20)
                "snapToGrid": False,
            },
        )
        assert result["success"], result.get("message")
        assert result["labelsMoved"] == 1

        # Stub translated rigidly: (130, 76.19)-(130, 73.65), 2.54 mm, axis-aligned.
        wires = self._parse_wires(sch)
        assert len(wires) == 1
        (sx, sy), (ex, ey) = wires[0]
        assert math.dist((sx, sy), (ex, ey)) == pytest.approx(2.54, abs=1e-3)
        # No long diagonal wire anywhere.
        for a, b in wires:
            diagonal = abs(a[0] - b[0]) > 1e-3 and abs(a[1] - b[1]) > 1e-3
            assert not diagonal, f"stretched diagonal wire {a}->{b}"
        ends = {(round(sx, 2), round(sy, 2)), (round(ex, 2), round(ey, 2))}
        assert (130.0, 76.19) in ends and (130.0, 73.65) in ends

        # Label followed the far endpoint; nothing left at the old (100, 93.65).
        labels = self._parse_labels(sch)
        assert ("GND", (130.0, 73.65)) in [(n, (round(x, 2), round(y, 2))) for n, (x, y) in labels]
        assert all(
            not (abs(x - 100) < 0.5 and abs(y - 93.65) < 0.5) for _n, (x, y) in labels
        ), "orphaned label left at the old stub coordinate"

    def test_shared_far_endpoint_stretches_but_moved_end_label_moves(self) -> None:
        """A wire genuinely anchored at its far end keeps stretch behavior, yet a
        label sitting on the MOVED (pin) endpoint must still travel with the part."""
        sch = self._make_schematic()
        # R1 at (100, 100): pin1 world = (100, 96.19).
        self._add_resistor(sch, "R1", 100, 100)
        # Wire A from the pin to a shared junction (120, 96.19); Wire B anchors it.
        self._add_wire(sch, 100, 96.19, 120, 96.19)
        self._add_wire(sch, 120, 96.19, 120, 80)
        # A label sitting right on the moved pin endpoint.
        self._add_label(sch, "SIG", 100, 96.19)

        result = self._handler()(
            MagicMock(),
            {
                "schematicPath": str(sch),
                "reference": "R1",
                "position": {"x": 110, "y": 100},  # delta (10, 0)
                "snapToGrid": False,
            },
        )
        assert result["success"], result.get("message")
        assert result["labelsMoved"] == 1

        wires = self._parse_wires(sch)
        # Wire A's far endpoint stayed anchored at (120, 96.19) (stretch).
        anchored = [
            (a, b)
            for a, b in wires
            if any(abs(p[0] - 120) < 0.05 and abs(p[1] - 96.19) < 0.05 for p in (a, b))
        ]
        assert anchored, f"expected the anchored far endpoint (120, 96.19) to persist: {wires}"

        # The label on the moved pin endpoint travelled to the new pin position.
        labels = self._parse_labels(sch)
        rounded = [(n, (round(x, 2), round(y, 2))) for n, (x, y) in labels]
        assert ("SIG", (110.0, 96.19)) in rounded, rounded

    def test_free_stub_wire_moves_even_without_label(self) -> None:
        """A bare stub (free far end, no label) still translates rigidly."""
        sch = self._make_schematic()
        self._add_resistor(sch, "R1", 100, 100)
        self._add_wire(sch, 100, 96.19, 100, 93.65)  # free stub, no label

        result = self._handler()(
            MagicMock(),
            {
                "schematicPath": str(sch),
                "reference": "R1",
                "position": {"x": 110, "y": 100},
                "snapToGrid": False,
            },
        )
        assert result["success"], result.get("message")
        assert result["labelsMoved"] == 0
        wires = self._parse_wires(sch)
        assert len(wires) == 1
        (sx, sy), (ex, ey) = wires[0]
        assert math.dist((sx, sy), (ex, ey)) == pytest.approx(2.54, abs=1e-3)
        ends = {(round(sx, 2), round(sy, 2)), (round(ex, 2), round(ey, 2))}
        assert (110.0, 96.19) in ends and (110.0, 93.65) in ends
