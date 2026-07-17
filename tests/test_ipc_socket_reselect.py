"""Tests for IPCBackend.reselect_preferring_board.

A second KiCad instance (standalone ``pcbnew <board>``) serves its own
``api-<pid>.sock``; a client stuck on the board-less first instance must be
able to re-run the connect-time socket selection (which prefers an instance
with a ``.kicad_pcb`` document) instead of reporting "no board" forever.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from kicad_api.ipc_backend import IPCBackend  # noqa: E402
from kicad_api.ipc_backend import _backend as backend_mod  # noqa: E402


def _connected_backend() -> IPCBackend:
    be = IPCBackend()
    be._kicad = MagicMock(name="kipy_client")
    be._connected = True
    return be


def test_noop_when_board_already_open(monkeypatch: pytest.MonkeyPatch) -> None:
    be = _connected_backend()
    monkeypatch.setattr(backend_mod, "has_open_pcb_document", lambda k: True)
    be.connect = MagicMock(name="connect")  # type: ignore[assignment]

    assert be.reselect_preferring_board() is True
    be.connect.assert_not_called()


def test_reconnects_to_board_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    be = _connected_backend()
    # Current connection: no board. After reconnect: board present.
    states = iter([False, True])
    monkeypatch.setattr(backend_mod, "has_open_pcb_document", lambda k: next(states))

    def _fake_connect() -> None:
        be._kicad = MagicMock(name="kipy_client_board_instance")
        be._connected = True

    be.connect = MagicMock(side_effect=_fake_connect)  # type: ignore[assignment]

    assert be.reselect_preferring_board() is True
    be.connect.assert_called_once()


def test_false_when_no_board_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    be = _connected_backend()
    monkeypatch.setattr(backend_mod, "has_open_pcb_document", lambda k: False)

    def _fake_connect() -> None:
        be._kicad = MagicMock(name="kipy_client_still_no_board")
        be._connected = True

    be.connect = MagicMock(side_effect=_fake_connect)  # type: ignore[assignment]

    assert be.reselect_preferring_board() is False


def test_false_when_reconnect_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    be = _connected_backend()
    monkeypatch.setattr(backend_mod, "has_open_pcb_document", lambda k: False)
    be.connect = MagicMock(side_effect=ConnectionError("gone"))  # type: ignore[assignment]

    assert be.reselect_preferring_board() is False
