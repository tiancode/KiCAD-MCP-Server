"""Regression test: direct SWIG saves must mark cross-backend divergence.

FreeroutingCommands (autoroute / import_ses) saves the routed board straight
to disk through its signature callback.  When that callback only recorded the
disk signature, ``_swig_writes_landed`` stayed False — ``reconcile_backends``
reported 'already in sync' and the next ``ipc_save_board`` overwrote the
autoroute results with KiCad's stale in-memory board.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from kicad_interface import KiCADInterface  # noqa: E402


def test_on_swig_direct_save_records_signature_and_sets_flag() -> None:
    iface = MagicMock()
    iface._swig_writes_landed = False

    KiCADInterface._on_swig_direct_save(iface, "/tmp/board.kicad_pcb")

    iface._record_board_signature.assert_called_once_with("/tmp/board.kicad_pcb")
    assert iface._swig_writes_landed is True


def test_freerouting_commands_wired_to_direct_save_bookkeeping() -> None:
    """The constructor must pass _on_swig_direct_save (not the bare
    signature recorder) as FreeroutingCommands' save callback."""
    import inspect

    src = inspect.getsource(KiCADInterface.__init__)
    assert "FreeroutingCommands(" in src
    assert "signature_callback=self._on_swig_direct_save" in src


def test_board_mutating_commands_cover_known_mutators() -> None:
    """Audit lock (2026-06-11): every command that mutates the SWIG board
    must be in _BOARD_MUTATING_COMMANDS, or its changes are never
    auto-saved and the next swig_reload silently drops them (observed with
    add_gnd_stitching_vias: 14 placed vias vanished on reconcile)."""
    required = {
        "add_gnd_stitching_vias",
        "add_layer",
        "align_components",
        "copy_routing_pattern",
        "duplicate_component",
        "edit_component",
        # 2026-07: edits pads of a placed footprint (annular-ring repair);
        # without auto-save the repaired pads vanish on the next reload.
        "edit_component_pad",
        "modify_trace",
        "place_component_array",
        "route_differential_pair",
        "set_board_size",
        "set_design_rules",
        "edit_copper_pour",
        "delete_copper_pour",
    }
    missing = required - KiCADInterface._BOARD_MUTATING_COMMANDS
    assert not missing, f"Mutating commands missing from auto-save set: {sorted(missing)}"
