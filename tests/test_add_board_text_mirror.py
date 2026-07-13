"""add_board_text must auto-mirror back-layer text (GD32 E2E finding P13).

Silkscreen (or any graphic) text placed on a B.* layer is read through the
board, so KiCad expects it mirrored — un-mirrored back text trips DRC's
``nonmirrored_text_on_back_layer``.  add_board_text used to place it
un-mirrored, so every back-layer label failed DRC.

The fix auto-mirrors any B.* layer by default and accepts an explicit
``mirror`` boolean to override, reporting the applied state.  Covered on the
SWIG path (commands.board.outline.add_text, exercised against a real pcbnew
board) and the IPC path (backend add_text + fast-path handler).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


# ---------------------------------------------------------------------------
# SWIG path — pcbnew is stubbed globally (see tests/conftest.py); assert on the
# response + the SetMirrored call the implementation makes.  Real end-to-end
# mirror behaviour against pcbnew 10.0.4 was verified manually.
# ---------------------------------------------------------------------------
@pytest.fixture
def swig_cmds():
    import pcbnew

    from commands.board.outline import BoardOutlineCommands

    pcbnew.reset_mock()
    pcbnew.DEGREES_T = "deg"
    board = MagicMock(name="board")
    board.GetLayerID.return_value = 7  # a valid (>=0) layer id
    return BoardOutlineCommands(board=board), pcbnew


def _add(cmds, layer, **extra):
    params = {
        "text": "REV A",
        "position": {"x": 10, "y": 10, "unit": "mm"},
        "layer": layer,
        "size": 1.0,
    }
    params.update(extra)
    return cmds.add_text(params)


@pytest.mark.unit
def test_swig_back_layer_auto_mirrors(swig_cmds):
    cmds, pcbnew = swig_cmds
    out = _add(cmds, "B.Silkscreen")
    assert out["success"] is True
    assert out["text"]["mirror"] is True
    assert out["text"]["mirrorAuto"] is True
    pcbnew.PCB_TEXT.return_value.SetMirrored.assert_called_once_with(True)


@pytest.mark.unit
def test_swig_front_layer_not_mirrored(swig_cmds):
    cmds, pcbnew = swig_cmds
    out = _add(cmds, "F.Silkscreen")
    assert out["text"]["mirror"] is False
    assert out["text"]["mirrorAuto"] is False
    pcbnew.PCB_TEXT.return_value.SetMirrored.assert_called_once_with(False)


@pytest.mark.unit
def test_swig_explicit_mirror_false_overrides_back_layer(swig_cmds):
    cmds, pcbnew = swig_cmds
    out = _add(cmds, "B.Silkscreen", mirror=False)
    assert out["text"]["mirror"] is False
    # Not auto — the user forced it.
    assert out["text"]["mirrorAuto"] is False
    pcbnew.PCB_TEXT.return_value.SetMirrored.assert_called_once_with(False)


@pytest.mark.unit
def test_swig_explicit_mirror_true_on_front(swig_cmds):
    cmds, pcbnew = swig_cmds
    out = _add(cmds, "F.Silkscreen", mirror=True)
    assert out["text"]["mirror"] is True
    assert out["text"]["mirrorAuto"] is False
    pcbnew.PCB_TEXT.return_value.SetMirrored.assert_called_once_with(True)


# ---------------------------------------------------------------------------
# IPC path — backend BoardText.attributes.mirrored
# ---------------------------------------------------------------------------
def _ipc_api():
    from kicad_api.ipc_backend._board_core import IPCBoardAPI

    api = IPCBoardAPI.__new__(IPCBoardAPI)
    api._get_board = MagicMock(return_value=MagicMock())
    api._notify = MagicMock()
    captured = {}

    def _cap(board, item, desc):
        captured["item"] = item

    api._apply_create = _cap
    return api, captured


@pytest.mark.unit
def test_ipc_backend_auto_mirrors_back_layer(real_kipy):
    api, captured = _ipc_api()
    ok = api.add_text(text="REV A", x=1.0, y=2.0, layer="B.Silkscreen")
    assert ok is True
    assert captured["item"].attributes.mirrored is True


@pytest.mark.unit
def test_ipc_backend_front_layer_not_mirrored(real_kipy):
    api, captured = _ipc_api()
    api.add_text(text="REV A", x=1.0, y=2.0, layer="F.Silkscreen")
    assert captured["item"].attributes.mirrored is False


@pytest.mark.unit
def test_ipc_backend_explicit_override(real_kipy):
    api, captured = _ipc_api()
    api.add_text(text="REV A", x=1.0, y=2.0, layer="B.Silkscreen", mirror=False)
    assert captured["item"].attributes.mirrored is False


# ---------------------------------------------------------------------------
# IPC path — fast-path handler reports + forwards the mirror decision
# ---------------------------------------------------------------------------
def _ipc_iface():
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    api = MagicMock()
    api.add_text.return_value = True
    iface.ipc_board_api = api
    return iface, api


@pytest.mark.unit
def test_ipc_handler_auto_mirrors_and_reports():
    from handlers.ipc_fastpath._board import handle_add_text

    iface, api = _ipc_iface()
    out = handle_add_text(
        iface,
        {"text": "REV A", "position": {"x": 1, "y": 2, "unit": "mm"}, "layer": "B.Silkscreen", "size": 1.0},
    )
    assert out["success"] is True
    assert out["mirror"] is True
    assert out["mirrorAuto"] is True
    # The concrete mirror decision is forwarded to the backend.
    assert api.add_text.call_args.kwargs["mirror"] is True


@pytest.mark.unit
def test_ipc_handler_front_layer_not_mirrored():
    from handlers.ipc_fastpath._board import handle_add_text

    iface, api = _ipc_iface()
    out = handle_add_text(
        iface,
        {"text": "F", "position": {"x": 1, "y": 2, "unit": "mm"}, "layer": "F.Silkscreen", "size": 1.0},
    )
    assert out["mirror"] is False
    assert out["mirrorAuto"] is False
    assert api.add_text.call_args.kwargs["mirror"] is False


@pytest.mark.unit
def test_ipc_handler_explicit_override():
    from handlers.ipc_fastpath._board import handle_add_text

    iface, api = _ipc_iface()
    out = handle_add_text(
        iface,
        {
            "text": "X",
            "position": {"x": 1, "y": 2, "unit": "mm"},
            "layer": "B.Silkscreen",
            "size": 1.0,
            "mirror": False,
        },
    )
    assert out["mirror"] is False
    assert out["mirrorAuto"] is False
    assert api.add_text.call_args.kwargs["mirror"] is False
