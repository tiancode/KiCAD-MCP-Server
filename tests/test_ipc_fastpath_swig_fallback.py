"""Tests for cross-backend bookkeeping of IPC fast-path SWIG fallbacks.

Regression context: handle_delete_trace (IPC path) fell back to the SWIG
implementation without the dispatcher's bookkeeping — no conflict gate, no
auto-save, no ``_swig_writes_landed`` flag.  The deleted traces lived on in
KiCad's memory and the next ``ipc_save_board`` resurrected them on disk.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from handlers.ipc_fastpath._common import swig_fallback_mutation  # noqa: E402
from handlers.ipc_fastpath._routing import handle_delete_trace  # noqa: E402


def _iface(*, conflict: Dict[str, Any] | None = None, saved: bool = True) -> MagicMock:
    iface = MagicMock(name="iface")
    iface._cross_backend_conflict.return_value = conflict
    iface._auto_save_board.return_value = (
        {"saved": True, "boardPath": "/tmp/x.kicad_pcb", "backup": None}
        if saved
        else {"saved": False, "warning": "refused: changed externally"}
    )
    iface._swig_writes_landed = False
    return iface


def test_success_saves_and_flags_divergence() -> None:
    iface = _iface()
    swig_op = MagicMock(return_value={"success": True, "message": "Deleted 6 traces"})

    result = swig_fallback_mutation(iface, "delete_trace", swig_op, {"net": "USB_DP"})

    assert result["success"] is True
    swig_op.assert_called_once_with({"net": "USB_DP"})
    iface._auto_save_board.assert_called_once()
    assert iface._swig_writes_landed is True
    # Caller is told the IPC side is now stale
    assert any("reconcile_backends" in w for w in result["warnings"])


def test_conflict_gate_refuses_before_mutation() -> None:
    conflict = {"success": False, "needs_reconcile": True, "direction": "ipc_to_swig"}
    iface = _iface(conflict=conflict)
    swig_op = MagicMock()

    result = swig_fallback_mutation(iface, "delete_trace", swig_op, {})

    assert result is conflict
    swig_op.assert_not_called()
    iface._auto_save_board.assert_not_called()
    assert iface._swig_writes_landed is False


def test_failed_mutation_does_not_save_or_flag() -> None:
    iface = _iface()
    swig_op = MagicMock(return_value={"success": False, "message": "no such net"})

    result = swig_fallback_mutation(iface, "delete_trace", swig_op, {})

    assert result["success"] is False
    iface._auto_save_board.assert_not_called()
    assert iface._swig_writes_landed is False


def test_refused_autosave_surfaces_warning_without_flag() -> None:
    iface = _iface(saved=False)
    swig_op = MagicMock(return_value={"success": True})

    result = swig_fallback_mutation(iface, "delete_trace", swig_op, {})

    assert result["success"] is True
    assert iface._swig_writes_landed is False
    assert result["autoSave"]["saved"] is False
    assert any("refused" in w for w in result["warnings"])


def test_handle_delete_trace_routes_through_fallback_helper() -> None:
    iface = _iface()
    iface.routing_commands.delete_trace.return_value = {"success": True}

    result = handle_delete_trace(iface, {"net": "USB_DP"})

    assert result["success"] is True
    iface.routing_commands.delete_trace.assert_called_once_with({"net": "USB_DP"})
    assert iface._swig_writes_landed is True
