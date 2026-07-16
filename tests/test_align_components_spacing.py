"""align_components must honour spacing / alignmentType / referenceComponent
(GD32 E2E finding B3).

The bug: the TS tool sent ``alignmentType`` / ``spacing`` / ``referenceComponent``
but the Python read ``alignment`` / ``distribution`` / ``spacing``.  So the
alignment type and the anchor were dropped and — because ``distribution`` was
never "spacing" — the spacing argument was dead.  align happily stacked parts at
identical coordinates (exact-overlap courtyard collisions) while the response
admitted ``distribution: "none"``.

Fix (decision 5):
  * accept ``alignmentType`` (keep ``alignment`` as a legacy alias);
  * a supplied ``spacing`` implies ``distribution == "spacing"``;
  * ``referenceComponent`` is the anchor — the aligned axis is fixed to its
    coordinate and the spacing sequence starts from it (the anchor stays put);
  * unknown ``alignmentType`` is validation-refused (the removed "grid").

Uses the conftest pcbnew stub with a real VECTOR2I shim so final positions can be
read back.  align_components is SWIG-only (no IPC fast-path).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

NM = 1_000_000  # nm per mm


class FakeFP:
    """A minimal footprint tracking position/orientation through SetPosition."""

    def __init__(self, ref: str, x_mm: float, y_mm: float, rot: float = 0.0):
        self._ref = ref
        self._pos = SimpleNamespace(x=int(round(x_mm * NM)), y=int(round(y_mm * NM)))
        self._rot = float(rot)

    def GetReference(self):
        return self._ref

    def GetPosition(self):
        return self._pos

    def SetPosition(self, vec):
        self._pos = SimpleNamespace(x=int(vec.x), y=int(vec.y))

    def GetOrientation(self):
        return SimpleNamespace(AsDegrees=lambda: self._rot)


def _pos_mm(fp: FakeFP):
    p = fp.GetPosition()
    return (round(p.x / NM, 4), round(p.y / NM, 4))


@pytest.fixture
def cmds_factory(monkeypatch):
    """Return a builder for (ComponentCommands, board) with the given footprints.

    pcbnew.VECTOR2I is shimmed to a real namespace so SetPosition carries actual
    coordinates instead of a MagicMock.
    """
    import pcbnew  # conftest stub (MagicMock)

    monkeypatch.setattr(pcbnew, "VECTOR2I", lambda x, y: SimpleNamespace(x=int(x), y=int(y)))

    from commands.component import ComponentCommands

    def _make(fps):
        lookup = {fp.GetReference(): fp for fp in fps}
        board = MagicMock()
        board.FindFootprintByReference.side_effect = lambda r: lookup.get(r)
        cmds = ComponentCommands.__new__(ComponentCommands)
        cmds.board = board
        return cmds, board

    return _make


# ---------------------------------------------------------------------------
# spacing honoured -> no overlap (the exact B3 repro)
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_horizontal_spacing_prevents_overlap(cmds_factory):
    fps = [
        FakeFP("C1", 30, 27.8),
        FakeFP("C11", 35.5, 20),
        FakeFP("C4", 35.5, 25),
        FakeFP("C12", 44.5, 10),
        FakeFP("C5", 44.5, 15),
    ]
    cmds, _ = cmds_factory(fps)
    out = cmds.align_components(
        {
            "references": ["C1", "C11", "C12", "C4", "C5"],
            "alignmentType": "horizontal",
            "spacing": 3,
            "referenceComponent": "C1",
        }
    )

    assert out["success"] is True
    assert out["alignmentType"] == "horizontal"
    # supplied spacing implies distribution "spacing" (was "none" -> dead).
    assert out["distribution"] == "spacing"
    # All aligned onto the anchor's (C1) Y line.
    ys = {_pos_mm(fp)[1] for fp in fps}
    assert ys == {27.8}
    # X coords 3 mm apart, anchored at C1 (30) -> no two parts overlap.
    xs = sorted(_pos_mm(fp)[0] for fp in fps)
    assert xs == [30.0, 33.0, 36.0, 39.0, 42.0]
    assert len(set(xs)) == len(xs)


@pytest.mark.unit
def test_spacing_without_reference_component(cmds_factory):
    """spacing alone (no anchor) still spaces parts apart from the leftmost."""
    fps = [FakeFP("R1", 10, 5), FakeFP("R2", 10, 8), FakeFP("R3", 10, 2)]
    cmds, _ = cmds_factory(fps)
    out = cmds.align_components(
        {"references": ["R1", "R2", "R3"], "alignmentType": "horizontal", "spacing": 2}
    )

    assert out["success"] is True
    assert out["distribution"] == "spacing"
    xs = sorted(_pos_mm(fp)[0] for fp in fps)
    assert xs == [10.0, 12.0, 14.0]
    assert len(set(xs)) == 3


@pytest.mark.unit
def test_no_spacing_aligns_only(cmds_factory):
    """No spacing -> distribution 'none', parts share a line but keep their X."""
    fps = [FakeFP("R1", 10, 5), FakeFP("R2", 20, 9)]
    cmds, _ = cmds_factory(fps)
    out = cmds.align_components({"references": ["R1", "R2"], "alignmentType": "horizontal"})

    assert out["distribution"] == "none"
    ys = {_pos_mm(fp)[1] for fp in fps}
    assert len(ys) == 1
    xs = sorted(_pos_mm(fp)[0] for fp in fps)
    assert xs == [10.0, 20.0]


# ---------------------------------------------------------------------------
# referenceComponent as anchor
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_reference_component_fixes_axis(cmds_factory):
    """The shared Y line is the anchor's Y, not the average."""
    fps = [FakeFP("A1", 0, 100), FakeFP("A2", 5, 10), FakeFP("A3", 10, 20)]
    cmds, _ = cmds_factory(fps)
    out = cmds.align_components(
        {
            "references": ["A1", "A2", "A3"],
            "alignmentType": "horizontal",
            "referenceComponent": "A1",
        }
    )

    assert out["success"] is True
    assert out["referenceComponent"] == "A1"
    ys = {_pos_mm(fp)[1] for fp in fps}
    assert ys == {100.0}  # anchor's Y, not the ~43 average


@pytest.mark.unit
def test_anchor_stays_fixed_under_spacing(cmds_factory):
    fps = [FakeFP("M2", 5, 0), FakeFP("M1", 20, 0), FakeFP("M3", 35, 0)]
    cmds, _ = cmds_factory(fps)
    cmds.align_components(
        {
            "references": ["M1", "M2", "M3"],
            "alignmentType": "horizontal",
            "spacing": 10,
            "referenceComponent": "M1",
        }
    )

    m1 = next(f for f in fps if f.GetReference() == "M1")
    assert _pos_mm(m1)[0] == 20.0  # anchor unchanged
    xs = sorted(_pos_mm(f)[0] for f in fps)
    assert xs == [10.0, 20.0, 30.0]


@pytest.mark.unit
def test_vertical_spacing(cmds_factory):
    fps = [FakeFP("R1", 0, 5), FakeFP("R2", 8, 20), FakeFP("R3", 3, 35)]
    cmds, _ = cmds_factory(fps)
    cmds.align_components(
        {
            "references": ["R1", "R2", "R3"],
            "alignmentType": "vertical",
            "spacing": 4,
            "referenceComponent": "R1",
        }
    )

    xs = {_pos_mm(f)[0] for f in fps}
    assert xs == {0.0}  # anchor's X line
    ys = sorted(_pos_mm(f)[1] for f in fps)
    assert ys == [5.0, 9.0, 13.0]


# ---------------------------------------------------------------------------
# validation / legacy alias
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_unknown_alignment_type_refused(cmds_factory):
    fps = [FakeFP("R1", 1, 1), FakeFP("R2", 2, 2)]
    cmds, _ = cmds_factory(fps)
    out = cmds.align_components({"references": ["R1", "R2"], "alignmentType": "grid"})

    assert out["success"] is False
    assert out["errorCode"] == "VALIDATION"
    # Nothing moved.
    assert _pos_mm(fps[0]) == (1.0, 1.0)
    assert _pos_mm(fps[1]) == (2.0, 2.0)


@pytest.mark.unit
def test_reference_component_not_found_refused(cmds_factory):
    fps = [FakeFP("R1", 1, 1), FakeFP("R2", 2, 2)]
    cmds, _ = cmds_factory(fps)
    out = cmds.align_components(
        {
            "references": ["R1", "R2"],
            "alignmentType": "horizontal",
            "referenceComponent": "Z9",
        }
    )

    assert out["success"] is False
    assert out["errorCode"] == "VALIDATION"


@pytest.mark.unit
def test_legacy_alignment_alias(cmds_factory):
    fps = [FakeFP("R1", 10, 5), FakeFP("R2", 20, 9)]
    cmds, _ = cmds_factory(fps)
    out = cmds.align_components(
        {"references": ["R1", "R2"], "alignment": "horizontal"}  # legacy key
    )

    assert out["success"] is True
    assert out["alignmentType"] == "horizontal"
