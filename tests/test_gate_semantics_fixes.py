"""Regression tests for the gate-semantics fixes B2 / B3 (E2E phase B).

- **B2**: ``handlers.ipc_gate.require_ipc`` used to special-case only
  ``needs_pcb_editor`` and funnel every other refusal — including a
  cross-backend ``needs_reconcile`` conflict — through ``ipc_unavailable``,
  telling the user to "enable IPC" while IPC was connected.  The fix forwards
  any structured refusal (the ones WITHOUT ``_ipc_reason``) verbatim and only
  rewraps the raw-reason envelope.

- **B3**: after the auto-launch/auto-open self-heal opens the board from the
  current disk file, ``_swig_writes_landed`` must be cleared when the on-disk
  signature still matches the recorded landed-write signature (IPC == disk ==
  SWIG), but kept when the disk changed externally since.

(B11's dispatcher-level auto-heal lives in ``test_cross_backend_conflict.py``
alongside the rest of the dispatcher-gate tests.)
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _ipc_unavailable(reason: str = ""):
    base = "Board commands require the IPC backend. Enable it, then retry."
    return {"success": False, "message": f"{base} ({reason})" if reason else base}


# ---------------------------------------------------------------------------
# B2 — require_ipc forwards structured refusals, rewraps only _ipc_reason
# ---------------------------------------------------------------------------
def test_require_ipc_passes_needs_reconcile_through_intact():
    """A cross-backend conflict (needs_reconcile + direction, no _ipc_reason)
    must reach the agent verbatim — not be rewritten as 'enable IPC'."""
    from handlers.ipc_gate import require_ipc

    conflict = {
        "success": False,
        "needs_reconcile": True,
        "direction": "swig_to_ipc",
        "message": "SWIG wrote new content to disk ... call reconcile_backends.",
    }
    iface = MagicMock()
    iface.require_ipc_board_op.return_value = conflict

    out = require_ipc(iface, _ipc_unavailable)

    assert out is conflict  # forwarded verbatim, not rebuilt
    assert out["needs_reconcile"] is True
    assert out["direction"] == "swig_to_ipc"
    assert "reconcile_backends" in out["message"]
    # The misleading "require the IPC backend" envelope must NOT appear.
    assert "require the IPC backend" not in out["message"]


def test_require_ipc_passes_needs_pcb_editor_through_unchanged():
    """The editor-frame gate keeps passing through (behavior preserved)."""
    from handlers.ipc_gate import require_ipc

    gate = {
        "success": False,
        "needs_pcb_editor": True,
        "message": "This IPC board operation requires the PCB editor ...",
    }
    iface = MagicMock()
    iface.require_ipc_board_op.return_value = gate

    out = require_ipc(iface, _ipc_unavailable)

    assert out is gate
    assert out["needs_pcb_editor"] is True


def test_require_ipc_wraps_raw_reason_envelope_as_ipc_unavailable():
    """The only shape that gets rewrapped is the raw ``_ipc_reason`` envelope,
    so genuine IPC-unavailability still maps to the caller's domain message."""
    from handlers.ipc_gate import require_ipc

    iface = MagicMock()
    iface.require_ipc_board_op.return_value = {
        "success": False,
        "_ipc_reason": "socket refused",
    }

    out = require_ipc(iface, _ipc_unavailable)

    assert out["success"] is False
    assert "socket refused" in out["message"]
    assert "require the IPC backend" in out["message"]
    # Not misclassified as a reconcile conflict.
    assert "needs_reconcile" not in out
    assert "_ipc_reason" not in out


def test_require_ipc_ready_returns_empty():
    from handlers.ipc_gate import require_ipc

    iface = MagicMock()
    iface.require_ipc_board_op.return_value = {}

    assert require_ipc(iface, _ipc_unavailable) == {}


# ---------------------------------------------------------------------------
# B3 — _clear_swig_landed_if_disk_matches
# ---------------------------------------------------------------------------
def _iface_with_board(tmp_path):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    board = tmp_path / "demo.kicad_pcb"
    board.write_text("(kicad_pcb)\n", encoding="utf-8")
    iface.board = MagicMock()
    iface.board.GetFileName.return_value = str(board)
    iface._swig_writes_landed = True
    return iface, board


def test_clear_swig_landed_when_disk_matches_recorded_signature(tmp_path):
    """Fresh open from a disk state whose signature equals the recorded landed
    write → IPC == disk == SWIG → drop the flag (no false conflict)."""
    from kicad_interface import KiCADInterface

    iface, board = _iface_with_board(tmp_path)
    iface._board_disk_signature = KiCADInterface._disk_signature(str(board))

    iface._clear_swig_landed_if_disk_matches()

    assert iface._swig_writes_landed is False


def test_keep_swig_landed_when_disk_changed_externally(tmp_path):
    """If the on-disk hash no longer matches the recorded landed-write hash,
    the file moved on externally — the divergence is real, keep the flag so
    reconcile_backends still reloads the SWIG side."""
    from kicad_interface import KiCADInterface

    iface, board = _iface_with_board(tmp_path)
    real = KiCADInterface._disk_signature(str(board))
    # Same mtime slot, different content hash → treated as external edit.
    iface._board_disk_signature = (real[0], "0" * 64)

    iface._clear_swig_landed_if_disk_matches()

    assert iface._swig_writes_landed is True


def test_clear_swig_landed_noop_when_flag_unset(tmp_path):
    from kicad_interface import KiCADInterface

    iface, board = _iface_with_board(tmp_path)
    iface._swig_writes_landed = False
    iface._board_disk_signature = KiCADInterface._disk_signature(str(board))

    iface._clear_swig_landed_if_disk_matches()

    assert iface._swig_writes_landed is False


def test_keep_swig_landed_when_no_board_path():
    """No SWIG board to derive a path from → can't verify, so keep the flag."""
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.board = None
    iface._swig_writes_landed = True
    iface._board_disk_signature = (1, "abc")

    iface._clear_swig_landed_if_disk_matches()

    assert iface._swig_writes_landed is True


def test_auto_open_run_action_clears_swig_landed_when_disk_matches(monkeypatch, tmp_path):
    """End-to-end wiring: the run_action fresh-open path in _try_auto_open_board
    calls the clear helper, so a landed-write flag is dropped when the board is
    freshly opened from a matching disk state (finding B3)."""
    from kicad_interface import KiCADInterface

    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)
    board = tmp_path / "demo.kicad_pcb"
    board.write_text("(kicad_pcb)\n", encoding="utf-8")

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    iface.board = MagicMock()
    iface.board.GetFileName.return_value = str(board)
    iface._swig_writes_landed = True
    iface._board_disk_signature = KiCADInterface._disk_signature(str(board))
    iface._current_board_path = lambda: None  # force the run_action route
    iface._current_project_path = None
    iface._auto_open_cooldown_until = 0.0

    state = {"open": False}
    iface._ipc_has_open_board_document = lambda: state["open"]
    iface.ipc_backend = MagicMock()
    iface.ipc_backend.is_connected = lambda: True

    def _fake_run_action(action):
        state["open"] = True
        return {"success": True, "statusName": "RAS_OK"}

    iface.ipc_backend.run_action = _fake_run_action

    assert iface._try_auto_open_board(timeout_s=1.0) is True
    assert iface._swig_writes_landed is False
