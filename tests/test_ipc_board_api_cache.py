"""Regression tests: IPCBackend.get_board() must return a cached instance.

The dispatcher calls _try_enable_ipc_backend() → _refresh_ipc_board_api() →
get_board() on EVERY IPC-capable command.  When get_board() handed out a
fresh IPCBoardAPI each time, per-instance state was silently dropped between
dispatches — most visibly the open transaction handle (_current_commit):
begin_transaction stored it on instance A, the next mutation ran on a new
instance B with _current_commit=None, opened a second KiCad commit, and was
refused with 'client already has a commit in progress' while
get_transaction_status (instance C) reported open=false.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from kicad_api.ipc_backend import IPCBackend  # noqa: E402


def _connected_backend() -> IPCBackend:
    be = IPCBackend()
    be._kicad = MagicMock(name="kipy_client")
    be._connected = True
    return be


def test_get_board_returns_cached_instance() -> None:
    be = _connected_backend()
    assert be.get_board() is be.get_board()


def test_transaction_state_survives_get_board_roundtrip() -> None:
    be = _connected_backend()
    board_api = be.get_board()
    commit_handle = object()
    board_api._current_commit = commit_handle

    # A later dispatch fetches the board API again — the open transaction
    # must still be there.
    assert be.get_board()._current_commit is commit_handle


def test_disconnect_invalidates_cache() -> None:
    be = _connected_backend()
    first = be.get_board()
    be.disconnect()

    be._kicad = MagicMock(name="kipy_client_2")
    be._connected = True
    second = be.get_board()

    assert first is not second


def test_kicad_client_swap_recreates_board_api() -> None:
    """A reconnect that replaces _kicad without calling disconnect must not
    keep handing out a board API bound to the dead client object."""
    be = _connected_backend()
    first = be.get_board()

    be._kicad = MagicMock(name="kipy_client_after_reconnect")
    second = be.get_board()

    assert first is not second
    assert second._kicad is be._kicad
