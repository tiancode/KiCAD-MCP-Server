"""Board-scoped cross-backend gate (E2E P2, fix D + finding 1): switching
projects with open_project must not let the previous board's pending IPC writes
gate ops on the newly opened board (whose reconcile remedy would clobber the
OTHER project) — BUT the pending state must be board-SCOPED (stashed per board),
not dropped, so an A→B→A round-trip still gates a SWIG mutation on A that would
clobber KiCad's still-unsaved memory.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _healthy_board(filename):
    board = MagicMock()
    board.GetFileName.return_value = filename
    return board


def _make_iface():
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = False
    iface.ipc_backend = None
    iface.ipc_board_api = None
    iface.board = None
    iface.command_routes = {}
    iface._board_disk_signature = None
    iface._current_project_path = None
    iface._last_auto_save_status = None
    iface._ipc_writes_pending = False
    iface._ipc_writes_pending_by_board = {}
    iface._swig_writes_landed = False
    iface._ipc_change_callback_registered = False
    iface._auto_open_cooldown_until = 5.0
    iface._pending_fresh_open_clear = ("x", "y")
    iface.project_commands = MagicMock()
    iface._record_board_signature = MagicMock()
    iface._update_command_handlers = MagicMock()
    return iface


def _run_open(iface, new_board_path):
    iface.project_commands.board = _healthy_board(new_board_path)
    iface.command_routes = {
        "open_project": lambda p: {
            "success": True,
            "project": {"boardPath": new_board_path},
        }
    }
    return iface.handle_command("open_project", {"filename": new_board_path})


def test_switch_to_different_board_scopes_ipc_writes_pending():
    iface = _make_iface()
    iface.board = _healthy_board("/proj/a/a.kicad_pcb")
    # Pending IPC writes belong to board A.
    iface._ipc_writes_pending = True

    out = _run_open(iface, "/proj/b/b.kicad_pcb")

    assert out["success"] is True
    # Board switched → the live flag now reflects B (nothing pending), but A's
    # pending state is STASHED (not dropped) so a switch-back can restore it.
    assert iface._ipc_writes_pending is False
    assert iface._ipc_writes_pending_by_board["/proj/a/a.kicad_pcb"] is True
    assert iface._auto_open_cooldown_until == 0.0
    assert iface._pending_fresh_open_clear is None


def test_switch_back_restores_pending_and_gates_swig_mutation():
    """Finding 1: A (IPC edit, unsaved) → open B → open A again must RESTORE
    A's pending-IPC-writes gate, so a SWIG mutation on A refuses with
    needs_reconcile instead of clobbering KiCad's unsaved memory."""
    iface = _make_iface()
    iface.board = _healthy_board("/proj/a/a.kicad_pcb")
    iface._ipc_writes_pending = True  # unsaved IPC edit on A

    _run_open(iface, "/proj/b/b.kicad_pcb")
    assert iface._ipc_writes_pending is False  # B has nothing pending
    _run_open(iface, "/proj/a/a.kicad_pcb")
    # A's pending flag restored on switch-back.
    assert iface._ipc_writes_pending is True

    # A SWIG mutation on A must now be refused by the ipc_to_swig gate.
    ran = {"n": 0}
    iface.command_routes = {
        "route_pad_to_pad": lambda p: ran.__setitem__("n", ran["n"] + 1) or {"success": True}
    }
    iface._auto_save_board = MagicMock(return_value={"saved": True})

    out = iface.handle_command("route_pad_to_pad", {})

    assert out["success"] is False
    assert out.get("needs_reconcile") is True
    assert out.get("direction") == "ipc_to_swig"
    assert ran["n"] == 0  # mutation never ran on A


def test_reopen_same_board_keeps_ipc_writes_pending():
    """A same-board reopen keeps _ipc_writes_pending — KiCad may still hold
    unsaved edits that a SWIG mutation would clobber."""
    iface = _make_iface()
    iface.board = _healthy_board("/proj/a/a.kicad_pcb")
    iface._ipc_writes_pending = True

    out = _run_open(iface, "/proj/a/a.kicad_pcb")

    assert out["success"] is True
    assert iface._ipc_writes_pending is True


def test_first_open_no_previous_board_does_not_touch_flag():
    """First open (no previous board) is not a 'switch' — leave the flag as-is
    so single-board behaviour is unchanged."""
    iface = _make_iface()
    iface.board = None
    iface._ipc_writes_pending = True

    out = _run_open(iface, "/proj/b/b.kicad_pcb")

    assert out["success"] is True
    assert iface._ipc_writes_pending is True


def test_switch_does_not_falsely_gate_next_swig_mutation():
    """End-to-end: after switching A→B, a SWIG mutation on B is NOT refused by
    the ipc_to_swig gate that board A's pending IPC writes would have tripped."""
    iface = _make_iface()
    iface.board = _healthy_board("/proj/a/a.kicad_pcb")
    iface._ipc_writes_pending = True
    _run_open(iface, "/proj/b/b.kicad_pcb")

    # Now a SWIG board mutation on B (route_pad_to_pad is in _BOARD_MUTATING).
    ran = {"n": 0}
    iface.command_routes = {
        "route_pad_to_pad": lambda p: ran.__setitem__("n", ran["n"] + 1) or {"success": True}
    }
    iface._auto_save_board = MagicMock(return_value={"saved": True})

    out = iface.handle_command("route_pad_to_pad", {})

    assert out["success"] is True
    assert ran["n"] == 1
    assert "needs_reconcile" not in out
