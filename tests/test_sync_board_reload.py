"""Regression tests: sync_schematic_to_board must reload the SWIG board.

``sync_schematic_to_board`` rewrites the ``.kicad_pcb`` on disk (adds
footprints/nets, reassigns pad nets) and saves it.  The long-lived Python
process must then hold the *canonical* on-disk board, not a stale/in-place
proxy: after a successful sync the handler reloads via
``iface._safe_load_board``, re-records the disk signature (so
``reconcile_backends`` doesn't misreport an external change and the
dispatcher's follow-up auto-save is a harmless no-op), sets
``_swig_writes_landed`` (so a running KiCad's stale in-memory board is
reported truthfully), and reports ``boardReloaded`` in the response.

These tests pin that contract with the stubbed ``pcbnew`` (see
tests/conftest.py) — no real KiCAD install required.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from handlers.schematic_io._io import handle_sync_schematic_to_board  # noqa: E402

BOARD_PATH = "/proj/board.kicad_pcb"


def _make_board() -> MagicMock:
    """A SWIG-board stand-in with just enough surface for the handler."""
    board = MagicMock(name="board")
    board.GetFileName.return_value = BOARD_PATH
    board.GetFootprints.return_value = []  # no pads to (re)assign
    board.GetNetInfo.return_value.NetsByName.return_value = MagicMock()
    return board


def _make_iface(*, ipc_attached: bool, reload_ok: bool = True) -> MagicMock:
    """A KiCADInterface stand-in wired for the sync handler's happy path."""
    iface = MagicMock(name="iface")
    iface.board = _make_board()
    iface._swig_writes_landed = False
    iface.ipc_board_api = MagicMock(name="ipc_api") if ipc_attached else None
    # kicad-cli unavailable in this stubbed environment → the handler takes the
    # label/BFS fallback (kept as a MagicMock so we don't spawn a real export).
    iface._export_schematic_netlist_xml.return_value = None
    # No nets, no missing footprints → the sync mutates/saves but adds nothing.
    iface._build_hierarchical_pad_net_map.return_value = ({}, [])
    iface._add_missing_footprints_from_schematic.return_value = ([], [])
    # Reloaded board is a distinct sentinel so tests can assert the swap.
    if reload_ok:
        iface._reloaded_sentinel = MagicMock(name="reloaded_board")
        iface._safe_load_board.return_value = iface._reloaded_sentinel
    else:
        iface._safe_load_board.return_value = None
    return iface


@pytest.fixture()
def schematic(tmp_path: Any) -> str:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text("(kicad_sch)\n")
    return str(sch)


@pytest.mark.unit
class TestSyncReloadsSwigBoard:
    def test_successful_sync_reloads_board_and_records_signature(self, schematic: str) -> None:
        iface = _make_iface(ipc_attached=False)
        original_board = iface.board

        result = handle_sync_schematic_to_board(iface, {"schematicPath": schematic})

        assert result["success"] is True
        assert result["boardReloaded"] is True
        # Reload came from disk at the board's own path.
        iface._safe_load_board.assert_called_once_with(BOARD_PATH)
        # The in-memory board was swapped for the freshly-loaded one.
        assert iface.board is iface._reloaded_sentinel
        assert iface.board is not original_board
        # Handlers were re-pointed and the disk signature re-recorded so the
        # dispatcher's follow-up auto-save is a harmless no-op.
        iface._update_command_handlers.assert_called_once()
        iface._record_board_signature.assert_called_once_with(BOARD_PATH)

    def test_successful_sync_sets_swig_writes_landed(self, schematic: str) -> None:
        """SWIG landed new content on disk → the SWIG->IPC divergence flag
        must be set so get_backend_info / reconcile_backends report it and a
        later IPC save is gated."""
        iface = _make_iface(ipc_attached=False)

        result = handle_sync_schematic_to_board(iface, {"schematicPath": schematic})

        assert result["success"] is True
        assert iface._swig_writes_landed is True

    def test_reload_failure_keeps_in_place_board_and_reports_false(self, schematic: str) -> None:
        """If _safe_load_board can't recover the board, the handler must NOT
        drop iface.board to None — the in-place-mutated board already matches
        disk — and must report boardReloaded=False."""
        iface = _make_iface(ipc_attached=False, reload_ok=False)
        original_board = iface.board

        result = handle_sync_schematic_to_board(iface, {"schematicPath": schematic})

        assert result["success"] is True
        assert result["boardReloaded"] is False
        # Board reference untouched; never set to the None the reload returned.
        assert iface.board is original_board
        iface._update_command_handlers.assert_not_called()
        iface._record_board_signature.assert_not_called()
        # Disk still changed under a possible KiCad instance → flag stays truthful.
        assert iface._swig_writes_landed is True

    def test_no_reload_when_no_board_loaded(self) -> None:
        """A sync that fails before touching disk must not reload or flag."""
        iface = _make_iface(ipc_attached=False)
        iface.board = None  # no board, no boardPath → early failure

        result = handle_sync_schematic_to_board(iface, {})

        assert result["success"] is False
        assert "boardReloaded" not in result
        iface._safe_load_board.assert_not_called()
        iface._record_board_signature.assert_not_called()
        assert iface._swig_writes_landed is False

    def test_no_reload_when_sync_raises(self, schematic: str) -> None:
        """An exception mid-sync is caught and returned as success=False; the
        reload and divergence flag must not run on that path."""
        iface = _make_iface(ipc_attached=False)
        iface._add_missing_footprints_from_schematic.side_effect = RuntimeError("boom")

        result = handle_sync_schematic_to_board(iface, {"schematicPath": schematic})

        assert result["success"] is False
        assert "boardReloaded" not in result
        iface._safe_load_board.assert_not_called()
        assert iface._swig_writes_landed is False


@pytest.mark.unit
class TestSyncIpcStaleness:
    def test_ipc_attached_flags_stale(self, schematic: str) -> None:
        iface = _make_iface(ipc_attached=True)

        result = handle_sync_schematic_to_board(iface, {"schematicPath": schematic})

        assert result["success"] is True
        assert result["ipcStale"] is True
        assert "reconcile_backends" in result["ipcStaleHint"]

    def test_no_ipc_no_stale_note(self, schematic: str) -> None:
        iface = _make_iface(ipc_attached=False)

        result = handle_sync_schematic_to_board(iface, {"schematicPath": schematic})

        assert result["success"] is True
        assert "ipcStale" not in result
        assert "ipcStaleHint" not in result


@pytest.mark.unit
def test_sync_stays_classified_as_board_mutating() -> None:
    """Audit lock: sync_schematic_to_board must stay in _BOARD_MUTATING_COMMANDS.

    The handler now saves AND reloads the board itself, but membership is
    still correct — it gives the dispatcher's *pre-handler*
    _cross_backend_conflict(attempting="swig") gate a chance to refuse the
    sync when IPC has unsaved changes (which the sync would otherwise clobber
    by reading stale disk). The post-handler auto-save that also runs is a
    harmless no-op re-save of the freshly-reloaded, signature-aligned board.
    """
    from kicad_interface import KiCADInterface

    assert "sync_schematic_to_board" in KiCADInterface._BOARD_MUTATING_COMMANDS
