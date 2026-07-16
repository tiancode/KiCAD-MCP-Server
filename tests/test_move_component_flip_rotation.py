"""move_component: a simultaneous layer-flip + rotation must apply the
REQUESTED rotation and report the ACTUAL applied values (GD32 E2E finding B2).

The bug: move_component called ``SetOrientation()`` and THEN ``Flip()``.  KiCad's
Flip() mirrors the current orientation, so the just-set angle was silently
rewritten — a requested 0° landed as 180° on the far side — and the response
echoed the REQUESTED value (0°), not what actually hit the board.  A caller
trusting the response saw 0° while the .kicad_pcb held 180°.

Fix: flip FIRST, then SetOrientation (so the requested angle is the last word),
and build the response from read-backs (GetOrientation / GetLayerName /
GetPosition), never the requested values.

This exercises the REAL pcbnew Flip()/SetOrientation() math — a MagicMock can't
reproduce the mirroring — so it loads the real bindings for the duration of the
test (conftest globally stubs pcbnew).  SWIG-only: the IPC path has no layer arg.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

NM = 1_000_000  # nm per mm


@pytest.fixture
def real_pcbnew(monkeypatch):
    """Point commands.component._placement at the REAL pcbnew for this test.

    conftest replaces sys.modules['pcbnew'] with a MagicMock so the heavy .so
    never loads; here we load the real extension and monkeypatch the
    _placement module's module-level ``pcbnew`` name at it, leaving the global
    stub in place for every other module.  monkeypatch restores it afterwards.
    """
    stub = sys.modules.get("pcbnew")
    if "pcbnew" in sys.modules:
        del sys.modules["pcbnew"]
    try:
        real = importlib.import_module("pcbnew")
    except Exception:  # pragma: no cover - environment without real pcbnew
        if stub is not None:
            sys.modules["pcbnew"] = stub
        pytest.skip("real pcbnew not importable")
    # Keep the global stub for everyone else; only _placement needs the real one.
    sys.modules["pcbnew"] = stub if stub is not None else real
    import commands.component._placement as pl

    monkeypatch.setattr(pl, "pcbnew", real)
    return real


def _cmds_with_board(real, board):
    from commands.component import ComponentCommands

    cmds = ComponentCommands.__new__(ComponentCommands)
    cmds.board = board
    return cmds


def _make_footprint(real, ref="U3", pos_mm=(10, 10), rotation=0, on_back=False):
    board = real.BOARD()
    fp = real.FOOTPRINT(board)
    fp.SetReference(ref)
    board.Add(fp)
    fp.SetPosition(real.VECTOR2I(int(pos_mm[0] * NM), int(pos_mm[1] * NM)))
    fp.SetOrientation(real.EDA_ANGLE(rotation, real.DEGREES_T))
    if on_back:
        fp.Flip(fp.GetPosition(), False)
    return board, fp


@pytest.mark.unit
def test_flip_back_to_front_with_rotation_zero(real_pcbnew):
    """The exact B2 repro: U3 on B.Cu (180°) → move to F.Cu, rotation 0."""
    real = real_pcbnew
    board, fp = _make_footprint(real, rotation=180, on_back=True)
    assert board.GetLayerName(fp.GetLayer()) == "B.Cu"

    cmds = _cmds_with_board(real, board)
    out = cmds.move_component(
        {
            "reference": "U3",
            "position": {"x": 19, "y": 13, "unit": "mm"},
            "layer": "F.Cu",
            "rotation": 0,
        }
    )

    assert out["success"] is True
    # On-disk truth: the requested 0° stuck and the part is on F.Cu.
    assert board.GetLayerName(fp.GetLayer()) == "F.Cu"
    assert fp.GetOrientation().AsDegrees() == 0.0
    # Response is a read-back of the ACTUAL state, not an echo of the request.
    assert out["component"]["rotation"] == 0.0
    assert out["component"]["layer"] == "F.Cu"
    assert out["component"]["position"]["x"] == pytest.approx(19.0)
    assert out["component"]["position"]["y"] == pytest.approx(13.0)


@pytest.mark.unit
def test_flip_front_to_back_with_rotation(real_pcbnew):
    """F.Cu (0°) → move to B.Cu with rotation 90 lands at exactly 90°."""
    real = real_pcbnew
    board, fp = _make_footprint(real, rotation=0, on_back=False)
    assert board.GetLayerName(fp.GetLayer()) == "F.Cu"

    cmds = _cmds_with_board(real, board)
    out = cmds.move_component(
        {
            "reference": "U3",
            "position": {"x": 20, "y": 20, "unit": "mm"},
            "layer": "B.Cu",
            "rotation": 90,
        }
    )

    assert out["success"] is True
    assert board.GetLayerName(fp.GetLayer()) == "B.Cu"
    assert fp.GetOrientation().AsDegrees() == 90.0
    assert out["component"]["rotation"] == 90.0
    assert out["component"]["layer"] == "B.Cu"


@pytest.mark.unit
def test_rotation_only_no_flip(real_pcbnew):
    """No layer change: rotation applies and the response reads it back."""
    real = real_pcbnew
    board, fp = _make_footprint(real, rotation=0, on_back=False)

    cmds = _cmds_with_board(real, board)
    out = cmds.move_component(
        {"reference": "U3", "position": {"x": 5, "y": 5, "unit": "mm"}, "rotation": 45}
    )

    assert out["success"] is True
    assert board.GetLayerName(fp.GetLayer()) == "F.Cu"
    assert fp.GetOrientation().AsDegrees() == 45.0
    assert out["component"]["rotation"] == 45.0


@pytest.mark.unit
def test_flip_without_rotation_reports_flipped_orientation(real_pcbnew):
    """No rotation arg: the flip changes the orientation and the response must
    report the flip-induced value (read-back), never a stale/echoed one."""
    real = real_pcbnew
    board, fp = _make_footprint(real, rotation=30, on_back=False)

    cmds = _cmds_with_board(real, board)
    out = cmds.move_component(
        {"reference": "U3", "position": {"x": 8, "y": 8, "unit": "mm"}, "layer": "B.Cu"}
    )

    assert out["success"] is True
    assert board.GetLayerName(fp.GetLayer()) == "B.Cu"
    # Whatever the flip produced on disk is exactly what the response reports.
    assert out["component"]["rotation"] == fp.GetOrientation().AsDegrees()
    assert out["component"]["layer"] == "B.Cu"
