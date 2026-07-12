"""F11 regression: IPC-ONLY mutating commands get the same swig_to_ipc
auto-reconcile as IPC-capable ones.

With a landed SWIG write and a clean IPC side, ``move_component`` (IPC fast
path) auto-reconciles and proceeds — but IPC-only mutators like
``set_title_block_info`` / ``set_origin`` / ``add_segment`` gate themselves in
their handler and used to return the ``needs_reconcile`` refusal verbatim.
Same conflict, opposite outcome.  The dispatcher now heals the lossless case
for these commands too; two-sided conflicts and the KICAD_AUTO_RECONCILE=false
opt-out still refuse verbatim.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _make_iface(*, use_ipc=True):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = use_ipc
    iface.ipc_backend = MagicMock() if use_ipc else None
    iface.ipc_board_api = MagicMock() if use_ipc else None
    iface.board = None
    iface.command_routes = {}
    iface._board_disk_signature = None
    iface._current_project_path = None
    iface._last_auto_save_status = None
    iface._ipc_writes_pending = False
    iface._swig_writes_landed = False
    iface._ipc_change_callback_registered = False
    return iface


@pytest.fixture(autouse=True)
def _default_env(monkeypatch):
    monkeypatch.delenv("KICAD_AUTO_RECONCILE", raising=False)  # default on


def _pin_reconcile(monkeypatch, iface, revert_result=True):
    """Wire the swig_to_ipc reconcile path: IPC reachable + revert()."""
    from kicad_interface import KiCADInterface

    monkeypatch.setattr(KiCADInterface, "ensure_ipc", lambda self, **kw: (True, ""))
    monkeypatch.setattr(KiCADInterface, "_refresh_ipc_board_api", lambda self: True)
    fake_revert = MagicMock(return_value=revert_result)
    iface.ipc_board_api.revert = fake_revert
    return fake_revert


# ---------------------------------------------------------------------------
# The core F11 fix: title_block / origin / shapes auto-reconcile like
# move_component.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("command", ["set_title_block_info", "set_origin", "add_segment"])
def test_ipc_only_mutation_auto_reconciles_when_swig_landed_and_ipc_clean(monkeypatch, command):
    iface = _make_iface()
    iface._swig_writes_landed = True
    iface._ipc_writes_pending = False
    fake_revert = _pin_reconcile(monkeypatch, iface)

    called = {"n": 0}

    def fake_handler(params):
        called["n"] += 1
        # By the time the handler runs the dispatcher has healed the conflict.
        assert iface._swig_writes_landed is False
        return {"success": True, "command": command}

    iface.command_routes = {command: fake_handler}

    result = iface.handle_command(command, {})

    fake_revert.assert_called_once()
    assert called["n"] == 1
    assert result["success"] is True
    assert result["auto_reconciled"] is True
    assert "ipc_revert" in result.get("stepsTaken", [])
    assert "needs_reconcile" not in result
    assert iface._swig_writes_landed is False
    assert iface._ipc_writes_pending is False


def test_ipc_only_two_sided_conflict_refuses_verbatim(monkeypatch):
    """IPC ALSO dirty → reverting would discard its changes.  No auto-heal; the
    handler's own gate returns the needs_reconcile refusal verbatim and the
    mutation does not run."""
    iface = _make_iface()
    iface._swig_writes_landed = True
    iface._ipc_writes_pending = True
    fake_revert = _pin_reconcile(monkeypatch, iface)

    called = {"n": 0}

    def fake_handler(params):
        # Faithfully reproduce the in-handler gate (handlers/ipc_gate.require_ipc).
        gate = iface.require_ipc_board_op(allow_launch=True)
        if gate:
            return gate
        called["n"] += 1
        return {"success": True}

    iface.command_routes = {"set_title_block_info": fake_handler}

    result = iface.handle_command("set_title_block_info", {})

    assert result["success"] is False
    assert result["needs_reconcile"] is True
    assert result["direction"] == "swig_to_ipc"
    fake_revert.assert_not_called()
    assert called["n"] == 0
    assert "auto_reconciled" not in result


def test_ipc_only_auto_reconcile_disabled_refuses_verbatim(monkeypatch):
    """KICAD_AUTO_RECONCILE=false opts out of the dispatcher auto-heal; the
    in-handler gate returns the verbatim needs_reconcile refusal."""
    monkeypatch.setenv("KICAD_AUTO_RECONCILE", "false")
    iface = _make_iface()
    iface._swig_writes_landed = True
    iface._ipc_writes_pending = False
    fake_revert = _pin_reconcile(monkeypatch, iface)

    def fake_handler(params):
        gate = iface.require_ipc_board_op(allow_launch=True)
        if gate:
            return gate
        return {"success": True}

    iface.command_routes = {"set_origin": fake_handler}

    result = iface.handle_command("set_origin", {})

    assert result["success"] is False
    assert result["needs_reconcile"] is True
    assert result["direction"] == "swig_to_ipc"
    fake_revert.assert_not_called()
    assert iface._swig_writes_landed is True  # flag preserved — divergence not lost


def test_ipc_only_in_handler_gate_passes_after_dispatcher_heals(monkeypatch):
    """End-to-end: a handler that runs the real ``require_ipc_board_op`` gate
    must find it CLEAN after the dispatcher's auto-heal, so the mutation
    proceeds instead of bouncing on its own conflict check."""
    iface = _make_iface()
    iface._swig_writes_landed = True
    iface._ipc_writes_pending = False
    _pin_reconcile(monkeypatch, iface)

    gate_seen = {}

    def fake_handler(params):
        gate = iface.require_ipc_board_op(allow_launch=True)
        gate_seen["gate"] = gate
        if gate:
            return gate
        return {"success": True, "titleBlock": {"title": "GD32 Dev Board"}}

    iface.command_routes = {"set_title_block_info": fake_handler}

    result = iface.handle_command("set_title_block_info", {})

    assert gate_seen["gate"] == {}  # clean by the time the handler checks
    assert result["success"] is True
    assert result["auto_reconciled"] is True


def test_ipc_only_no_conflict_runs_without_markers(monkeypatch):
    """Clean backends → the command runs normally, no auto_reconciled marker
    leaks onto the response."""
    iface = _make_iface()
    iface._swig_writes_landed = False
    iface._ipc_writes_pending = False

    def fake_handler(params):
        return {"success": True}

    iface.command_routes = {"add_circle": fake_handler}

    result = iface.handle_command("add_circle", {})

    assert result["success"] is True
    assert "auto_reconciled" not in result
    assert "needs_reconcile" not in result


# ---------------------------------------------------------------------------
# Set-membership audit
# ---------------------------------------------------------------------------
def test_ipc_only_mutating_set_scoped_to_board_content_mutators():
    from kicad_interface import KiCADInterface

    mut = KiCADInterface._IPC_ONLY_MUTATING_COMMANDS
    expected = {
        "set_origin",
        "set_title_block_info",
        "add_segment",
        "add_arc",
        "add_circle",
        "add_rectangle",
        "add_polygon",
        "delete_shape",
        "edit_shape",
    }
    assert mut == expected


def test_ipc_only_mutating_set_disjoint_from_read_only():
    """A command can't be both a mutator to auto-heal and a read-only query."""
    from kicad_interface import KiCADInterface

    overlap = KiCADInterface._IPC_ONLY_MUTATING_COMMANDS & KiCADInterface._IPC_READ_ONLY_COMMANDS
    assert overlap == set()


def test_ipc_only_mutating_set_all_ipc_required():
    """Every auto-healed IPC-only mutator must be a real IPC-required command."""
    from kicad_interface import KiCADInterface

    unknown = KiCADInterface._IPC_ONLY_MUTATING_COMMANDS - set(KiCADInterface.IPC_REQUIRED_COMMANDS)
    assert unknown == set()
