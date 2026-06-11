"""Regression tests: reconcile_backends(swig_to_ipc) must detect external
(.kicad_pcb edited outside the MCP) disk changes.

Previously the handler keyed only off the runtime ``_swig_writes_landed``
flag, so a file edited by a text editor / git / script reported
'Backends are already in sync' and KiCad kept rendering its stale memory.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from handlers.ui import handle_reconcile_backends  # noqa: E402


def _iface(
    *,
    flag: bool = False,
    ipc_pending: bool = False,
    disk_sig=(1, "aaa"),
    recorded_sig=(1, "aaa"),
) -> MagicMock:
    iface = MagicMock()
    iface._swig_writes_landed = flag
    iface._ipc_writes_pending = ipc_pending
    iface.board.GetFileName.return_value = "/tmp/board.kicad_pcb"
    iface._board_disk_signature = recorded_sig
    iface._disk_signature.return_value = disk_sig
    iface.ensure_ipc.return_value = (True, "")
    iface.ipc_board_api.revert.return_value = True
    iface._safe_load_board.return_value = MagicMock(name="reloaded_board")
    return iface


def test_noop_when_flag_clear_and_disk_unchanged() -> None:
    iface = _iface()
    result = handle_reconcile_backends(iface, {"direction": "swig_to_ipc"})
    assert result["success"] is True
    assert result.get("noop") is True
    iface.ipc_board_api.revert.assert_not_called()


def test_external_disk_edit_triggers_revert_and_swig_reload() -> None:
    iface = _iface(disk_sig=(2, "bbb"), recorded_sig=(1, "aaa"))

    result = handle_reconcile_backends(iface, {"direction": "swig_to_ipc"})

    assert result["success"] is True
    assert result.get("externalDiskChange") is True
    assert "ipc_revert" in result["stepsTaken"]
    assert "swig_reload" in result["stepsTaken"]
    iface.ipc_board_api.revert.assert_called_once()
    iface._safe_load_board.assert_called_once_with("/tmp/board.kicad_pcb")
    iface._record_board_signature.assert_called_once_with("/tmp/board.kicad_pcb")
    assert iface._swig_writes_landed is False
    assert iface._ipc_writes_pending is False


def test_flag_path_still_reverts_without_swig_reload() -> None:
    iface = _iface(flag=True)

    result = handle_reconcile_backends(iface, {"direction": "swig_to_ipc"})

    assert result["success"] is True
    assert result.get("externalDiskChange") is False
    assert result["stepsTaken"] == ["ipc_revert"]
    iface._safe_load_board.assert_not_called()


def test_external_edit_plus_ipc_pending_is_two_sided_conflict() -> None:
    iface = _iface(disk_sig=(2, "bbb"), recorded_sig=(1, "aaa"), ipc_pending=True)

    result = handle_reconcile_backends(iface, {"direction": "swig_to_ipc"})

    assert result["success"] is False
    assert result.get("needs_manual_action") is True
    iface.ipc_board_api.revert.assert_not_called()
