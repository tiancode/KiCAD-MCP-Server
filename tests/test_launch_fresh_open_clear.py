"""Fresh-open clear for the EXPLICIT launch path (papercut 5, GD32 E2E).

After ``manage_kicad_ui(action=launch)`` opened the board fresh from disk,
IPC reads still reported ``staleVsDisk: true`` and the first mutation ran a
needless auto-reconcile, because only the *auto*-open self-heal ran
``_clear_swig_landed_if_disk_matches`` (finding B3) — the explicit launch
handler never did.  These tests pin the wiring:

- forward verified at handler time → synchronous clear (same safety: only
  when the disk signature still matches the recorded landed write);
- cold launch / spawn (attach completes later) → a deferred marker armed
  with the launch-time disk signature, consumed at the first point a board
  document is confirmed open over IPC (dispatcher + ensure_ipc gate);
- the marker must NOT clear when the disk moved on after the launch, when
  a different board was launched, or when KiCad already had the board open
  (its memory may predate the landed write).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _iface_with_board(tmp_path, *, landed=True):
    """KiCADInterface stand-in with a real on-disk board + matching signature."""
    from kicad_interface import KiCADInterface

    board = tmp_path / "demo.kicad_pcb"
    board.write_text("(kicad_pcb rev-A)\n", encoding="utf-8")

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    iface.ipc_backend = MagicMock()
    iface.ipc_backend.is_connected = lambda: True
    iface.ipc_board_api = MagicMock()
    iface.board = MagicMock()
    iface.board.GetFileName.return_value = str(board)
    iface.command_routes = {}
    iface._current_project_path = None
    iface._last_auto_save_status = None
    iface._ipc_writes_pending = False
    iface._swig_writes_landed = landed
    iface._ipc_change_callback_registered = False
    iface._board_disk_signature = KiCADInterface._disk_signature(str(board))
    iface._pending_fresh_open_clear = None
    iface._backend_status = lambda: {
        "backend": "ipc",
        "realtime_sync": True,
        "ipc_connected": True,
        "capabilities": {},
    }
    iface._try_enable_ipc_backend = lambda force=False: True
    return iface, board


def _launch(iface, board, monkeypatch, *, launch_result, forward=None, doc_open=False):
    """Drive handle_launch_kicad_ui with canned process/forward outcomes."""
    from handlers import ui as ui_handler

    monkeypatch.setattr(
        ui_handler, "check_and_launch_kicad", lambda path, auto_launch=True: dict(launch_result)
    )
    if forward is not None:
        monkeypatch.setattr(
            ui_handler,
            "_forward_file_open_to_running_kicad",
            lambda _iface, _path: dict(forward),
        )
    iface._ipc_has_open_board_document = lambda: doc_open
    return ui_handler.handle_launch_kicad_ui(iface, {"projectPath": str(board)})


_ALREADY_RUNNING = {
    "running": True,
    "launched": False,
    "alreadyRunning": True,
    "processes": [],
    "message": "KiCAD is already running",
}
_COLD_LAUNCH = {
    "running": True,
    "launched": True,
    "alreadyRunning": False,
    "processes": [],
    "message": "KiCAD launched",
}


# ---------------------------------------------------------------------------
# Synchronous clear: forward verified while the handler is still running
# ---------------------------------------------------------------------------
def test_verified_forward_clears_swig_landed_synchronously(monkeypatch, tmp_path):
    iface, board = _iface_with_board(tmp_path)

    out = _launch(
        iface,
        board,
        monkeypatch,
        launch_result=_ALREADY_RUNNING,
        forward={"fileOpenForwarded": True, "fileOpenMethod": "ipc_action"},
        doc_open=True,
    )

    assert out["success"] is True
    assert iface._swig_writes_landed is False
    assert iface._pending_fresh_open_clear is None


def test_already_open_board_keeps_flag(monkeypatch, tmp_path):
    """KiCad already had the board open — its memory may predate the landed
    SWIG write, so neither a clear nor a deferred marker is allowed."""
    iface, board = _iface_with_board(tmp_path)

    out = _launch(
        iface,
        board,
        monkeypatch,
        launch_result=_ALREADY_RUNNING,
        forward={"fileOpenForwarded": True, "fileOpenMethod": "already_open"},
        doc_open=True,
    )

    assert out["success"] is True
    assert iface._swig_writes_landed is True
    assert iface._pending_fresh_open_clear is None


def test_launch_of_different_board_never_clears(monkeypatch, tmp_path):
    iface, _board = _iface_with_board(tmp_path)
    other = tmp_path / "other.kicad_pcb"
    other.write_text("(kicad_pcb other)\n", encoding="utf-8")

    out = _launch(
        iface,
        other,  # projectPath != the loaded SWIG board
        monkeypatch,
        launch_result=_ALREADY_RUNNING,
        forward={"fileOpenForwarded": True, "fileOpenMethod": "spawn"},
        doc_open=True,
    )

    assert out["success"] is True
    assert iface._swig_writes_landed is True
    assert iface._pending_fresh_open_clear is None


# ---------------------------------------------------------------------------
# Deferred clear: cold launch, attach completes after the handler returns
# ---------------------------------------------------------------------------
def test_cold_launch_arms_marker_then_consume_clears(monkeypatch, tmp_path):
    iface, board = _iface_with_board(tmp_path)

    out = _launch(iface, board, monkeypatch, launch_result=_COLD_LAUNCH, doc_open=False)

    assert out["success"] is True
    assert iface._swig_writes_landed is True  # not yet — attach pending
    assert iface._pending_fresh_open_clear is not None

    # Later: the first gated call sees the board document open and consumes.
    iface._consume_pending_fresh_open_clear()

    assert iface._swig_writes_landed is False
    assert iface._pending_fresh_open_clear is None  # single-shot


def test_deferred_clear_keeps_flag_when_disk_changed_after_launch(monkeypatch, tmp_path):
    """Race safety: a SWIG write landing BETWEEN the launch-time open and the
    eventual attach means KiCad's freshly opened memory no longer equals
    disk.  The marker's launch-time signature must veto the clear even though
    the recorded landed signature matches the (new) disk content."""
    from kicad_interface import KiCADInterface

    iface, board = _iface_with_board(tmp_path)
    _launch(iface, board, monkeypatch, launch_result=_COLD_LAUNCH, doc_open=False)
    assert iface._pending_fresh_open_clear is not None

    # A second SWIG write lands: new disk content + new recorded signature.
    board.write_text("(kicad_pcb rev-B)\n", encoding="utf-8")
    iface._board_disk_signature = KiCADInterface._disk_signature(str(board))
    iface._swig_writes_landed = True

    iface._consume_pending_fresh_open_clear()

    assert iface._swig_writes_landed is True
    assert iface._pending_fresh_open_clear is None  # still single-shot


def test_expired_marker_keeps_flag(tmp_path):
    from kicad_interface import KiCADInterface

    iface, board = _iface_with_board(tmp_path)
    sig = KiCADInterface._disk_signature(str(board))
    iface._pending_fresh_open_clear = (time.monotonic() - 1.0, sig)

    iface._consume_pending_fresh_open_clear()

    assert iface._swig_writes_landed is True


def test_arm_is_noop_without_landed_writes(monkeypatch, tmp_path):
    iface, board = _iface_with_board(tmp_path, landed=False)

    out = _launch(iface, board, monkeypatch, launch_result=_COLD_LAUNCH, doc_open=False)

    assert out["success"] is True
    assert iface._pending_fresh_open_clear is None


# ---------------------------------------------------------------------------
# Consume wiring: ensure_ipc editor gate + dispatcher fast path
# ---------------------------------------------------------------------------
def test_ensure_ipc_editor_gate_consumes_marker(monkeypatch, tmp_path):
    iface, board = _iface_with_board(tmp_path)
    iface._arm_pending_fresh_open_clear()
    assert iface._pending_fresh_open_clear is not None
    iface._ipc_has_open_board_document = lambda: True

    ok, reason = iface.ensure_ipc(allow_launch=False)

    assert ok is True
    assert iface._swig_writes_landed is False
    # And the board-op gate no longer sees a cross-backend conflict.
    assert iface.require_ipc_board_op(allow_launch=False) == {}


def test_dispatcher_consumes_marker_before_conflict_and_stale_checks(monkeypatch, tmp_path):
    """End-to-end through handle_command: after an explicit launch armed the
    marker, the first IPC command must neither refuse with needs_reconcile,
    nor auto-reconcile (no revert), nor stamp staleVsDisk on reads."""
    monkeypatch.delenv("KICAD_AUTO_RECONCILE", raising=False)
    iface, board = _iface_with_board(tmp_path)
    iface._arm_pending_fresh_open_clear()
    iface._ipc_has_open_board_document = lambda: True
    fake_revert = MagicMock(return_value=True)
    iface.ipc_board_api.revert = fake_revert

    iface._ipc_get_board_info = lambda params: {"success": True, "componentCount": 42}
    read = iface.handle_command("get_board_info", {})

    assert read["success"] is True
    assert "staleVsDisk" not in read
    assert "staleHint" not in read

    iface._ipc_place_component = lambda params: {"success": True, "reference": "R1"}
    result = iface.handle_command("place_component", {"reference": "R1"})

    assert result["success"] is True
    assert "needs_reconcile" not in result
    assert "auto_reconciled" not in result
    fake_revert.assert_not_called()
    assert iface._swig_writes_landed is False
