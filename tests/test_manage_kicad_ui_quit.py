"""D6: manage_kicad_ui gains a 'quit' action that terminates ONLY the KiCad GUI
the server itself launched (SIGTERM -> bounded wait -> SIGKILL), leaving an
externally started KiCad untouched, and reports every case truthfully.

Process interactions are fully mocked — no real KiCad is launched or signalled.
"""

import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from utils import kicad_process as kp  # noqa: E402

KiCADProcessManager = kp.KiCADProcessManager


# --------------------------------------------------------------------------
# terminate_launched
# --------------------------------------------------------------------------


def _reset_tracking(pids):
    KiCADProcessManager._launched_pids = set(pids)


def test_terminate_launched_graceful_sigterm(monkeypatch):
    _reset_tracking({1234})
    state = {"dead": False}
    sig_calls = []

    def fake_running():
        return set() if state["dead"] else {1234}

    def fake_signal(pid, sig):
        sig_calls.append((pid, sig))
        if sig == signal.SIGTERM:
            state["dead"] = True
        return True

    monkeypatch.setattr(KiCADProcessManager, "_running_gui_pids", staticmethod(fake_running))
    monkeypatch.setattr(KiCADProcessManager, "_signal_pid", staticmethod(fake_signal))

    out = KiCADProcessManager.terminate_launched(timeout_s=0.05)

    assert out["terminated"] == [1234]
    assert out["forced"] == []
    assert out["survived"] == []
    assert out["launchedGuiRunning"] is True
    assert out["externalGuiRunning"] is False
    assert (1234, signal.SIGTERM) in sig_calls
    # Never escalated to SIGKILL for a process that exited on SIGTERM.
    assert (1234, signal.SIGKILL) not in sig_calls
    # Confirmed-dead PID pruned from tracking.
    assert 1234 not in KiCADProcessManager._launched_pids


def test_terminate_launched_escalates_to_sigkill(monkeypatch):
    _reset_tracking({5555})
    state = {"killed": False}
    sig_calls = []

    def fake_running():
        # Ignores SIGTERM; only dies once SIGKILL has been sent.
        return set() if state["killed"] else {5555}

    def fake_signal(pid, sig):
        sig_calls.append((pid, sig))
        if sig == signal.SIGKILL:
            state["killed"] = True
        return True

    monkeypatch.setattr(KiCADProcessManager, "_running_gui_pids", staticmethod(fake_running))
    monkeypatch.setattr(KiCADProcessManager, "_signal_pid", staticmethod(fake_signal))

    out = KiCADProcessManager.terminate_launched(timeout_s=0.05)

    assert out["terminated"] == [5555]
    assert out["forced"] == [5555]
    assert out["survived"] == []
    assert (5555, signal.SIGTERM) in sig_calls
    assert (5555, signal.SIGKILL) in sig_calls
    assert 5555 not in KiCADProcessManager._launched_pids


def test_terminate_launched_survives_even_sigkill(monkeypatch):
    _reset_tracking({4242})
    sig_calls = []

    monkeypatch.setattr(KiCADProcessManager, "_running_gui_pids", staticmethod(lambda: {4242}))
    monkeypatch.setattr(
        KiCADProcessManager,
        "_signal_pid",
        staticmethod(lambda pid, sig: sig_calls.append((pid, sig)) or True),
    )

    out = KiCADProcessManager.terminate_launched(timeout_s=0.05)

    assert out["terminated"] == []
    assert out["survived"] == [4242]
    assert (4242, signal.SIGKILL) in sig_calls
    # Survivor kept in tracking so a retry can target it again.
    assert 4242 in KiCADProcessManager._launched_pids


def test_terminate_launched_external_gui_left_untouched(monkeypatch):
    _reset_tracking(set())
    sig_calls = []

    monkeypatch.setattr(KiCADProcessManager, "_running_gui_pids", staticmethod(lambda: {9999}))
    monkeypatch.setattr(
        KiCADProcessManager,
        "_signal_pid",
        staticmethod(lambda pid, sig: sig_calls.append((pid, sig)) or True),
    )

    out = KiCADProcessManager.terminate_launched(timeout_s=0.05)

    assert out["terminated"] == []
    assert out["externalGuiPids"] == [9999]
    assert out["externalGuiRunning"] is True
    assert out["launchedGuiRunning"] is False
    assert sig_calls == []  # never signalled a GUI we did not launch


def test_terminate_launched_none_running(monkeypatch):
    _reset_tracking(set())
    monkeypatch.setattr(KiCADProcessManager, "_running_gui_pids", staticmethod(lambda: set()))

    out = KiCADProcessManager.terminate_launched(timeout_s=0.05)

    assert out["terminated"] == []
    assert out["externalGuiPids"] == []
    assert out["launchedGuiRunning"] is False
    assert out["externalGuiRunning"] is False


def test_terminate_launched_already_exited_pruned(monkeypatch):
    _reset_tracking({777})
    # The GUI we launched has since exited on its own — no GUI processes at all.
    monkeypatch.setattr(KiCADProcessManager, "_running_gui_pids", staticmethod(lambda: set()))

    out = KiCADProcessManager.terminate_launched(timeout_s=0.05)

    assert out["alreadyExited"] == [777]
    assert out["terminated"] == []
    assert 777 not in KiCADProcessManager._launched_pids


def test_terminate_never_kills_reused_pid(monkeypatch):
    """A tracked PID that is NOT currently a KiCad GUI (reused by the OS) must
    not be signalled — only pids in the live GUI set are targeted."""
    _reset_tracking({321})
    sig_calls = []
    # 321 is tracked but the live GUI set only has an unrelated external pid.
    monkeypatch.setattr(KiCADProcessManager, "_running_gui_pids", staticmethod(lambda: {888}))
    monkeypatch.setattr(
        KiCADProcessManager,
        "_signal_pid",
        staticmethod(lambda pid, sig: sig_calls.append((pid, sig)) or True),
    )

    out = KiCADProcessManager.terminate_launched(timeout_s=0.05)

    assert sig_calls == []  # 321 never signalled
    assert out["alreadyExited"] == [321]
    assert out["externalGuiPids"] == [888]


def test_launch_records_pid(monkeypatch):
    """launch() records the spawned GUI PID so quit can find it later."""
    _reset_tracking(set())
    # Not already running (so launch() proceeds to spawn) and wait_for_start
    # off (so we skip the poll loop entirely — no real waiting).
    monkeypatch.setattr(KiCADProcessManager, "is_running", staticmethod(lambda: False))
    monkeypatch.setattr(KiCADProcessManager, "ensure_ipc_api_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(
        KiCADProcessManager, "get_executable_path", staticmethod(lambda: Path("/usr/bin/kicad"))
    )

    class _FakeProc:
        pid = 24680

    monkeypatch.setattr(kp.subprocess, "Popen", lambda *a, **k: _FakeProc())

    KiCADProcessManager.launch(wait_for_start=False)
    assert 24680 in KiCADProcessManager._launched_pids


# --------------------------------------------------------------------------
# handle_quit_kicad_ui
# --------------------------------------------------------------------------


def _make_iface(use_ipc=True, ipc_backend=None, ipc_board_api=None):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = use_ipc
    iface.ipc_backend = ipc_backend
    iface.ipc_board_api = ipc_board_api
    iface.board = None
    return iface


def _patch_terminate(monkeypatch, result):
    from handlers import ui

    monkeypatch.setattr(ui.KiCADProcessManager, "terminate_launched", lambda **kw: dict(result))
    return ui


def test_handle_quit_terminates_and_resets_ipc(monkeypatch):
    ui = _patch_terminate(
        monkeypatch,
        {
            "terminated": [42],
            "forced": [],
            "survived": [],
            "alreadyExited": [],
            "externalGuiPids": [],
            "launchedGuiRunning": True,
            "externalGuiRunning": False,
        },
    )
    iface = _make_iface(use_ipc=True, ipc_backend=object(), ipc_board_api=object())
    out = ui.handle_quit_kicad_ui(iface, {})
    assert out["success"] is True
    assert "42" in out["message"]
    # IPC dropped so the next call re-probes / falls back to SWIG.
    assert iface.use_ipc is False
    assert iface.ipc_backend is None
    assert iface.ipc_board_api is None
    # Truthful backend status merged in.
    assert out["backend"] == "swig"


def test_handle_quit_external_gui_not_ours(monkeypatch):
    ui = _patch_terminate(
        monkeypatch,
        {
            "terminated": [],
            "forced": [],
            "survived": [],
            "alreadyExited": [],
            "externalGuiPids": [1000],
            "launchedGuiRunning": False,
            "externalGuiRunning": True,
        },
    )
    iface = _make_iface(use_ipc=True, ipc_backend=object(), ipc_board_api=object())
    out = ui.handle_quit_kicad_ui(iface, {})
    assert out["success"] is True
    assert "did not launch it" in out["message"]
    # We didn't kill anything, so IPC must NOT be torn down.
    assert iface.use_ipc is True


def test_handle_quit_nothing_running(monkeypatch):
    ui = _patch_terminate(
        monkeypatch,
        {
            "terminated": [],
            "forced": [],
            "survived": [],
            "alreadyExited": [],
            "externalGuiPids": [],
            "launchedGuiRunning": False,
            "externalGuiRunning": False,
        },
    )
    iface = _make_iface(use_ipc=False, ipc_backend=None)
    out = ui.handle_quit_kicad_ui(iface, {})
    assert out["success"] is True
    assert "No KiCad GUI is running" in out["message"]


def test_handle_quit_survivor_reports_failure(monkeypatch):
    ui = _patch_terminate(
        monkeypatch,
        {
            "terminated": [],
            "forced": [],
            "survived": [66],
            "alreadyExited": [],
            "externalGuiPids": [],
            "launchedGuiRunning": True,
            "externalGuiRunning": False,
        },
    )
    iface = _make_iface(use_ipc=True, ipc_backend=object())
    out = ui.handle_quit_kicad_ui(iface, {})
    assert out["success"] is False
    assert "still running" in out["message"]


def test_handle_quit_already_exited(monkeypatch):
    ui = _patch_terminate(
        monkeypatch,
        {
            "terminated": [],
            "forced": [],
            "survived": [],
            "alreadyExited": [555],
            "externalGuiPids": [],
            "launchedGuiRunning": False,
            "externalGuiRunning": False,
        },
    )
    iface = _make_iface(use_ipc=False, ipc_backend=None)
    out = ui.handle_quit_kicad_ui(iface, {})
    assert out["success"] is True
    assert "already exited" in out["message"]
