"""F7 regression: manage_kicad_ui(action="launch") must actually launch.

An explicit launch request means launching IS the intent, so ``autoLaunch``
defaults ON for the launch action (unlike the passive IPC-required auto-open,
which is opt-in via KICAD_AUTO_LAUNCH=true).  A hard opt-out
(``autoLaunch:false`` param or env ``KICAD_AUTO_LAUNCH=false``) must return
``success:false`` with a clear message — not the old ``success:true`` masking a
silent no-op.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _make_iface():
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = False
    iface.ipc_backend = None
    iface._try_enable_ipc_backend = lambda **kw: False  # type: ignore[assignment]
    iface._backend_status = lambda: {  # type: ignore[assignment]
        "backend": "swig",
        "realtime_sync": False,
        "ipc_connected": False,
        "capabilities": {},
    }
    return iface


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)


def _patch_check(monkeypatch, return_value):
    """Patch handlers.ui.check_and_launch_kicad and capture its call."""
    from handlers import ui as ui_handler

    fake = MagicMock(return_value=dict(return_value))
    monkeypatch.setattr(ui_handler, "check_and_launch_kicad", fake)
    return fake


def test_explicit_launch_defaults_autolaunch_on(monkeypatch):
    """No autoLaunch param, no env → the launch action defaults to ON and
    actually attempts the launch (auto_launch=True passed downstream)."""
    from handlers import ui as ui_handler

    fake = _patch_check(
        monkeypatch,
        {"running": True, "launched": True, "alreadyRunning": False, "message": "launched"},
    )
    iface = _make_iface()

    out = ui_handler.handle_launch_kicad_ui(iface, {"projectPath": "/tmp/x/x.kicad_pro"})

    # auto_launch must have resolved True (the second positional arg).
    assert fake.call_args[0][1] is True
    assert out["success"] is True
    assert out["launched"] is True


def test_env_hard_optout_returns_failure(monkeypatch):
    """KICAD_AUTO_LAUNCH=false is a hard opt-out: the launch action must NOT
    launch, and must report success:false with an actionable message rather
    than masking the no-op as success:true."""
    from handlers import ui as ui_handler

    monkeypatch.setenv("KICAD_AUTO_LAUNCH", "false")
    fake = _patch_check(
        monkeypatch,
        {
            "running": False,
            "launched": False,
            "alreadyRunning": False,
            "message": "KiCAD is not running (auto-launch disabled)",
        },
    )
    iface = _make_iface()

    out = ui_handler.handle_launch_kicad_ui(iface, {"projectPath": "/tmp/x/x.kicad_pro"})

    assert fake.call_args[0][1] is False  # auto_launch suppressed
    assert out["success"] is False
    assert "KICAD_AUTO_LAUNCH=false" in out["message"]


def test_explicit_param_false_returns_failure(monkeypatch):
    """autoLaunch:false param opts out too → success:false with a message that
    names the param."""
    from handlers import ui as ui_handler

    fake = _patch_check(
        monkeypatch,
        {"running": False, "launched": False, "alreadyRunning": False, "message": "not running"},
    )
    iface = _make_iface()

    out = ui_handler.handle_launch_kicad_ui(
        iface, {"projectPath": "/tmp/x/x.kicad_pro", "autoLaunch": False}
    )

    assert fake.call_args[0][1] is False
    assert out["success"] is False
    assert "autoLaunch:false" in out["message"]


def test_env_optout_wins_over_explicit_true(monkeypatch):
    """env KICAD_AUTO_LAUNCH=false is a HARD opt-out — it wins even when the
    caller passes autoLaunch:true."""
    from handlers import ui as ui_handler

    monkeypatch.setenv("KICAD_AUTO_LAUNCH", "false")
    fake = _patch_check(
        monkeypatch,
        {"running": False, "launched": False, "alreadyRunning": False, "message": "not running"},
    )
    iface = _make_iface()

    out = ui_handler.handle_launch_kicad_ui(
        iface, {"projectPath": "/tmp/x/x.kicad_pro", "autoLaunch": True}
    )

    assert fake.call_args[0][1] is False
    assert out["success"] is False


def test_already_running_is_success_not_false(monkeypatch):
    """When KiCad is already running the launch action is a success even though
    launched=False — the false-failure override must not fire."""
    from handlers import ui as ui_handler

    _patch_check(
        monkeypatch,
        {
            "running": True,
            "launched": False,
            "alreadyRunning": True,
            "processes": [],
            "message": "KiCAD is already running",
        },
    )
    iface = _make_iface()
    # Avoid the file-open forward path doing real work.
    monkeypatch.setattr(ui_handler, "_forward_file_open_to_running_kicad", lambda i, p: {})

    out = ui_handler.handle_launch_kicad_ui(iface, {})

    assert out["success"] is True


def test_launch_attempted_but_failed_reports_failure(monkeypatch):
    """auto_launch True but the process didn't come up → success:false with the
    original 'Failed to launch' message (not the opt-out message)."""
    from handlers import ui as ui_handler

    fake = _patch_check(
        monkeypatch,
        {
            "running": False,
            "launched": False,
            "alreadyRunning": False,
            "message": "Failed to launch KiCAD",
        },
    )
    iface = _make_iface()

    out = ui_handler.handle_launch_kicad_ui(iface, {})

    assert fake.call_args[0][1] is True  # launch WAS attempted
    assert out["success"] is False
    # Keeps the downstream failure message, does not overwrite with opt-out text.
    assert out["message"] == "Failed to launch KiCAD"
