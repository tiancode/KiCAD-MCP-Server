"""Board-identity gate (E2E P1): never operate on a foreign IPC document.

With KiCad already running project A and the MCP switched to project B, IPC can
stay bound to A (a second pcbnew serves its own ``api-<pid>.sock`` the initial
socket scan missed).  Every IPC-routed read/mutation for B then silently
targeted A — one even reporting ``auto_reconciled: true``.  The gate refuses
with ``wrong_board`` / WRONG_BOARD_OPEN before any read, mutation, or the
lossless auto-reconcile heal can run.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _make_iface(*, expected=None, ipc_board=None):
    """Bare interface with the flags the dispatcher gate reads.

    ``expected`` / ``ipc_board`` install instance-level overrides of the two
    board-path resolvers so the wiring can be exercised without a real SWIG
    board or kipy document (those are unit-tested separately below).
    """
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    iface.ipc_backend = MagicMock()
    # Default: reselect self-heal finds no matching instance.
    iface.ipc_backend.reselect_preferring_board.return_value = False
    iface.ipc_board_api = MagicMock()
    iface.board = None
    iface.command_routes = {}
    iface._board_disk_signature = None
    iface._current_project_path = None
    iface._last_auto_save_status = None
    iface._ipc_writes_pending = False
    iface._swig_writes_landed = False
    iface._ipc_change_callback_registered = False
    if expected is not None:
        iface._expected_project_board_path = lambda: expected  # type: ignore[assignment]
    if ipc_board is not None:
        iface._ipc_document_board_path = lambda: ipc_board  # type: ignore[assignment]
    return iface


def _patch_common(monkeypatch):
    """PCB-editor gate open; suppress the IPC (re)attach refresh churn."""
    from kicad_interface import KiCADInterface

    monkeypatch.setattr(KiCADInterface, "_ipc_has_open_board_document", lambda self: True)
    monkeypatch.setattr(KiCADInterface, "_try_enable_ipc_backend", lambda self, force=False: True)


# ---------------------------------------------------------------------------
# Pure helper: _ipc_board_identity_conflict
# ---------------------------------------------------------------------------
def _fake_ipc_api(board_filename: str, project_dir: str):
    doc = types.SimpleNamespace(
        board_filename=board_filename,
        project=types.SimpleNamespace(path=project_dir),
    )
    board = types.SimpleNamespace(document=doc)
    api = MagicMock()
    api._get_board.return_value = board
    return api


def test_expected_and_ipc_paths_resolved_from_real_objects():
    iface = _make_iface()
    swig_board = MagicMock()
    swig_board.GetFileName.return_value = "/proj/b/board.kicad_pcb"
    iface.board = swig_board
    iface.ipc_board_api = _fake_ipc_api("a.kicad_pcb", "/proj/a")

    assert iface._expected_project_board_path() == "/proj/b/board.kicad_pcb"
    assert iface._ipc_document_board_path() == "/proj/a/a.kicad_pcb"


def test_identity_conflict_when_boards_differ():
    iface = _make_iface(expected="/proj/b/board.kicad_pcb", ipc_board="/proj/a/a.kicad_pcb")
    out = iface._ipc_board_identity_conflict("get_board_info")
    assert out is not None
    assert out["success"] is False
    assert out["wrong_board"] == {
        "ipc_board": "/proj/a/a.kicad_pcb",
        "expected": "/proj/b/board.kicad_pcb",
    }


def test_no_conflict_when_boards_match():
    iface = _make_iface(expected="/proj/b/board.kicad_pcb", ipc_board="/proj/b/board.kicad_pcb")
    assert iface._ipc_board_identity_conflict() is None


def test_no_conflict_when_expected_unknown():
    """Pure-IPC attach with no open_project → nothing to compare → allow.

    Uses the REAL _expected_project_board_path (board=None, no project) so it
    returns None."""
    iface = _make_iface(ipc_board="/proj/a/a.kicad_pcb")
    iface.board = None
    iface._current_project_path = None
    assert iface._ipc_board_identity_conflict() is None


def test_no_conflict_when_ipc_path_unreadable():
    """Fail-open: a board doc is open (editor gate passed) but its path can't
    be read → don't false-refuse on incomplete info.

    Uses the REAL _ipc_document_board_path against a doc-less board API."""
    iface = _make_iface(expected="/proj/b/board.kicad_pcb")
    iface.ipc_board_api = MagicMock()
    iface.ipc_board_api._get_board.return_value = types.SimpleNamespace(document=None)
    assert iface._ipc_board_identity_conflict() is None


# ---------------------------------------------------------------------------
# Finding 2: the gate must read a LIVE document (not the forever-cached kipy
# Board wrapper), and the heal path must invalidate the cache.
# ---------------------------------------------------------------------------
def _pcb_doc(board_filename: str, project_dir: str):
    return types.SimpleNamespace(
        board_filename=board_filename,
        project=types.SimpleNamespace(path=project_dir),
    )


class _FakeClient:
    """Minimal kipy client: get_open_documents returns the CURRENT docs, which
    the test mutates to model a same-instance board switch."""

    def __init__(self, docs):
        self.docs = list(docs)

    def get_open_documents(self, *args):
        return self.docs


def test_identity_gate_reads_live_client_after_same_instance_switch():
    """Finding 2a: after the user switches the SAME KiCad instance to the
    project's board, the gate passes — it reads the live document, not the
    stale cached Board wrapper (which still reports the old board)."""
    from kicad_interface import KiCADInterface

    client = _FakeClient([_pcb_doc("a.kicad_pcb", "/proj/a")])
    backend = MagicMock()
    backend._kicad = client

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.ipc_backend = backend
    # A STALE cached wrapper still reporting board A — must NOT be the authority.
    iface.ipc_board_api = _fake_ipc_api("a.kicad_pcb", "/proj/a")
    iface.board = None
    iface._current_project_path = None
    iface._expected_project_board_path = lambda: "/proj/b/board.kicad_pcb"  # type: ignore[assignment]

    # Live client on board A → conflict with the expected board B.
    assert iface._ipc_board_identity_conflict() is not None
    # The user opens board B in the SAME instance.
    client.docs = [_pcb_doc("board.kicad_pcb", "/proj/b")]
    # The gate now passes — even though the cached wrapper still says A.
    assert iface._ipc_board_identity_conflict() is None


def test_reselect_heal_invalidates_board_cache():
    """Finding 2b: the heal path drops both cached Board wrappers so a post-heal
    read re-fetches the CURRENT document instead of the stale one."""
    from kicad_api.ipc_backend._backend import IPCBackend
    from kicad_api.ipc_backend._board_core import IPCBoardAPI
    from kicad_interface import KiCADInterface

    fake_client = object()
    api = IPCBoardAPI(fake_client, lambda *a, **k: None)
    api._board = "STALE_WRAPPER"  # a cached, now-stale document
    backend = IPCBackend()
    backend._board_api = api
    backend._kicad = fake_client
    backend._connected = True
    backend.reselect_preferring_board = lambda prefer_board_path=None: True  # type: ignore[assignment]

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.ipc_backend = backend
    iface.ipc_board_api = api
    iface.board = None
    iface._current_project_path = None
    iface._expected_project_board_path = lambda: "/proj/b/board.kicad_pcb"  # type: ignore[assignment]
    iface._refresh_ipc_board_api = lambda: True  # type: ignore[assignment]

    assert iface._try_reselect_to_expected_board() is True
    # Both caches invalidated → next _get_board re-fetches the current document.
    assert api._board is None
    assert backend._board_api._board is None


# ---------------------------------------------------------------------------
# Dispatcher wiring: IPC fast-path read + mutation
# ---------------------------------------------------------------------------
def test_ipc_read_refused_on_foreign_board(monkeypatch):
    _patch_common(monkeypatch)
    iface = _make_iface(expected="/proj/b/board.kicad_pcb", ipc_board="/proj/a/a.kicad_pcb")
    iface._ipc_get_board_info = lambda params: {"success": True, "componentCount": 7}

    out = iface.handle_command("get_board_info", {})

    assert out["success"] is False
    assert out["wrong_board"]["ipc_board"] == "/proj/a/a.kicad_pcb"
    assert out["wrong_board"]["expected"] == "/proj/b/board.kicad_pcb"
    assert out["errorCode"] == "WRONG_BOARD_OPEN"


def test_ipc_mutation_refused_on_foreign_board(monkeypatch):
    _patch_common(monkeypatch)
    iface = _make_iface(expected="/proj/b/board.kicad_pcb", ipc_board="/proj/a/a.kicad_pcb")
    called = {"n": 0}
    iface._ipc_place_component = lambda params: called.__setitem__("n", called["n"] + 1) or {
        "success": True
    }

    out = iface.handle_command("place_component", {"reference": "R1"})

    assert out["success"] is False
    assert out["errorCode"] == "WRONG_BOARD_OPEN"
    assert called["n"] == 0  # handler never ran on the foreign board


def test_foreign_board_blocks_auto_reconcile(monkeypatch):
    """The decisive E2E case: swigWritesLanded + a mutation on a foreign board
    must NOT auto-reconcile (revert A from disk) — it must refuse wrong_board.
    ``revert`` is never called."""
    _patch_common(monkeypatch)
    iface = _make_iface(expected="/proj/b/board.kicad_pcb", ipc_board="/proj/a/a.kicad_pcb")
    iface._swig_writes_landed = True
    revert = MagicMock(return_value=True)
    iface.ipc_board_api.revert = revert
    iface._ipc_place_component = lambda params: {"success": True}

    out = iface.handle_command("place_component", {"reference": "R1"})

    assert out["success"] is False
    assert out["errorCode"] == "WRONG_BOARD_OPEN"
    assert "auto_reconciled" not in out
    revert.assert_not_called()


def test_matching_board_allows_op(monkeypatch):
    """No false refusal when IPC is on the project's board."""
    _patch_common(monkeypatch)
    iface = _make_iface(expected="/proj/b/board.kicad_pcb", ipc_board="/proj/b/board.kicad_pcb")
    iface._ipc_get_board_info = lambda params: {"success": True, "componentCount": 27}

    out = iface.handle_command("get_board_info", {})

    assert out["success"] is True
    assert out["componentCount"] == 27
    assert "wrong_board" not in out


def test_reselect_heals_then_op_runs(monkeypatch):
    """If a sibling instance holds the project's board, the gate re-attaches to
    it (fix B self-heal) and the op runs instead of refusing."""
    _patch_common(monkeypatch)
    iface = _make_iface()
    # Initially foreign, then healed to the project's board after reselect.
    states = iter(["/proj/a/a.kicad_pcb", "/proj/b/board.kicad_pcb"])
    iface._expected_project_board_path = lambda: "/proj/b/board.kicad_pcb"  # type: ignore[assignment]
    iface._ipc_document_board_path = lambda: next(states)  # type: ignore[assignment]
    iface.ipc_backend.reselect_preferring_board.return_value = True
    from kicad_interface import KiCADInterface

    monkeypatch.setattr(KiCADInterface, "_refresh_ipc_board_api", lambda self: True)
    iface._ipc_get_board_info = lambda params: {"success": True, "componentCount": 27}

    out = iface.handle_command("get_board_info", {})

    assert out["success"] is True
    iface.ipc_backend.reselect_preferring_board.assert_called_once()


# ---------------------------------------------------------------------------
# require_ipc_board_op (IPC-only handlers path)
# ---------------------------------------------------------------------------
def test_require_ipc_board_op_refuses_foreign_board(monkeypatch):
    from kicad_interface import KiCADInterface

    iface = _make_iface(expected="/proj/b/board.kicad_pcb", ipc_board="/proj/a/a.kicad_pcb")
    monkeypatch.setattr(KiCADInterface, "ensure_ipc", lambda self, **kw: (True, ""))

    out = iface.require_ipc_board_op()

    assert out["success"] is False
    assert out["wrong_board"]["ipc_board"] == "/proj/a/a.kicad_pcb"
    # No _ipc_reason envelope — passed through verbatim by ipc_gate.require_ipc.
    assert "_ipc_reason" not in out


def test_require_ipc_board_op_read_only_also_refuses_foreign(monkeypatch):
    from kicad_interface import KiCADInterface

    iface = _make_iface(expected="/proj/b/board.kicad_pcb", ipc_board="/proj/a/a.kicad_pcb")
    monkeypatch.setattr(KiCADInterface, "ensure_ipc", lambda self, **kw: (True, ""))

    out = iface.require_ipc_board_op(read_only=True)

    assert out["success"] is False
    assert "wrong_board" in out


def test_require_ipc_board_op_clean_when_matching(monkeypatch):
    from kicad_interface import KiCADInterface

    iface = _make_iface(expected="/proj/b/board.kicad_pcb", ipc_board="/proj/b/board.kicad_pcb")
    monkeypatch.setattr(KiCADInterface, "ensure_ipc", lambda self, **kw: (True, ""))

    assert iface.require_ipc_board_op() == {}
