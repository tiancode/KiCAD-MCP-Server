"""move_component must be board-outline aware (GD32 E2E finding P11).

Moving a footprint to a coordinate far off the board used to succeed silently
(e.g. D2 to (500, 500) on a 90×60 board — no signal it landed off-board).  The
fix mirrors the schematic-side page guard (POSITION_OFF_SHEET):

  * a merely-off-board (but plausible) target still moves, with an
    ``offBoardWarning`` naming the Edge.Cuts bbox;
  * an absurd target (>10× a board dimension) is rejected with errorCode
    POSITION_OFF_BOARD and the footprint is NOT moved;
  * no outline → no judgement, no warning.

Covers the SWIG path (commands.component._placement.move_component) and the IPC
fast-path handler (handlers.ipc_fastpath._components.handle_move_component).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

NM = 1_000_000  # nm per mm


# ---------------------------------------------------------------------------
# SWIG path
# ---------------------------------------------------------------------------
def _swig_cmds(bbox_nm):
    """A ComponentCommands with a mock board whose outline bbox is bbox_nm
    ``(left, top, right, bottom)`` in nm, or None for 'no outline'."""
    from commands.component import ComponentCommands

    cmds = ComponentCommands.__new__(ComponentCommands)

    board = MagicMock()
    module = MagicMock()
    module.GetOrientation.return_value.AsDegrees.return_value = 0.0
    board.FindFootprintByReference.return_value = module
    board.GetLayerName.return_value = "F.Cu"

    if bbox_nm is None:
        # Degenerate box → treated as "no outline".
        bb = MagicMock()
        bb.GetLeft.return_value = 0
        bb.GetTop.return_value = 0
        bb.GetRight.return_value = 0
        bb.GetBottom.return_value = 0
    else:
        left, top, right, bottom = bbox_nm
        bb = MagicMock()
        bb.GetLeft.return_value = left
        bb.GetTop.return_value = top
        bb.GetRight.return_value = right
        bb.GetBottom.return_value = bottom
    board.GetBoardEdgesBoundingBox.return_value = bb

    cmds.board = board
    return cmds, board, module


# A 90×60 mm board with its outline at the origin.
_BOARD_90x60 = (0, 0, 90 * NM, 60 * NM)


def _move(cmds, x, y):
    return cmds.move_component(
        {"reference": "D2", "position": {"x": x, "y": y, "unit": "mm"}}
    )


@pytest.mark.unit
def test_swig_off_board_move_succeeds_with_warning():
    cmds, board, module = _swig_cmds(_BOARD_90x60)
    out = _move(cmds, 500, 500)  # outside the board, within 10×
    assert out["success"] is True
    assert "offBoardWarning" in out
    assert out["boardOutline"] == {"x1": 0.0, "y1": 0.0, "x2": 90.0, "y2": 60.0, "unit": "mm"}
    # The move DID apply.
    module.SetPosition.assert_called_once()


@pytest.mark.unit
def test_swig_absurd_move_rejected_and_not_applied():
    cmds, board, module = _swig_cmds(_BOARD_90x60)
    out = _move(cmds, 5000, 5000)  # >10× the 90 mm width
    assert out["success"] is False
    assert out["errorCode"] == "POSITION_OFF_BOARD"
    assert out["boardOutline"]["x2"] == 90.0
    # The footprint must NOT have moved.
    module.SetPosition.assert_not_called()


@pytest.mark.unit
def test_swig_on_board_move_has_no_warning():
    cmds, board, module = _swig_cmds(_BOARD_90x60)
    out = _move(cmds, 45, 30)  # centre of the board
    assert out["success"] is True
    assert "offBoardWarning" not in out
    assert out["boardOutline"]["x2"] == 90.0
    module.SetPosition.assert_called_once()


@pytest.mark.unit
def test_swig_no_outline_no_warning():
    cmds, board, module = _swig_cmds(None)
    out = _move(cmds, 500, 500)
    assert out["success"] is True
    assert "offBoardWarning" not in out
    assert "boardOutline" not in out
    module.SetPosition.assert_called_once()


@pytest.mark.unit
def test_swig_off_board_only_in_one_axis():
    """Inside x, outside y still counts as off-board."""
    cmds, board, module = _swig_cmds(_BOARD_90x60)
    out = _move(cmds, 45, 200)  # x inside, y past the 60 mm bottom
    assert out["success"] is True
    assert "offBoardWarning" in out


# ---------------------------------------------------------------------------
# IPC fast-path
# ---------------------------------------------------------------------------
def _ipc_iface(outline):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    api = MagicMock()
    api.get_outline_bbox.return_value = outline
    api.move_component.return_value = True
    iface.ipc_board_api = api
    return iface, api


_OUTLINE_90x60 = {"x1": 0.0, "y1": 0.0, "x2": 90.0, "y2": 60.0, "unit": "mm"}


@pytest.mark.unit
def test_ipc_off_board_move_warns_and_moves():
    from handlers.ipc_fastpath._components import handle_move_component

    iface, api = _ipc_iface(_OUTLINE_90x60)
    out = handle_move_component(
        iface, {"reference": "D2", "position": {"x": 500, "y": 500, "unit": "mm"}}
    )
    assert out["success"] is True
    assert "offBoardWarning" in out
    assert out["boardOutline"] == _OUTLINE_90x60
    api.move_component.assert_called_once()


@pytest.mark.unit
def test_ipc_absurd_move_rejected_and_not_moved():
    from handlers.ipc_fastpath._components import handle_move_component

    iface, api = _ipc_iface(_OUTLINE_90x60)
    out = handle_move_component(
        iface, {"reference": "D2", "position": {"x": 5000, "y": 5000, "unit": "mm"}}
    )
    assert out["success"] is False
    assert out["errorCode"] == "POSITION_OFF_BOARD"
    api.move_component.assert_not_called()


@pytest.mark.unit
def test_ipc_on_board_move_no_warning():
    from handlers.ipc_fastpath._components import handle_move_component

    iface, api = _ipc_iface(_OUTLINE_90x60)
    out = handle_move_component(
        iface, {"reference": "D2", "position": {"x": 45, "y": 30, "unit": "mm"}}
    )
    assert out["success"] is True
    assert "offBoardWarning" not in out
    api.move_component.assert_called_once()


@pytest.mark.unit
def test_ipc_no_outline_no_warning():
    from handlers.ipc_fastpath._components import handle_move_component

    iface, api = _ipc_iface(None)
    out = handle_move_component(
        iface, {"reference": "D2", "position": {"x": 500, "y": 500, "unit": "mm"}}
    )
    assert out["success"] is True
    assert "offBoardWarning" not in out
    assert "boardOutline" not in out
    api.move_component.assert_called_once()
