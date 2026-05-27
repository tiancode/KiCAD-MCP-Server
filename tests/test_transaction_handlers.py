"""Tests for transaction handlers + the IPCBoardAPI helpers they drive.

Two layers under test:
  1. handlers/transactions.py — input validation + forward-to-backend
  2. kicad_api/ipc_backend.py — _apply_create/update/remove respect any
     open transaction (don't open their own commit when one is active)
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


# ---------------------------------------------------------------------------
# Stub kipy.util.units so importing IPCBoardAPI doesn't pull a real install.
# ---------------------------------------------------------------------------
_kipy = sys.modules.setdefault("kipy", MagicMock(name="kipy"))
_kipy_util = sys.modules.setdefault("kipy.util", MagicMock(name="kipy.util"))
_kipy_units = sys.modules.setdefault("kipy.util.units", MagicMock(name="kipy.util.units"))
_kipy_units.to_mm = lambda v: v / 1_000_000
_kipy_units.from_mm = lambda v: int(v * 1_000_000)


def _make_iface(ipc_board_api=None, use_ipc=True):
    """Bare KiCADInterface with the few attributes our handlers read."""
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = use_ipc
    iface.ipc_backend = MagicMock() if use_ipc else None
    iface.ipc_board_api = ipc_board_api
    iface.board = None
    iface.command_routes = {}
    iface._board_disk_signature = None
    iface._current_project_path = None
    iface._last_auto_save_status = None
    return iface


# ===========================================================================
# Handler layer — input validation + forwarding shape.
# ===========================================================================
class _RecordingAPI:
    def __init__(self):
        self.calls = []

    def begin_transaction(self, description):
        self.calls.append(("begin", description))
        return {"success": True, "description": description}

    def commit_transaction(self, description):
        self.calls.append(("commit", description))
        return {"success": True, "description": description or "default"}

    def rollback_transaction(self):
        self.calls.append(("rollback",))
        return {"success": True}

    def get_transaction_status(self):
        self.calls.append(("status",))
        return {"success": True, "open": False, "description": None}


def test_begin_transaction_forwards_description():
    api = _RecordingAPI()
    iface = _make_iface(api)
    out = iface._handle_begin_transaction({"description": "Move power section"})
    assert out["success"] is True
    assert api.calls[-1] == ("begin", "Move power section")


def test_begin_transaction_forwards_none_when_description_missing():
    """Handler no longer substitutes a default — that's the backend's job
    so 'MCP Operation' lives in exactly one place. None here = caller
    didn't supply a description, distinguishable from explicit ''."""
    api = _RecordingAPI()
    iface = _make_iface(api)
    iface._handle_begin_transaction({})
    assert api.calls[-1] == ("begin", None)


def test_begin_transaction_forwards_explicit_empty_string():
    """Handler must NOT swallow an empty-string description via `or`."""
    api = _RecordingAPI()
    iface = _make_iface(api)
    iface._handle_begin_transaction({"description": ""})
    assert api.calls[-1] == ("begin", "")


def test_commit_transaction_forwards_description_or_none():
    api = _RecordingAPI()
    iface = _make_iface(api)
    iface._handle_commit_transaction({})
    assert api.calls[-1] == ("commit", None)
    iface._handle_commit_transaction({"description": "Renamed"})
    assert api.calls[-1] == ("commit", "Renamed")


def test_rollback_transaction_forwards():
    api = _RecordingAPI()
    iface = _make_iface(api)
    iface._handle_rollback_transaction({})
    assert api.calls[-1] == ("rollback",)


def test_get_transaction_status_forwards():
    api = _RecordingAPI()
    iface = _make_iface(api)
    out = iface._handle_get_transaction_status({})
    assert out["success"] is True
    assert out["open"] is False


def test_transaction_handlers_require_ipc():
    iface = _make_iface(ipc_board_api=None, use_ipc=False)
    for cmd in (
        "begin_transaction",
        "commit_transaction",
        "rollback_transaction",
        "get_transaction_status",
    ):
        out = getattr(iface, f"_handle_{cmd}")({})
        assert out["success"] is False
        assert "IPC" in out["message"]


# ===========================================================================
# Backend layer — transaction state machine + mutator helpers.
# ===========================================================================
class _ServerStubItem:
    """Stand-in for what kipy returns from create_items: a fresh wrapper
    carrying the server-assigned KIID.  Tests use this to verify that
    _apply_create reads the id from the *returned* wrapper, not from the
    locally-built input wrapper."""

    def __init__(self, kiid):
        self.id = kiid


class _FakeBoard:
    """Tracks every commit-related call so tests can assert on the order
    of begin_commit / create_items / push_commit / drop_commit.

    ``create_items`` returns a one-element list of ``_ServerStubItem``
    with a generated KIID, mirroring kipy's real behaviour (the input
    wrapper is not mutated; the server hands back a new wrapper with
    the assigned id)."""

    def __init__(self):
        self.events = []
        self._next_commit_id = 0
        self._next_kiid = 0

    def begin_commit(self):
        self._next_commit_id += 1
        handle = f"commit-{self._next_commit_id}"
        self.events.append(("begin", handle))
        return handle

    def push_commit(self, commit, description):
        self.events.append(("push", commit, description))

    def drop_commit(self, commit):
        self.events.append(("drop", commit))

    def create_items(self, item):
        self.events.append(("create", item))
        self._next_kiid += 1
        return [_ServerStubItem(f"kiid-{self._next_kiid}")]

    def update_items(self, items):
        self.events.append(("update", tuple(items)))

    def remove_items(self, items):
        self.events.append(("remove", tuple(items)))


def _make_api(board):
    from kicad_api.ipc_backend import IPCBoardAPI

    api = IPCBoardAPI.__new__(IPCBoardAPI)
    api._kicad = MagicMock()
    api._board = board
    api._notify = lambda *a, **k: None
    api._current_commit = None
    api._current_commit_description = None
    return api


def test_apply_create_outside_transaction_wraps_its_own_commit():
    board = _FakeBoard()
    api = _make_api(board)
    api._apply_create(board, "item-1", "Added X")
    # Single-call path: begin → create → push.
    assert board.events == [
        ("begin", "commit-1"),
        ("create", "item-1"),
        ("push", "commit-1", "Added X"),
    ]


def test_apply_create_inside_transaction_skips_per_call_commit():
    board = _FakeBoard()
    api = _make_api(board)
    api.begin_transaction("Batch")
    api._apply_create(board, "item-1", "Added X")
    api._apply_create(board, "item-2", "Added Y")
    # Two creates rode the *one* commit opened by begin_transaction —
    # no per-call begin/push, no extra undo entries.
    assert board.events == [
        ("begin", "commit-1"),  # only from begin_transaction
        ("create", "item-1"),
        ("create", "item-2"),
    ]
    # Closing the transaction pushes once.
    api.commit_transaction()
    assert board.events[-1] == ("push", "commit-1", "Batch")


def test_apply_update_and_remove_respect_transaction():
    board = _FakeBoard()
    api = _make_api(board)
    api.begin_transaction("Refactor")
    api._apply_update(board, ["fp1"], "Moved fp1")
    api._apply_remove(board, ["fp2"], "Deleted fp2")
    api.commit_transaction()
    assert board.events == [
        ("begin", "commit-1"),
        ("update", ("fp1",)),
        ("remove", ("fp2",)),
        ("push", "commit-1", "Refactor"),
    ]


def test_begin_transaction_refuses_to_nest():
    board = _FakeBoard()
    api = _make_api(board)
    api.begin_transaction("First")
    out = api.begin_transaction("Second")
    assert out["success"] is False
    assert "already open" in out["message"].lower()
    # Original commit handle untouched — no second begin_commit fired.
    assert sum(1 for e in board.events if e[0] == "begin") == 1


def test_commit_transaction_without_open_transaction_fails_cleanly():
    board = _FakeBoard()
    api = _make_api(board)
    out = api.commit_transaction()
    assert out["success"] is False
    assert "no open transaction" in out["message"].lower()
    # No push fired.
    assert not any(e[0] == "push" for e in board.events)


def test_rollback_transaction_without_open_transaction_fails_cleanly():
    board = _FakeBoard()
    api = _make_api(board)
    out = api.rollback_transaction()
    assert out["success"] is False
    # No drop fired.
    assert not any(e[0] == "drop" for e in board.events)


def test_rollback_clears_state_and_drops_commit():
    board = _FakeBoard()
    api = _make_api(board)
    api.begin_transaction("Tentative")
    api._apply_create(board, "item-x", "tentative create")
    api.rollback_transaction()
    # Drop fired; the tentative create rode the (now dropped) commit;
    # state cleared so a subsequent begin works.
    assert ("drop", "commit-1") in board.events
    assert api._current_commit is None
    second = api.begin_transaction("Second")
    assert second["success"] is True


def test_commit_description_override():
    board = _FakeBoard()
    api = _make_api(board)
    api.begin_transaction("Original label")
    api.commit_transaction("Overridden label")
    push = next(e for e in board.events if e[0] == "push")
    assert push[2] == "Overridden label"


def test_get_transaction_status_reflects_state():
    board = _FakeBoard()
    api = _make_api(board)
    assert api.get_transaction_status() == {
        "success": True,
        "open": False,
        "description": None,
    }
    api.begin_transaction("Active")
    status = api.get_transaction_status()
    assert status["open"] is True
    assert status["description"] == "Active"


def test_apply_create_returns_server_assigned_kiid_outside_transaction():
    """kipy doesn't mutate the input wrapper; it returns a fresh wrapper
    with the server-assigned KIID. _apply_create must read the id from
    the *returned* list, not the input."""
    board = _FakeBoard()
    api = _make_api(board)

    class _LocalInput:
        id = ""  # local wrapper starts with empty id (matches reality)

    returned_id = api._apply_create(board, _LocalInput(), "Added shape")
    # First create → kiid-1. If _apply_create reads input.id instead, this
    # returns "" — the regression we're guarding against.
    assert returned_id == "kiid-1"


def test_apply_create_returns_server_assigned_kiid_inside_transaction():
    """Same contract holds when piggy-backing on an open transaction."""
    board = _FakeBoard()
    api = _make_api(board)
    api.begin_transaction("Batch")

    class _LocalInput:
        id = ""

    first = api._apply_create(board, _LocalInput(), "first")
    second = api._apply_create(board, _LocalInput(), "second")
    assert (first, second) == ("kiid-1", "kiid-2")
    # No per-call push fired — both rode the transaction's commit.
    assert sum(1 for e in board.events if e[0] == "begin") == 1
    assert not any(e[0] == "push" for e in board.events)


def test_begin_transaction_preserves_explicit_empty_description():
    """An empty-string label is the caller's choice — must not be
    silently replaced with the default."""
    board = _FakeBoard()
    api = _make_api(board)
    out = api.begin_transaction("")
    assert out["success"] is True
    assert out["description"] == ""
    assert api._current_commit_description == ""


def test_begin_transaction_none_uses_default_label():
    board = _FakeBoard()
    api = _make_api(board)
    out = api.begin_transaction(None)
    assert out["description"] == "MCP Operation"


def test_commit_transaction_explicit_empty_overrides_begin_label():
    """Three-state precedence: explicit override (incl. '') > begin label > default."""
    board = _FakeBoard()
    api = _make_api(board)
    api.begin_transaction("From begin")
    out = api.commit_transaction("")
    assert out["description"] == ""
    push = next(e for e in board.events if e[0] == "push")
    assert push[2] == ""
