"""Regression tests for refill_zones' SWIG-path refusal.

User concern: the tool description already warned "SWIG path has known
segfault risk" but the handler still attempted the fill (subprocess-
isolated, but the result can be silently wrong).  Putting the risk on
the user instead of refusing is the wrong default.

The SWIG handler now refuses by default and requires ``force: true`` to
attempt the subprocess fill.  The default-refuse response carries
``requires_ipc: True`` plus a structured recommendation.  The
auto-refill wrapper from ``add_copper_pour`` gracefully degrades to
``refillStatus: deferred_after_failure`` and the user-facing pour
operation still succeeds.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _iface_with_board():
    """Build a KiCADInterface stand-in carrying a board mock for the
    GetAreaCount probe; otherwise None.  Tests that care about the
    board attribute supply it; tests that don't, omit it."""
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.board = None
    return iface


# ---------------------------------------------------------------------------
# Default: refuse without touching the board
# ---------------------------------------------------------------------------
def test_default_refuses_swig_path():
    from handlers.routing import handle_refill_zones

    iface = _iface_with_board()
    fake_board = MagicMock()
    fake_board.GetAreaCount.return_value = 3
    fake_board.GetFileName.return_value = "/tmp/demo.kicad_pcb"
    iface.board = fake_board

    out = handle_refill_zones(iface, {})

    assert out["success"] is False
    assert out["requires_ipc"] is True
    assert "ZONE_FILLER" in out["message"]
    assert "IPC API Server" in out["message"]
    assert "force=true" in out["message"]
    # Zone count is still reported so the caller knows the board state.
    assert out["zoneCount"] == 3
    # Recommendation gives a clear next step.
    assert "manage_kicad_ui(action=launch)" in out["recommendation"]
    # CRUCIALLY: the handler must not have touched the board.  No save
    # / subprocess call.  We verify via the board mock methods that DO
    # mutate state (the original handler called save_board_and_record).
    assert not fake_board.Save.called


def test_default_refusal_without_loaded_board_still_succeeds():
    """No board loaded — the refusal still fires without erroring;
    zoneCount is simply None."""
    from handlers.routing import handle_refill_zones

    iface = _iface_with_board()
    iface.board = None

    out = handle_refill_zones(iface, {})

    assert out["success"] is False
    assert out["requires_ipc"] is True
    assert out["zoneCount"] is None


# ---------------------------------------------------------------------------
# force=True: attempts the subprocess fill (existing legacy behaviour)
# ---------------------------------------------------------------------------
def test_force_true_attempts_subprocess_fill(monkeypatch, tmp_path):
    """``force=True`` falls back to the historical subprocess-isolated
    SWIG fill.  On success the response includes a ``warnings`` entry
    flagging the uncertainty so the agent knows to verify."""
    from handlers.routing import handle_refill_zones

    board_path = tmp_path / "demo.kicad_pcb"
    board_path.write_text("(kicad_pcb)\n", encoding="utf-8")

    iface = _iface_with_board()
    fake_board = MagicMock()
    fake_board.GetAreaCount.return_value = 2
    fake_board.GetFileName.return_value = str(board_path)
    iface.board = fake_board
    iface._save_board_and_record = MagicMock()
    iface._safe_load_board = MagicMock(return_value=MagicMock())
    iface._update_command_handlers = MagicMock()
    iface._record_board_signature = MagicMock()

    fake_result = MagicMock(returncode=0, stdout="ok", stderr="")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_result)

    out = handle_refill_zones(iface, {"force": True})

    assert out["success"] is True
    assert out["zoneCount"] == 2
    # warnings entry calls out that the result may not match KiCad's own.
    assert any("force=true" in w for w in out.get("warnings", []))


def test_force_true_subprocess_failure_returns_deferred(monkeypatch, tmp_path):
    """When the subprocess crashes or returns non-zero, the handler
    still fails gracefully — no exception bubbles, status is reported."""
    from handlers.routing import handle_refill_zones

    board_path = tmp_path / "demo.kicad_pcb"
    board_path.write_text("(kicad_pcb)\n", encoding="utf-8")

    iface = _iface_with_board()
    fake_board = MagicMock()
    fake_board.GetAreaCount.return_value = 1
    fake_board.GetFileName.return_value = str(board_path)
    iface.board = fake_board
    iface._save_board_and_record = MagicMock()
    iface._safe_load_board = MagicMock(return_value=MagicMock())
    iface._update_command_handlers = MagicMock()
    iface._record_board_signature = MagicMock()

    fake_result = MagicMock(returncode=1, stdout="", stderr="boom")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_result)

    out = handle_refill_zones(iface, {"force": True})

    assert out["success"] is False
    assert "press B" in out["message"]
    assert out["zoneCount"] == 1


# ---------------------------------------------------------------------------
# add_copper_pour auto-refill: gracefully degrades to deferred state
# ---------------------------------------------------------------------------
def test_add_copper_pour_auto_refill_degrades_when_swig_refuses(monkeypatch, tmp_path):
    """The add_copper_pour SWIG wrapper used to call handle_refill_zones
    expecting subprocess isolation to work.  With the new refusal it
    must gracefully mark the refill as deferred without failing the
    pour itself."""
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.board = None
    iface.routing_commands = MagicMock()
    iface.routing_commands.add_copper_pour = lambda params: {
        "success": True,
        "message": "Pour created",
        "pour": {"layer": "F.Cu"},
    }

    out = iface._add_copper_pour_with_optional_refill(
        {"layer": "F.Cu", "net": "GND", "autoRefill": True}
    )

    # Pour creation still succeeds — refill is a follow-up.
    assert out["success"] is True
    assert out["refillStatus"] == "deferred_after_failure"
    # The refusal message bubbles up as a warning so the agent sees why.
    warnings = out.get("warnings", [])
    assert any("Auto-refill failed" in w for w in warnings)
    assert any("KiCAD" in w or "kicad" in w.lower() for w in warnings)
