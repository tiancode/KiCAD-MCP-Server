"""Regression tests for the IPC-attach polling in ``_autolaunch_for_project``.

User report: ``create_project`` returned ``ipcAttached: false``
immediately after spawning KiCAD because the wxApp init takes a few
seconds and IPC isn't reachable yet.  The handler now polls
``_try_enable_ipc_backend`` for up to
``_AUTOLAUNCH_IPC_POLL_DEADLINE_S`` and, when the deadline expires,
surfaces ``retryAfterMs`` + a clear "wait N seconds and retry" warning
so the agent doesn't read a transient failure as a permanent broken
state.
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _bare_iface():
    from kicad_interface import KiCADInterface

    obj = KiCADInterface.__new__(KiCADInterface)
    obj.use_ipc = False
    obj.ipc_backend = None
    obj.ipc_board_api = None
    obj.board = None
    obj.command_routes = {}
    obj._board_disk_signature = None
    obj._current_project_path = None
    obj._last_auto_save_status = None
    obj._ipc_writes_pending = False
    obj._swig_writes_landed = False
    obj._ipc_change_callback_registered = False
    obj._current_board_path = lambda: None
    return obj


def _running_launch_info(launched: bool = True):
    return lambda path, auto_launch=True: {
        "running": True,
        "launched": launched,
        "alreadyRunning": not launched,
        "processes": [],
        "message": "KiCAD launched" if launched else "KiCAD already running",
    }


# ---------------------------------------------------------------------------
# Poll terminates fast when attach succeeds on the first try
# ---------------------------------------------------------------------------
def test_attach_succeeds_first_try_no_polling(monkeypatch, tmp_path):
    from handlers import project as project_handler
    from kicad_interface import KiCADInterface

    project_file = tmp_path / "demo.kicad_pro"
    project_file.write_text("(kicad_project)\n", encoding="utf-8")

    iface = _bare_iface()
    attempts = {"n": 0}

    def _attach(force=False):
        attempts["n"] += 1
        return True

    iface._try_enable_ipc_backend = _attach
    # Pretend KiCad has the board loaded — the pcbDocumentOpen branch
    # (separate from the attach poll) would otherwise add its own
    # warning text and shadow the test's "no retry hint" assertion.
    monkeypatch.setattr(KiCADInterface, "_ipc_has_open_board_document", lambda self: True)
    monkeypatch.setattr(
        project_handler, "check_and_launch_kicad", _running_launch_info(launched=True)
    )

    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    out = project_handler._autolaunch_for_project(iface, project_file, {})

    assert out["ipcAttached"] is True
    assert out["ipcAttachAttempts"] == 1
    # No sleep when first attempt landed.
    assert sleeps == []
    # No warning / retryAfterMs on success.
    assert out["warning"] is None
    assert "retryAfterMs" not in out


# ---------------------------------------------------------------------------
# Poll retries until success, reports the elapsed attempts
# ---------------------------------------------------------------------------
def test_attach_polls_until_kicad_finishes_booting(monkeypatch, tmp_path):
    """Simulate KiCad's wxApp init: attach returns False for the first
    3 polls, then True.  The handler must keep polling and ultimately
    report ``ipcAttached: true`` with ``ipcAttachAttempts: 4``."""
    from handlers import project as project_handler

    project_file = tmp_path / "demo.kicad_pro"
    project_file.write_text("(kicad_project)\n", encoding="utf-8")

    iface = _bare_iface()
    attempts = {"n": 0}

    def _attach(force=False):
        attempts["n"] += 1
        return attempts["n"] >= 4  # first 3 fail, 4th succeeds

    iface._try_enable_ipc_backend = _attach
    monkeypatch.setattr(
        project_handler, "check_and_launch_kicad", _running_launch_info(launched=True)
    )

    # Speed up the test: skip the real sleep, count calls instead.
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    out = project_handler._autolaunch_for_project(iface, project_file, {})

    assert out["ipcAttached"] is True
    assert out["ipcAttachAttempts"] == 4
    # Three sleeps between the four attempts.
    assert len(sleeps) == 3
    # No retry hint when we eventually attach.
    assert "retryAfterMs" not in out


# ---------------------------------------------------------------------------
# Poll exhausted: surface retryAfterMs + an actionable warning
# ---------------------------------------------------------------------------
def test_attach_poll_exhausted_surfaces_retry_hint(monkeypatch, tmp_path):
    from handlers import project as project_handler

    project_file = tmp_path / "demo.kicad_pro"
    project_file.write_text("(kicad_project)\n", encoding="utf-8")

    iface = _bare_iface()
    iface._try_enable_ipc_backend = lambda force=False: False
    monkeypatch.setattr(
        project_handler, "check_and_launch_kicad", _running_launch_info(launched=True)
    )
    # Shrink the deadline so the test doesn't actually wait 10 s.
    monkeypatch.setattr(project_handler, "_AUTOLAUNCH_IPC_POLL_DEADLINE_S", 0.1)
    monkeypatch.setattr(project_handler, "_AUTOLAUNCH_IPC_POLL_INTERVAL_S", 0.02)

    out = project_handler._autolaunch_for_project(iface, project_file, {})

    assert out["ipcAttached"] is False
    # Multiple attempts were made.
    assert out["ipcAttachAttempts"] >= 2
    assert out["ipcAttachElapsedMs"] >= 100
    # Actionable retry hint.
    assert out["retryAfterMs"] == 5000
    warning = out["warning"]
    assert warning is not None
    assert "wait" in warning.lower()
    assert "get_backend_info" in warning
    assert "Preferences" in warning  # second-level fallback when retry doesn't help


# ---------------------------------------------------------------------------
# When KiCad was ALREADY running, no polling — single-shot is plenty
# ---------------------------------------------------------------------------
def test_already_running_skips_poll(monkeypatch, tmp_path):
    from handlers import project as project_handler

    project_file = tmp_path / "demo.kicad_pro"
    project_file.write_text("(kicad_project)\n", encoding="utf-8")

    iface = _bare_iface()
    attempts = {"n": 0}

    def _attach(force=False):
        attempts["n"] += 1
        return False  # fails once

    iface._try_enable_ipc_backend = _attach
    monkeypatch.setattr(
        project_handler, "check_and_launch_kicad", _running_launch_info(launched=False)
    )

    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    out = project_handler._autolaunch_for_project(iface, project_file, {})

    # Single attempt, no sleep, no retry hint (KiCad isn't booting —
    # IPC just isn't reachable, that's a different problem).
    assert out["ipcAttached"] is False
    assert out["ipcAttachAttempts"] == 1
    assert sleeps == []
    assert "retryAfterMs" not in out
    # ``warning`` stays None — the cross-mismatch + pcbDocumentOpen
    # branches own their own warning text; the new retry hint only
    # fires on the freshly-launched case where polling makes sense.
    assert out["warning"] is None
