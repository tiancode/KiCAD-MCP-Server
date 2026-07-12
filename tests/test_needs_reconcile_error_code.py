"""Truthful errorCode on gate refusals (papercut 1, GD32 E2E).

A ``duplicate_component`` (SWIG-only) refused under IPC-dirty state carried
the correct ``needs_reconcile: true`` + ``direction`` shape but ALSO
``errorCode: "INTERNAL_ERROR"`` — enrich_failure had no branch for the
cross-backend gate, so the deliberate refusal fell through to the generic
classifier.  These tests pin the ``NEEDS_RECONCILE`` code (and that the
PCB-editor gate keeps its already-truthful ``PCB_EDITOR_REQUIRED``), with
the refusal shape passing through verbatim otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from utils.failure import enrich_failure  # noqa: E402


# ---------------------------------------------------------------------------
# enrich_failure unit behavior
# ---------------------------------------------------------------------------
def test_needs_reconcile_gets_truthful_code_and_hint():
    refusal = {
        "success": False,
        "needs_reconcile": True,
        "direction": "ipc_to_swig",
        "message": "IPC has unsaved changes ... Call `reconcile_backends` ...",
    }

    out = enrich_failure("duplicate_component", refusal)

    assert out["errorCode"] == "NEEDS_RECONCILE"
    assert "reconcile_backends" in out["hint"]
    assert "ipc_to_swig" in out["hint"]
    # Shape preserved verbatim.
    assert out["needs_reconcile"] is True
    assert out["direction"] == "ipc_to_swig"
    assert out["message"] == refusal["message"]
    # The input dict is not mutated (shared-constant safety).
    assert "errorCode" not in refusal


def test_needs_reconcile_without_direction_still_coded():
    out = enrich_failure(
        "place_component",
        {"success": False, "needs_reconcile": True, "message": "conflict"},
    )

    assert out["errorCode"] == "NEEDS_RECONCILE"
    assert "reconcile_backends" in out["hint"]


def test_needs_reconcile_preserves_handler_supplied_hint():
    out = enrich_failure(
        "place_component",
        {
            "success": False,
            "needs_reconcile": True,
            "direction": "swig_to_ipc",
            "message": "conflict",
            "hint": "custom next step",
        },
    )

    assert out["errorCode"] == "NEEDS_RECONCILE"
    assert out["hint"] == "custom next step"


def test_pcb_editor_gate_keeps_its_truthful_code():
    out = enrich_failure(
        "list_shapes",
        {
            "success": False,
            "needs_pcb_editor": True,
            "message": "'list_shapes' requires the PCB editor: ...",
        },
    )

    assert out["errorCode"] == "PCB_EDITOR_REQUIRED"
    assert out["needs_pcb_editor"] is True


# ---------------------------------------------------------------------------
# Dispatcher integration: the exact E2E repro (duplicate_component, SWIG-only,
# refused while IPC has unsaved changes)
# ---------------------------------------------------------------------------
def _make_iface():
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    iface.ipc_backend = MagicMock()
    iface.ipc_board_api = MagicMock()
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
    from kicad_interface import KiCADInterface
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr(KiCADProcessManager, "is_pcb_editor_running", lambda: True)
    monkeypatch.setattr(KiCADInterface, "_ipc_has_open_board_document", lambda self: True)


def test_duplicate_component_refusal_carries_needs_reconcile_code():
    iface = _make_iface()
    iface._ipc_writes_pending = True  # IPC dirty → SWIG mutation must refuse
    called = {"n": 0}

    def fake_duplicate(params):
        called["n"] += 1
        return {"success": True}

    iface.command_routes = {"duplicate_component": fake_duplicate}

    result = iface.handle_command("duplicate_component", {"reference": "R1"})

    assert result["success"] is False
    assert result["needs_reconcile"] is True
    assert result["direction"] == "ipc_to_swig"
    assert result["errorCode"] == "NEEDS_RECONCILE"
    assert result["errorCode"] != "INTERNAL_ERROR"
    assert "reconcile_backends" in result["hint"]
    assert called["n"] == 0


def test_pcb_editor_gate_refusal_via_dispatcher_keeps_code(monkeypatch):
    from kicad_interface import KiCADInterface

    iface = _make_iface()
    # No board document AND the auto-open self-heal fails → gate response.
    monkeypatch.setattr(KiCADInterface, "_ipc_has_open_board_document", lambda self: False)
    monkeypatch.setattr(KiCADInterface, "_try_auto_open_board", lambda self, **kw: False)

    result = iface.handle_command("place_component", {"reference": "R1"})

    assert result["success"] is False
    assert result["needs_pcb_editor"] is True
    assert result["errorCode"] == "PCB_EDITOR_REQUIRED"
