"""move_component must be board-outline aware (GD32 E2E findings P11 + B10).

Moving a footprint to a coordinate off the board used to succeed silently.  The
guard mirrors the schematic-side page guard (POSITION_OFF_SHEET):

  * an absurd target (>10× a board dimension) is rejected with errorCode
    POSITION_OFF_BOARD and the footprint is NOT moved (units-error case);
  * a merely-off-board (but plausible) target is ALSO refused by default with
    errorCode POSITION_OFF_BOARD and NOT moved (B10) — silently parking a part
    off the board is almost never intended;
  * ``allowOffBoard: true`` reinstates the apply-with-warning behaviour for the
    deliberate case: the move applies and the response carries an
    ``offBoardWarning`` naming the Edge.Cuts bbox;
  * no outline → no judgement, move applies with no warning.

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
    # A numeric position so the response read-back (GetPosition → mm) is clean.
    module.GetPosition.return_value = MagicMock(x=0, y=0)
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


def _move(cmds, x, y, **extra):
    return cmds.move_component(
        {"reference": "D2", "position": {"x": x, "y": y, "unit": "mm"}, **extra}
    )


@pytest.mark.unit
def test_swig_off_board_refused_by_default():
    """B10: a merely-off-board target is refused and the part is NOT moved."""
    cmds, board, module = _swig_cmds(_BOARD_90x60)
    out = _move(cmds, 500, 500)  # outside the board, within 10×
    assert out["success"] is False
    assert out["errorCode"] == "POSITION_OFF_BOARD"
    assert out["boardOutline"] == {"x1": 0.0, "y1": 0.0, "x2": 90.0, "y2": 60.0, "unit": "mm"}
    module.SetPosition.assert_not_called()


@pytest.mark.unit
def test_swig_off_board_applies_with_allow_off_board():
    """B10: allowOffBoard:true reinstates apply-with-warning."""
    cmds, board, module = _swig_cmds(_BOARD_90x60)
    out = _move(cmds, 500, 500, allowOffBoard=True)
    assert out["success"] is True
    assert "offBoardWarning" in out
    assert out["boardOutline"] == {"x1": 0.0, "y1": 0.0, "x2": 90.0, "y2": 60.0, "unit": "mm"}
    module.SetPosition.assert_called_once()


@pytest.mark.unit
def test_swig_absurd_move_rejected_and_not_applied():
    cmds, board, module = _swig_cmds(_BOARD_90x60)
    # >10× the 90 mm width; even allowOffBoard cannot rescue a units error.
    out = _move(cmds, 5000, 5000, allowOffBoard=True)
    assert out["success"] is False
    assert out["errorCode"] == "POSITION_OFF_BOARD"
    assert out["boardOutline"]["x2"] == 90.0
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
def test_swig_off_board_only_in_one_axis_refused():
    """Inside x, outside y still counts as off-board — refused by default."""
    cmds, board, module = _swig_cmds(_BOARD_90x60)
    out = _move(cmds, 45, 200)  # x inside, y past the 60 mm bottom
    assert out["success"] is False
    assert out["errorCode"] == "POSITION_OFF_BOARD"
    module.SetPosition.assert_not_called()


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
def test_ipc_off_board_refused_by_default():
    from handlers.ipc_fastpath._components import handle_move_component

    iface, api = _ipc_iface(_OUTLINE_90x60)
    out = handle_move_component(
        iface, {"reference": "D2", "position": {"x": 500, "y": 500, "unit": "mm"}}
    )
    assert out["success"] is False
    assert out["errorCode"] == "POSITION_OFF_BOARD"
    assert out["boardOutline"] == _OUTLINE_90x60
    api.move_component.assert_not_called()


@pytest.mark.unit
def test_ipc_off_board_applies_with_allow_off_board():
    from handlers.ipc_fastpath._components import handle_move_component

    iface, api = _ipc_iface(_OUTLINE_90x60)
    out = handle_move_component(
        iface,
        {
            "reference": "D2",
            "position": {"x": 500, "y": 500, "unit": "mm"},
            "allowOffBoard": True,
        },
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
        iface,
        {
            "reference": "D2",
            "position": {"x": 5000, "y": 5000, "unit": "mm"},
            "allowOffBoard": True,
        },
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
