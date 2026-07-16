"""D3: mutation success messages must not claim '(visible in KiCAD UI)' while a
transaction is open — the change is buffered until commit_transaction.

These tests cover the shared ``visibility_suffix`` helper (handlers.transactions)
that the IPC mutation handlers should append instead of the hard-coded literal.
See the P5b report for the cross-package call sites still to be switched over.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from handlers.transactions import visibility_suffix  # noqa: E402


class _FakeBoardApi:
    def __init__(self, current_commit=None):
        # Mirrors IPCBoardAPI: _current_commit is the open-transaction marker.
        self._current_commit = current_commit


class _FakeIface:
    def __init__(self, current_commit=None, has_api=True):
        self.ipc_board_api = _FakeBoardApi(current_commit) if has_api else None


def test_suffix_visible_when_no_transaction():
    assert visibility_suffix(_FakeIface(current_commit=None)) == "(visible in KiCAD UI)"


def test_suffix_pending_when_transaction_open():
    suffix = visibility_suffix(_FakeIface(current_commit=object()))
    assert "pending" in suffix.lower()
    assert "commit_transaction" in suffix
    # Must NOT falsely claim immediate UI visibility mid-transaction.
    assert "visible in KiCAD UI" not in suffix


def test_suffix_safe_without_ipc_board_api():
    # SWIG-only iface (no ipc_board_api) — never a transaction, plain suffix.
    assert visibility_suffix(_FakeIface(has_api=False)) == "(visible in KiCAD UI)"


def test_suffix_safe_when_iface_missing_attr():
    class _Bare:
        pass

    assert visibility_suffix(_Bare()) == "(visible in KiCAD UI)"


# ---------------------------------------------------------------------------
# Integration-style: drive real IPC mutation handlers and prove their success
# message flips from "(visible in KiCAD UI)" to the pending-in-transaction
# wording when a transaction is open (the D3 wiring that appended
# visibility_suffix at the 16 call sites).
# ---------------------------------------------------------------------------


class _RecordingBoardApi:
    """Minimal IPCBoardAPI stand-in: an explicit ``_current_commit`` marker
    plus the couple of mutation entry points the handlers under test call.

    A bare MagicMock can't be used here because ``getattr(api,
    "_current_commit")`` would auto-create a truthy child mock, making every
    call look like it's inside a transaction.  This makes the marker explicit.
    """

    def __init__(self, current_commit=None):
        self._current_commit = current_commit

    def add_track(self, **kwargs):
        return True

    def delete_component(self, **kwargs):
        return True


class _HandlerIface:
    def __init__(self, current_commit=None):
        self.ipc_board_api = _RecordingBoardApi(current_commit)


def test_route_trace_message_pending_when_transaction_open():
    """handle_route_trace's success message must say 'pending' (not claim
    immediate UI visibility) while a transaction is open."""
    from handlers.ipc_fastpath._routing import handle_route_trace

    iface = _HandlerIface(current_commit=object())
    # net=None keeps the cross-net-short guard from running (needs a live board).
    out = handle_route_trace(
        iface, {"start": {"x": 0, "y": 0}, "end": {"x": 1, "y": 1}, "net": None}
    )

    assert out["success"] is True
    assert "pending" in out["message"].lower()
    assert "commit_transaction" in out["message"]
    assert "visible in KiCAD UI" not in out["message"]


def test_route_trace_message_visible_when_no_transaction():
    """Companion: with NO transaction open the default '(visible in KiCAD UI)'
    wording is preserved (assertions that hard-code the old literal expect this
    non-transaction default form)."""
    from handlers.ipc_fastpath._routing import handle_route_trace

    iface = _HandlerIface(current_commit=None)
    out = handle_route_trace(
        iface, {"start": {"x": 0, "y": 0}, "end": {"x": 1, "y": 1}, "net": None}
    )

    assert out["success"] is True
    assert out["message"] == "Added trace (visible in KiCAD UI)"


def test_delete_component_message_pending_when_transaction_open():
    """A second handler (handle_delete_component) proves the flip is wired at
    that call site too, not just for route_trace."""
    from handlers.ipc_fastpath._components import handle_delete_component

    iface = _HandlerIface(current_commit=object())
    out = handle_delete_component(iface, {"reference": "R1"})

    assert out["success"] is True
    assert "pending" in out["message"].lower()
    assert "commit_transaction" in out["message"]
    assert "visible in KiCAD UI" not in out["message"]
