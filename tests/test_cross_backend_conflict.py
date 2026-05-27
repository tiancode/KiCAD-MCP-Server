"""Regression tests for the cross-backend write-conflict gate.

KiCAD-MCP routes mutations through two backends — SWIG (in-memory + disk
file) and IPC (KiCad's UI memory).  Writes from one side silently
invalidate the other; in practice the user reported a sequence where
``ipc_save_board`` overwrote SWIG-written traces.

These tests cover the dispatcher's conflict guard and the
``reconcile_backends`` tool that resolves the IPC→SWIG direction
programmatically.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _make_iface(*, use_ipc=True):
    """Construct a KiCADInterface without running its real __init__.

    Initialises the flags the dispatcher gate reads so tests get the same
    defaults as a freshly-booted interface.
    """
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
def _pcb_editor_open(monkeypatch):
    """PCB editor gate is orthogonal; assume it's open in these tests."""
    from kicad_interface import KiCADInterface
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr(KiCADProcessManager, "is_pcb_editor_running", lambda: True)
    monkeypatch.setattr(
        KiCADInterface, "_ipc_has_open_board_document", lambda self: True
    )


# ---------------------------------------------------------------------------
# Gate at the IPC fast-path dispatch
# ---------------------------------------------------------------------------
def test_ipc_fastpath_blocked_when_swig_wrote_disk():
    """An IPC mutation through the fast path must refuse to land when SWIG
    just wrote new content to disk — KiCad memory is stale and the IPC
    save would overwrite the SWIG content."""
    iface = _make_iface(use_ipc=True)
    iface._swig_writes_landed = True
    iface._ipc_place_component = lambda params: {  # type: ignore[attr-defined]
        "success": True,
    }

    result = iface.handle_command("place_component", {"reference": "R1"})

    assert result["success"] is False
    assert result["needs_reconcile"] is True
    assert result["direction"] == "swig_to_ipc"
    # The handler must NOT have run.
    assert "SWIG" in result["message"]


def test_ipc_fastpath_read_only_allowed_even_when_swig_dirty():
    """Read-only IPC queries don't mutate KiCad memory and can't cause
    data loss — let them through so an agent can still introspect the
    board while the user reloads in KiCad."""
    iface = _make_iface(use_ipc=True)
    iface._swig_writes_landed = True
    iface._ipc_get_board_info = lambda params: {  # type: ignore[attr-defined]
        "success": True,
        "board": {"size": "100x80"},
    }

    result = iface.handle_command("get_board_info", {})

    assert result["success"] is True
    assert "needs_reconcile" not in result


# ---------------------------------------------------------------------------
# Gate at the SWIG dispatch (BOARD_MUTATING_COMMANDS)
# ---------------------------------------------------------------------------
def test_swig_mutation_blocked_when_ipc_writes_pending():
    """SWIG mutations must refuse when IPC has unsaved changes — SWIG would
    read the stale disk file and its auto-save would lose the IPC writes."""
    iface = _make_iface(use_ipc=True)
    iface._ipc_writes_pending = True
    called = {"n": 0}

    def fake_route_handler(params):
        called["n"] += 1
        return {"success": True}

    iface.command_routes = {"route_pad_to_pad": fake_route_handler}

    result = iface.handle_command("route_pad_to_pad", {})

    assert result["success"] is False
    assert result["needs_reconcile"] is True
    assert result["direction"] == "ipc_to_swig"
    assert "reconcile_backends" in result["message"]
    # The SWIG handler must NOT have been invoked.
    assert called["n"] == 0


def test_swig_non_mutating_command_passes_when_ipc_dirty():
    """The gate only fires for SWIG board mutators — lifecycle / query /
    schematic commands continue to run regardless of IPC dirty state."""
    iface = _make_iface(use_ipc=True)
    iface._ipc_writes_pending = True
    called = {"n": 0}

    def fake_handler(params):
        called["n"] += 1
        return {"success": True, "rows": []}

    # list_schematic_components is not in _BOARD_MUTATING_COMMANDS.
    iface.command_routes = {"list_schematic_components": fake_handler}

    result = iface.handle_command("list_schematic_components", {})

    assert result["success"] is True
    assert called["n"] == 1


# ---------------------------------------------------------------------------
# Change-callback marks IPC dirty on real mutations and clean on save
# ---------------------------------------------------------------------------
def test_ipc_change_callback_marks_dirty_on_mutation():
    iface = _make_iface(use_ipc=True)
    assert iface._ipc_writes_pending is False

    iface._on_ipc_change("component_added", {"reference": "R1"})

    assert iface._ipc_writes_pending is True


def test_ipc_change_callback_clears_on_save():
    iface = _make_iface(use_ipc=True)
    iface._ipc_writes_pending = True
    iface._record_board_signature = MagicMock()

    iface._on_ipc_change("save", {})

    assert iface._ipc_writes_pending is False
    iface._record_board_signature.assert_called_once()


def test_ipc_change_callback_ignores_selection_events():
    """Selection state isn't saved to disk and doesn't cause data loss —
    don't mark the IPC side dirty on those events."""
    iface = _make_iface(use_ipc=True)
    assert iface._ipc_writes_pending is False

    for ev in ("selection_cleared", "selection_added", "selection_removed", "action_invoked"):
        iface._ipc_writes_pending = False
        iface._on_ipc_change(ev, {})
        assert iface._ipc_writes_pending is False, f"{ev} must not dirty IPC"


# ---------------------------------------------------------------------------
# reconcile_backends handler behaviour
# ---------------------------------------------------------------------------
def test_reconcile_backends_swig_to_ipc_refused_with_manual_steps():
    """The IPC side has no reload-from-disk API, so this direction returns
    manual recovery steps instead of attempting anything."""
    from handlers import ui as ui_handler

    iface = _make_iface(use_ipc=True)
    iface._swig_writes_landed = True

    out = ui_handler.handle_reconcile_backends(iface, {"direction": "swig_to_ipc"})

    assert out["success"] is False
    assert out["direction"] == "swig_to_ipc"
    assert out["needs_manual_action"] is True
    assert "Revert" in out["message"]
    assert isinstance(out["steps"], list) and len(out["steps"]) > 0


def test_reconcile_backends_ipc_to_swig_noop_when_both_clean():
    from handlers import ui as ui_handler

    iface = _make_iface(use_ipc=True)

    out = ui_handler.handle_reconcile_backends(iface, {"direction": "ipc_to_swig"})

    assert out["success"] is True
    assert out["noop"] is True


def test_reconcile_backends_ipc_to_swig_flushes_and_reloads(monkeypatch, tmp_path):
    """Happy-path reconcile: IPC dirty → ipc_save_board succeeds → SWIG
    reload from disk → both flags clear."""
    from handlers import ui as ui_handler
    from kicad_interface import KiCADInterface

    iface = _make_iface(use_ipc=True)
    iface._ipc_writes_pending = True

    # Pretend a board file exists at this path; _safe_load_board returns
    # a fresh fake board, not None.
    board_path = tmp_path / "demo.kicad_pcb"
    board_path.write_text("(kicad_pcb)\n", encoding="utf-8")

    fake_save = MagicMock(return_value=True)
    iface.ipc_board_api.save = fake_save

    monkeypatch.setattr(
        KiCADInterface,
        "ensure_ipc",
        lambda self, **kw: (True, ""),
    )
    monkeypatch.setattr(
        KiCADInterface,
        "_current_board_path",
        lambda self: str(board_path),
    )
    fake_reloaded_board = MagicMock(name="reloaded_board")
    monkeypatch.setattr(
        KiCADInterface,
        "_safe_load_board",
        lambda self, p: fake_reloaded_board,
    )
    monkeypatch.setattr(
        KiCADInterface,
        "_update_command_handlers",
        lambda self: None,
    )
    monkeypatch.setattr(
        KiCADInterface,
        "_record_board_signature",
        lambda self, *a, **k: None,
    )
    iface.project_commands = MagicMock()

    out = ui_handler.handle_reconcile_backends(iface, {"direction": "ipc_to_swig"})

    assert out["success"] is True
    assert out["direction"] == "ipc_to_swig"
    assert "ipc_save_board" in out["stepsTaken"]
    assert "swig_reload" in out["stepsTaken"]
    assert iface.board is fake_reloaded_board
    assert iface._ipc_writes_pending is False
    assert iface._swig_writes_landed is False
    fake_save.assert_called_once()


def test_reconcile_backends_rejects_unknown_direction():
    from handlers import ui as ui_handler

    iface = _make_iface(use_ipc=True)

    out = ui_handler.handle_reconcile_backends(iface, {"direction": "bogus"})

    assert out["success"] is False
    assert "direction" in out["message"]


# ---------------------------------------------------------------------------
# get_backend_state surfaces the dirty flags so callers can pre-empt the gate
# ---------------------------------------------------------------------------
def test_get_backend_state_exposes_cross_backend_dirty_flags(monkeypatch):
    """An agent should be able to detect divergence without hitting the
    dispatch-time gate first."""
    from handlers import ui as ui_handler
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr(KiCADProcessManager, "is_running", lambda: False)

    iface = _make_iface(use_ipc=False)
    iface.ipc_board_api = None
    iface._ipc_writes_pending = True
    iface._swig_writes_landed = True
    iface._backend_status = lambda: {
        "backend": "swig",
        "realtime_sync": False,
        "ipc_connected": False,
        "capabilities": {},
    }
    iface._current_board_path = lambda: None
    iface._current_project_file_path = lambda p: None
    iface._dirty_state = lambda p: {
        "dirty": False,
        "dirtyReason": None,
        "diskChangedExternally": False,
    }

    out = ui_handler.handle_get_backend_state(iface, {})

    assert out["ipcWritesPending"] is True
    assert out["swigWritesLanded"] is True
