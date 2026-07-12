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
    monkeypatch.setattr(KiCADInterface, "_ipc_has_open_board_document", lambda self: True)


# ---------------------------------------------------------------------------
# Gate at the IPC fast-path dispatch
# ---------------------------------------------------------------------------
def test_ipc_fastpath_blocked_when_swig_wrote_disk(monkeypatch):
    """An IPC mutation through the fast path must refuse to land when SWIG
    just wrote new content to disk — KiCad memory is stale and the IPC
    save would overwrite the SWIG content.

    Finding B11 made the *default* behavior auto-heal this exact case (the
    swig_to_ipc direction is lossless when IPC is clean), so this test now
    pins the refusal that remains under the ``KICAD_AUTO_RECONCILE=false``
    opt-out.  The auto-heal path itself is covered by
    ``test_ipc_fastpath_auto_reconciles_when_swig_wrote_disk_and_ipc_clean``."""
    monkeypatch.setenv("KICAD_AUTO_RECONCILE", "false")
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


def test_ipc_fastpath_auto_reconciles_when_swig_wrote_disk_and_ipc_clean(monkeypatch):
    """Finding B11: a swig_to_ipc conflict with a CLEAN IPC side is lossless to
    heal (revert KiCad from disk, which already holds the SWIG writes).  The
    dispatcher must auto-reconcile and then run the command, surfacing
    ``auto_reconciled: true`` — not bounce the caller to a manual reconcile."""
    from kicad_interface import KiCADInterface

    monkeypatch.delenv("KICAD_AUTO_RECONCILE", raising=False)  # default on
    iface = _make_iface(use_ipc=True)
    iface.board = None
    iface._swig_writes_landed = True
    iface._ipc_writes_pending = False
    # Reconcile's revert path: IPC reachable + revert succeeds.  Pin the board
    # API so the dispatcher's _try_enable_ipc_backend refresh doesn't swap it
    # out from under our fake_revert.
    monkeypatch.setattr(KiCADInterface, "ensure_ipc", lambda self, **kw: (True, ""))
    monkeypatch.setattr(KiCADInterface, "_refresh_ipc_board_api", lambda self: True)
    fake_revert = MagicMock(return_value=True)
    iface.ipc_board_api.revert = fake_revert

    called = {"n": 0}

    def fake_place(params):
        called["n"] += 1
        return {"success": True, "reference": "R1"}

    iface._ipc_place_component = fake_place  # type: ignore[attr-defined]

    result = iface.handle_command("place_component", {"reference": "R1"})

    # The reconcile ran (revert called) and the command executed once.
    fake_revert.assert_called_once()
    assert called["n"] == 1
    assert result["success"] is True
    assert result["auto_reconciled"] is True
    assert "ipc_revert" in result.get("stepsTaken", [])
    # Flags cleared by the reconcile so no further gate fires.
    assert iface._swig_writes_landed is False
    assert iface._ipc_writes_pending is False
    assert "needs_reconcile" not in result


def test_ipc_fastpath_two_sided_conflict_still_refuses_verbatim(monkeypatch):
    """Finding B11: when IPC ALSO has unsaved changes, reverting would discard
    them — a genuine two-sided conflict.  Auto-heal must NOT run; the original
    needs_reconcile refusal is returned verbatim and revert is never called."""
    monkeypatch.delenv("KICAD_AUTO_RECONCILE", raising=False)  # default on
    iface = _make_iface(use_ipc=True)
    iface._swig_writes_landed = True
    iface._ipc_writes_pending = True
    fake_revert = MagicMock(return_value=True)
    iface.ipc_board_api.revert = fake_revert
    called = {"n": 0}

    def fake_place(params):
        called["n"] += 1
        return {"success": True}

    iface._ipc_place_component = fake_place  # type: ignore[attr-defined]

    result = iface.handle_command("place_component", {"reference": "R1"})

    assert result["success"] is False
    assert result["needs_reconcile"] is True
    assert result["direction"] == "swig_to_ipc"
    fake_revert.assert_not_called()
    assert called["n"] == 0
    assert "auto_reconciled" not in result


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


def test_ipc_read_flagged_stale_when_swig_wrote_disk():
    """The user's footgun: after sync_schematic_to_board lands 359 footprints
    on disk, get_board_info reads KiCad's stale in-memory board and would
    return componentCount 0.  The read is let through (no data loss) but must
    carry a staleVsDisk hint so the caller knows disk is ahead."""
    iface = _make_iface(use_ipc=True)
    iface._swig_writes_landed = True
    iface._ipc_get_board_info = lambda params: {  # type: ignore[attr-defined]
        "success": True,
        "componentCount": 0,
    }

    result = iface.handle_command("get_board_info", {})

    assert result["success"] is True
    assert result["staleVsDisk"] is True
    assert "Revert" in result["staleHint"]
    # Still a let-through read, not a refusal.
    assert "needs_reconcile" not in result


def test_ipc_read_not_flagged_stale_when_backends_clean():
    """No SWIG writes pending → the IPC read reflects disk, so no stale flag
    is attached (the hint must stay targeted, not a blanket banner)."""
    iface = _make_iface(use_ipc=True)
    iface._swig_writes_landed = False
    iface._ipc_get_board_info = lambda params: {  # type: ignore[attr-defined]
        "success": True,
        "componentCount": 359,
    }

    result = iface.handle_command("get_board_info", {})

    assert result["success"] is True
    assert "staleVsDisk" not in result
    assert "staleHint" not in result


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
    """Save clears the pending flag but must NOT refresh the board signature.

    Finding N1: refreshing ``_board_disk_signature`` here made the recorded
    signature match the freshly-saved disk while the SWIG in-memory board
    still held the PRE-save content — hiding the divergence from
    ``_reload_swig_board_if_disk_changed`` and serving stale SWIG reads.  The
    stale signature IS the reload trigger now."""
    iface = _make_iface(use_ipc=True)
    iface._ipc_writes_pending = True
    iface._record_board_signature = MagicMock()

    iface._on_ipc_change("save", {})

    assert iface._ipc_writes_pending is False
    iface._record_board_signature.assert_not_called()


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
def test_reconcile_backends_swig_to_ipc_reverts_kicad_from_disk(monkeypatch):
    """SWIG landed disk content and IPC is clean → reload KiCad from disk via
    board.revert() (kipy DOES expose this). Both gate flags clear on success."""
    from handlers import ui as ui_handler
    from kicad_interface import KiCADInterface

    iface = _make_iface(use_ipc=True)
    iface._swig_writes_landed = True
    iface._ipc_writes_pending = False
    monkeypatch.setattr(KiCADInterface, "ensure_ipc", lambda self, **kw: (True, ""))
    fake_revert = MagicMock(return_value=True)
    iface.ipc_board_api.revert = fake_revert

    out = ui_handler.handle_reconcile_backends(iface, {"direction": "swig_to_ipc"})

    assert out["success"] is True
    assert out["direction"] == "swig_to_ipc"
    assert out["stepsTaken"] == ["ipc_revert"]
    fake_revert.assert_called_once()
    assert iface._swig_writes_landed is False
    assert iface._ipc_writes_pending is False


def test_reconcile_backends_swig_to_ipc_refused_when_ipc_also_dirty(monkeypatch):
    """If KiCad also has unsaved IPC changes, reverting would discard them —
    a genuine two-sided conflict only the user can resolve. revert must NOT
    be called."""
    from handlers import ui as ui_handler
    from kicad_interface import KiCADInterface

    iface = _make_iface(use_ipc=True)
    iface._swig_writes_landed = True
    iface._ipc_writes_pending = True
    monkeypatch.setattr(KiCADInterface, "ensure_ipc", lambda self, **kw: (True, ""))
    fake_revert = MagicMock(return_value=True)
    iface.ipc_board_api.revert = fake_revert

    out = ui_handler.handle_reconcile_backends(iface, {"direction": "swig_to_ipc"})

    assert out["success"] is False
    assert out["needs_manual_action"] is True
    assert "discard" in out["message"].lower()
    fake_revert.assert_not_called()


def test_reconcile_backends_swig_to_ipc_noop_when_nothing_landed():
    """No SWIG writes landed → nothing to push into KiCad."""
    from handlers import ui as ui_handler

    iface = _make_iface(use_ipc=True)
    iface._swig_writes_landed = False

    out = ui_handler.handle_reconcile_backends(iface, {"direction": "swig_to_ipc"})

    assert out["success"] is True
    assert out["noop"] is True


def test_reconcile_backends_swig_to_ipc_points_to_other_direction_when_only_ipc_dirty():
    """SWIG has nothing to push but IPC is dirty → don't claim "in sync";
    redirect the caller to ipc_to_swig and don't touch KiCad."""
    from handlers import ui as ui_handler

    iface = _make_iface(use_ipc=True)
    iface._swig_writes_landed = False
    iface._ipc_writes_pending = True

    out = ui_handler.handle_reconcile_backends(iface, {"direction": "swig_to_ipc"})

    assert out["success"] is False
    assert "ipc_to_swig" in out["message"]
    assert out.get("noop") is not True


def test_reconcile_backends_swig_to_ipc_falls_back_when_revert_fails(monkeypatch):
    """board.revert() returning False → surface manual recovery steps rather
    than claiming success."""
    from handlers import ui as ui_handler
    from kicad_interface import KiCADInterface

    iface = _make_iface(use_ipc=True)
    iface._swig_writes_landed = True
    iface._ipc_writes_pending = False
    monkeypatch.setattr(KiCADInterface, "ensure_ipc", lambda self, **kw: (True, ""))
    iface.ipc_board_api.revert = MagicMock(return_value=False)

    out = ui_handler.handle_reconcile_backends(iface, {"direction": "swig_to_ipc"})

    assert out["success"] is False
    assert out["needs_manual_action"] is True
    assert "Revert" in out["message"]
    # gate flag stays set so the divergence isn't silently forgotten
    assert iface._swig_writes_landed is True


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


# ---------------------------------------------------------------------------
# IPCBoardAPI.revert() — the binding the codebase wrongly called nonexistent
# ---------------------------------------------------------------------------
def test_ipc_board_api_revert_proxies_and_drops_cache():
    """revert() calls kipy Board.revert(), invalidates the cached board, and
    must NOT fire the change callback (a revert leaves KiCad memory == disk;
    _on_ipc_change would otherwise mark the IPC side dirty)."""
    from kicad_api.ipc_backend import IPCBoardAPI

    notifications = []
    board = MagicMock()
    api = IPCBoardAPI(None, lambda *a: notifications.append(a))
    api._board = board

    assert api.revert() is True
    board.revert.assert_called_once()
    assert api._board is None  # cache dropped → next query re-fetches
    assert notifications == []  # no dirty-marking notify


def test_ipc_board_api_revert_returns_false_on_error():
    from kicad_api.ipc_backend import IPCBoardAPI

    board = MagicMock()
    board.revert.side_effect = RuntimeError("RevertDocument not supported")
    api = IPCBoardAPI(None, lambda *a: None)
    api._board = board

    assert api.revert() is False


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
