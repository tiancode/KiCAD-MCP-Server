"""Read-gate parity (F11 follow-up): IPC-only READ commands pass the
cross-backend conflict gate instead of refusing it.

Before this fix, with ``_swig_writes_landed`` set and IPC clean:
- a MUTATING IPC-only command (set_title_block_info) auto-reconciled and
  proceeded (F11), while
- a READ-ONLY one (list_shapes, get_origin, get_selection, ...) refused with
  ``needs_reconcile`` — reads are strictly safer than writes, so refusing them
  while healing writes was backwards.

Required behavior pinned here:
- reads pass the gate via ``require_ipc_board_op(read_only=True)``;
- reads do NOT trigger the auto-reconcile heal (a query must not revert
  KiCad) — the divergence flag survives the read;
- successful read results get the fast-path ``staleVsDisk``/``staleHint``
  annotation from the dispatcher;
- ``needs_pcb_editor`` and the raw ``_ipc_reason`` envelope are unchanged;
- IPC-capable reads that fall back to SWIG are NOT annotated (SWIG reads the
  on-disk file, which already includes the landed writes).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from kicad_interface import KiCADInterface  # noqa: E402

# The audited IPC-only read commands (in-handler gating via
# handlers/ipc_gate.require_ipc with read_only=True).
AUDITED_IPC_ONLY_READS = frozenset(
    {
        "list_shapes",
        "get_origin",
        "get_title_block_info",
        "get_selection",
        "hit_test",
        "get_transaction_status",
        "ipc_list_components",
        "ipc_get_tracks",
        "ipc_get_vias",
    }
)


def _make_iface(*, use_ipc=True):
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


# ---------------------------------------------------------------------------
# Gate level: require_ipc_board_op(read_only=...)
# ---------------------------------------------------------------------------
def test_read_only_gate_passes_during_swig_to_ipc_conflict(monkeypatch):
    monkeypatch.setattr(KiCADInterface, "ensure_ipc", lambda self, **kw: (True, ""))
    iface = _make_iface()
    iface._swig_writes_landed = True

    gate = iface.require_ipc_board_op(allow_launch=True, read_only=True)

    assert gate == {}


def test_mutating_gate_still_refuses_during_swig_to_ipc_conflict(monkeypatch):
    """read_only defaults False — the write path keeps its refusal."""
    monkeypatch.setattr(KiCADInterface, "ensure_ipc", lambda self, **kw: (True, ""))
    iface = _make_iface()
    iface._swig_writes_landed = True

    gate = iface.require_ipc_board_op(allow_launch=True)

    assert gate["success"] is False
    assert gate["needs_reconcile"] is True
    assert gate["direction"] == "swig_to_ipc"


def test_read_only_gate_keeps_needs_pcb_editor(monkeypatch):
    """read_only must NOT bypass the editor-frame gate."""
    iface = _make_iface()
    monkeypatch.setattr(
        KiCADInterface,
        "ensure_ipc",
        lambda self, **kw: (False, self._pcb_editor_gate_reason()),
    )

    gate = iface.require_ipc_board_op(allow_launch=True, read_only=True)

    assert gate["success"] is False
    assert gate["needs_pcb_editor"] is True


def test_read_only_gate_keeps_ipc_reason_envelope(monkeypatch):
    """read_only must NOT bypass the raw-reason (IPC unavailable) envelope."""
    iface = _make_iface(use_ipc=False)
    monkeypatch.setattr(KiCADInterface, "ensure_ipc", lambda self, **kw: (False, "boom"))

    gate = iface.require_ipc_board_op(allow_launch=True, read_only=True)

    assert gate["success"] is False
    assert gate["_ipc_reason"] == "boom"


# ---------------------------------------------------------------------------
# Handler wiring: read handlers pass read_only=True; mutators don't.
# ---------------------------------------------------------------------------
READ_HANDLER_CASES = [
    ("handlers.shapes", "handle_list_shapes", {}),
    ("handlers.selection", "handle_get_selection", {}),
    ("handlers.selection", "handle_hit_test", {"x": 1, "y": 2}),
    ("handlers.board_meta", "handle_get_origin", {}),
    ("handlers.board_meta", "handle_get_title_block_info", {}),
    ("handlers.transactions", "handle_get_transaction_status", {}),
    ("handlers.ipc", "handle_ipc_list_components", {}),
    ("handlers.ipc", "handle_ipc_get_tracks", {}),
    ("handlers.ipc", "handle_ipc_get_vias", {}),
]

MUTATOR_HANDLER_CASES = [
    ("handlers.shapes", "handle_add_segment", {}),
    ("handlers.shapes", "handle_delete_shape", {"ids": ["x"]}),
    ("handlers.board_meta", "handle_set_origin", {"type": "drill", "x": 0, "y": 0}),
    ("handlers.board_meta", "handle_set_title_block_info", {"title": "t"}),
    ("handlers.selection", "handle_clear_selection", {}),
    ("handlers.transactions", "handle_begin_transaction", {}),
    ("handlers.ipc", "handle_ipc_save_board", {}),
]


def _gate_probe_iface():
    """Iface whose gate refuses with a sentinel, recording call kwargs."""
    iface = MagicMock()
    iface.require_ipc_board_op = MagicMock(
        return_value={"success": False, "needs_pcb_editor": True}
    )
    return iface


@pytest.mark.parametrize("module_name, func_name, params", READ_HANDLER_CASES)
def test_read_handlers_gate_with_read_only_true(module_name, func_name, params):
    import importlib

    handler = getattr(importlib.import_module(module_name), func_name)
    iface = _gate_probe_iface()

    out = handler(iface, dict(params))

    kwargs = iface.require_ipc_board_op.call_args.kwargs
    assert kwargs.get("read_only") is True, f"{func_name} must gate read_only"
    # The gate refusal is returned verbatim (handler stopped at the gate).
    assert out["needs_pcb_editor"] is True


@pytest.mark.parametrize("module_name, func_name, params", MUTATOR_HANDLER_CASES)
def test_mutating_handlers_gate_with_read_only_false(module_name, func_name, params):
    import importlib

    handler = getattr(importlib.import_module(module_name), func_name)
    iface = _gate_probe_iface()

    handler(iface, dict(params))

    kwargs = iface.require_ipc_board_op.call_args.kwargs
    assert kwargs.get("read_only") is False, f"{func_name} must NOT gate read_only"


# ---------------------------------------------------------------------------
# Dispatcher end-to-end: read passes, gets staleVsDisk, never reconciles.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("command", ["list_shapes", "get_origin", "get_selection"])
def test_ipc_only_read_passes_and_is_annotated_stale(monkeypatch, command):
    monkeypatch.delenv("KICAD_AUTO_RECONCILE", raising=False)
    monkeypatch.setattr(KiCADInterface, "ensure_ipc", lambda self, **kw: (True, ""))
    iface = _make_iface()
    iface._swig_writes_landed = True
    iface._ipc_writes_pending = False
    fake_revert = MagicMock(return_value=True)
    iface.ipc_board_api.revert = fake_revert

    def fake_handler(params):
        # Faithful in-handler read gate.
        gate = iface.require_ipc_board_op(allow_launch=True, read_only=True)
        if gate:
            return gate
        return {"success": True, "items": []}

    iface.command_routes = {command: fake_handler}

    result = iface.handle_command(command, {})

    assert result["success"] is True
    assert "needs_reconcile" not in result
    # Same annotation the IPC fast-path reads get.
    assert result["staleVsDisk"] is True
    assert "reconcile_backends" in result["staleHint"]
    # A read must NOT heal (no revert) and must NOT clear the divergence flag.
    fake_revert.assert_not_called()
    assert "auto_reconciled" not in result
    assert iface._swig_writes_landed is True


def test_ipc_only_read_not_annotated_when_backends_clean(monkeypatch):
    monkeypatch.setattr(KiCADInterface, "ensure_ipc", lambda self, **kw: (True, ""))
    iface = _make_iface()

    def fake_handler(params):
        gate = iface.require_ipc_board_op(allow_launch=True, read_only=True)
        if gate:
            return gate
        return {"success": True, "shapes": []}

    iface.command_routes = {"list_shapes": fake_handler}

    result = iface.handle_command("list_shapes", {})

    assert result["success"] is True
    assert "staleVsDisk" not in result
    assert "staleHint" not in result


def test_ipc_only_read_failure_not_annotated(monkeypatch):
    """Only SUCCESSFUL reads get the stale hint — a failure payload must not
    be dressed up with staleness metadata."""
    monkeypatch.setattr(KiCADInterface, "ensure_ipc", lambda self, **kw: (True, ""))
    iface = _make_iface()
    iface._swig_writes_landed = True

    iface.command_routes = {"list_shapes": lambda p: {"success": False, "message": "boom"}}

    result = iface.handle_command("list_shapes", {})

    assert result["success"] is False
    assert "staleVsDisk" not in result


def test_ipc_capable_read_on_swig_fallback_not_annotated(monkeypatch):
    """get_board_info is in _IPC_READ_ONLY_COMMANDS *and* IPC_CAPABLE_COMMANDS.
    When it falls back to the SWIG handler (no IPC), the SWIG read comes from
    the on-disk file — which already INCLUDES the landed writes — so the
    dispatcher must not stamp it stale."""
    monkeypatch.setattr(KiCADInterface, "_try_enable_ipc_backend", lambda self, **kw: False)
    iface = _make_iface(use_ipc=False)
    iface._swig_writes_landed = True

    iface.command_routes = {"get_board_info": lambda p: {"success": True, "board": {}}}

    result = iface.handle_command("get_board_info", {})

    assert result["success"] is True
    assert "staleVsDisk" not in result


def test_read_passes_even_when_auto_reconcile_disabled(monkeypatch):
    """KICAD_AUTO_RECONCILE=false disables the WRITE heal, but reads don't
    reconcile at all — they must still pass."""
    monkeypatch.setenv("KICAD_AUTO_RECONCILE", "false")
    monkeypatch.setattr(KiCADInterface, "ensure_ipc", lambda self, **kw: (True, ""))
    iface = _make_iface()
    iface._swig_writes_landed = True

    def fake_handler(params):
        gate = iface.require_ipc_board_op(allow_launch=True, read_only=True)
        if gate:
            return gate
        return {"success": True, "origin": {"x": 0, "y": 0}}

    iface.command_routes = {"get_origin": fake_handler}

    result = iface.handle_command("get_origin", {})

    assert result["success"] is True
    assert result["staleVsDisk"] is True
    assert iface._swig_writes_landed is True


# ---------------------------------------------------------------------------
# Set-membership audits
# ---------------------------------------------------------------------------
def test_audited_reads_are_in_read_only_set():
    missing = AUDITED_IPC_ONLY_READS - KiCADInterface._IPC_READ_ONLY_COMMANDS
    assert not missing, f"IPC-only reads missing from _IPC_READ_ONLY_COMMANDS: {sorted(missing)}"


def test_list_shapes_is_in_read_only_set():
    """The specific command the finding named."""
    assert "list_shapes" in KiCADInterface._IPC_READ_ONLY_COMMANDS


def test_read_only_and_ipc_only_mutating_sets_disjoint():
    overlap = KiCADInterface._IPC_READ_ONLY_COMMANDS & KiCADInterface._IPC_ONLY_MUTATING_COMMANDS
    assert overlap == set()


def test_read_only_set_disjoint_from_board_mutating_set():
    overlap = KiCADInterface._IPC_READ_ONLY_COMMANDS & KiCADInterface._BOARD_MUTATING_COMMANDS
    assert overlap == set()
