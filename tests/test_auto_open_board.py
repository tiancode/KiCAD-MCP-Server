"""Tests for the board auto-open path that completes the IPC auto-launch chain.

``ensure_ipc`` could already launch KiCad when IPC was needed, but the
PCB-editor gate still bounced every board op back to the user when no
``.kicad_pcb`` document was open.  ``_try_auto_open_board`` heals that state
automatically (file-open forward → run_action → poll), with
``KICAD_AUTO_LAUNCH=false`` as the opt-out and a cooldown after failures so
gated calls don't repeatedly block on the poll timeout.
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _bare_iface():
    """Construct a KiCADInterface without running its real __init__."""
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = False
    iface.ipc_board_api = None
    iface.ipc_backend = None
    iface.board = None
    iface._current_project_path = None
    return iface


# ---------------------------------------------------------------------------
# _board_path_for_auto_open — board-path resolution
# ---------------------------------------------------------------------------
def test_board_path_prefers_loaded_board(tmp_path):
    board = tmp_path / "demo.kicad_pcb"
    board.write_text("(kicad_pcb)\n", encoding="utf-8")

    iface = _bare_iface()
    iface._current_board_path = lambda: str(board)

    assert iface._board_path_for_auto_open() == board


def test_board_path_falls_back_to_project_dir_sibling(tmp_path):
    (tmp_path / "demo.kicad_pro").write_text("(kicad_project)\n", encoding="utf-8")
    board = tmp_path / "demo.kicad_pcb"
    board.write_text("(kicad_pcb)\n", encoding="utf-8")
    # A second stray board must not confuse the .kicad_pro→.kicad_pcb pairing.
    (tmp_path / "scratch.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")

    iface = _bare_iface()
    iface._current_board_path = lambda: None
    iface._current_project_path = tmp_path

    assert iface._board_path_for_auto_open() == board


def test_board_path_none_when_nothing_known():
    iface = _bare_iface()
    iface._current_board_path = lambda: None

    assert iface._board_path_for_auto_open() is None


# ---------------------------------------------------------------------------
# _try_auto_open_board — behavior
# ---------------------------------------------------------------------------
def test_auto_open_short_circuits_when_document_already_open():
    iface = _bare_iface()
    iface._ipc_has_open_board_document = lambda: True
    iface._board_path_for_auto_open = MagicMock(side_effect=AssertionError("must not probe"))

    assert iface._try_auto_open_board() is True
    iface._board_path_for_auto_open.assert_not_called()


def test_auto_open_respects_env_optout(monkeypatch, tmp_path):
    monkeypatch.setenv("KICAD_AUTO_LAUNCH", "false")

    iface = _bare_iface()
    iface._ipc_has_open_board_document = lambda: False
    iface._board_path_for_auto_open = MagicMock(side_effect=AssertionError("must not attempt"))

    assert iface._try_auto_open_board() is False
    iface._board_path_for_auto_open.assert_not_called()


def test_auto_open_forwards_board_and_polls_until_open(monkeypatch, tmp_path):
    import handlers.ui as ui_handler

    board = tmp_path / "demo.kicad_pcb"
    board.write_text("(kicad_pcb)\n", encoding="utf-8")

    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)

    iface = _bare_iface()
    iface._current_board_path = lambda: str(board)
    iface._try_enable_ipc_backend = lambda force=False: True

    # Document closed before the forward, open after.
    state = {"open": False}
    iface._ipc_has_open_board_document = lambda: state["open"]

    forwarded = {}

    def _fake_forward(_iface, path):
        forwarded["path"] = path
        state["open"] = True
        return {"fileOpenForwarded": True, "fileOpenMethod": "spawn"}

    monkeypatch.setattr(ui_handler, "_forward_file_open_to_running_kicad", _fake_forward)

    assert iface._try_auto_open_board(timeout_s=2.0) is True
    assert forwarded["path"] == board
    # Success clears any cooldown.
    assert iface._auto_open_cooldown_until == 0.0


def test_auto_open_already_open_forward_short_circuits(monkeypatch, tmp_path):
    import handlers.ui as ui_handler

    board = tmp_path / "demo.kicad_pcb"
    board.write_text("(kicad_pcb)\n", encoding="utf-8")

    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)

    iface = _bare_iface()
    iface._current_board_path = lambda: str(board)
    iface._ipc_has_open_board_document = lambda: False  # gate probe says no

    monkeypatch.setattr(
        ui_handler,
        "_forward_file_open_to_running_kicad",
        lambda _iface, path: {"fileOpenForwarded": True, "fileOpenMethod": "already_open"},
    )

    assert iface._try_auto_open_board(timeout_s=1.0) is True


def test_auto_open_failure_arms_cooldown(monkeypatch, tmp_path):
    import handlers.ui as ui_handler

    board = tmp_path / "demo.kicad_pcb"
    board.write_text("(kicad_pcb)\n", encoding="utf-8")

    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)

    iface = _bare_iface()
    iface._current_board_path = lambda: str(board)
    iface._try_enable_ipc_backend = lambda force=False: True
    iface._ipc_has_open_board_document = lambda: False  # never opens

    calls = {"n": 0}

    def _fake_forward(_iface, path):
        calls["n"] += 1
        return {"fileOpenForwarded": True, "fileOpenMethod": "spawn"}

    monkeypatch.setattr(ui_handler, "_forward_file_open_to_running_kicad", _fake_forward)

    # First call: attempts, polls out the (short) timeout, fails, arms cooldown.
    assert iface._try_auto_open_board(timeout_s=1.0) is False
    assert calls["n"] == 1
    assert iface._auto_open_cooldown_until > time.monotonic()

    # Second call inside the cooldown: fails fast without re-forwarding.
    started = time.monotonic()
    assert iface._try_auto_open_board(timeout_s=10.0) is False
    assert time.monotonic() - started < 0.5
    assert calls["n"] == 1


def test_auto_open_no_route_returns_false_without_poll():
    """No board path known and no IPC connection: nothing to attempt — the
    call must fail fast (cooldown armed) instead of polling the timeout."""
    iface = _bare_iface()
    iface._current_board_path = lambda: None
    iface._ipc_has_open_board_document = lambda: False

    started = time.monotonic()
    assert iface._try_auto_open_board(timeout_s=10.0) is False
    assert time.monotonic() - started < 0.5


def test_auto_open_uses_run_action_when_no_board_path(monkeypatch):
    """KiCad has the right project loaded but we never learned the board
    path — the project-manager editPCB action family is the fallback."""
    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)

    iface = _bare_iface()
    iface._current_board_path = lambda: None

    state = {"open": False}
    iface._ipc_has_open_board_document = lambda: state["open"]

    invoked = []
    iface.ipc_backend = MagicMock()
    iface.ipc_backend.is_connected = lambda: True

    def _fake_run_action(action):
        invoked.append(action)
        state["open"] = True
        return {"success": True, "statusName": "RAS_OK"}

    iface.ipc_backend.run_action = _fake_run_action

    assert iface._try_auto_open_board(timeout_s=1.0) is True
    assert len(invoked) == 1


# ---------------------------------------------------------------------------
# ensure_ipc editor gate — auto-open heals the gate; failure keeps the
# canonical needs_pcb_editor contract.
# ---------------------------------------------------------------------------
def _connected_iface():
    iface = _bare_iface()
    iface.use_ipc = True
    iface.ipc_board_api = MagicMock()
    iface.ipc_backend = MagicMock()
    iface.ipc_backend.is_connected = lambda: True
    return iface


def test_ensure_ipc_editor_gate_healed_by_auto_open(monkeypatch):
    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)

    iface = _connected_iface()
    iface._ipc_has_open_board_document = lambda: False
    iface._try_auto_open_board = lambda timeout_s=15.0: True

    ok, reason = iface.ensure_ipc()

    assert ok is True
    assert reason == ""


def test_ensure_ipc_editor_gate_still_fires_when_auto_open_fails(monkeypatch):
    from kicad_interface import KiCADInterface

    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)

    iface = _connected_iface()
    iface._ipc_has_open_board_document = lambda: False
    iface._try_auto_open_board = lambda timeout_s=15.0: False

    ok, reason = iface.ensure_ipc()

    assert ok is False
    assert reason == KiCADInterface._pcb_editor_gate_reason()
    # require_ipc_board_op keys on reason equality to emit needs_pcb_editor.
    out = iface.require_ipc_board_op()
    assert out["needs_pcb_editor"] is True


def test_ensure_ipc_editor_gate_skips_auto_open_on_env_optout(monkeypatch):
    from kicad_interface import KiCADInterface

    monkeypatch.setenv("KICAD_AUTO_LAUNCH", "false")

    iface = _connected_iface()
    iface._ipc_has_open_board_document = lambda: False
    iface._try_auto_open_board = MagicMock(side_effect=AssertionError("must not attempt"))

    ok, reason = iface.ensure_ipc()

    assert ok is False
    assert reason == KiCADInterface._pcb_editor_gate_reason()
    iface._try_auto_open_board.assert_not_called()


def test_ensure_ipc_cold_launch_passes_board_path(monkeypatch, tmp_path):
    """The cold-start launch must point KiCad at the known board so the PCB
    editor opens directly instead of a bare project manager."""
    from kicad_interface import KiCADInterface
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)

    board = tmp_path / "demo.kicad_pcb"
    board.write_text("(kicad_pcb)\n", encoding="utf-8")

    iface = _bare_iface()
    iface._current_board_path = lambda: str(board)
    monkeypatch.setattr(KiCADInterface, "_try_enable_ipc_backend", lambda self, force=False: False)
    monkeypatch.setattr(KiCADProcessManager, "is_running", staticmethod(lambda: False))

    launch_args = {}

    def _fake_launch(project_path=None, wait_for_start=True):
        launch_args["project_path"] = project_path
        return False  # stop ensure_ipc right after the launch attempt

    monkeypatch.setattr(KiCADProcessManager, "launch", staticmethod(_fake_launch))

    ok, reason = iface.ensure_ipc(timeout_s=1.0)

    assert ok is False
    assert launch_args["project_path"] == board


# ---------------------------------------------------------------------------
# ensure_ipc "running but IPC not usable" branch — the bare project-manager
# "not ready to reply" state.  KiCad is up and owns the socket, but the
# reachable IPC serves no board, so ensure_ipc must NOT sit on the cold-launch
# poll loop; it goes straight to the bounded board auto-open self-heal.
# ---------------------------------------------------------------------------
def _no_cold_launch(monkeypatch):
    """Fail the test if ensure_ipc ever reaches the cold-launch poll loop —
    that path is only valid when KiCAD is NOT already running."""
    from utils.kicad_process import KiCADProcessManager

    def _boom(*_a, **_k):
        raise AssertionError("must not cold-launch when KiCAD is already running")

    monkeypatch.setattr(KiCADProcessManager, "launch", staticmethod(_boom))


def test_ensure_ipc_bare_pm_not_ready_self_heals_via_spawn(monkeypatch):
    """KiCad is up as a bare project manager: the reachable IPC answers 'not
    ready to reply', so the initial attach never connects.  ensure_ipc must
    classify this as reachable-but-no-board, run the board auto-open self-heal
    (pcbnew spawn), and return success once the board lands — all bounded, no
    30 s poll."""
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)
    monkeypatch.setattr(KiCADProcessManager, "is_running", staticmethod(lambda: True))
    _no_cold_launch(monkeypatch)

    iface = _bare_iface()
    state = {"connected": False, "board_open": False}
    # Initial attach against the bare PM fails; the post-self-heal attach
    # (against the spawned pcbnew) reflects the flipped state.
    iface._try_enable_ipc_backend = lambda force=False: state["connected"]
    iface._ipc_has_open_board_document = lambda: state["board_open"]

    heal_calls = {"n": 0}

    def _fake_auto_open(timeout_s=15.0):
        heal_calls["n"] += 1
        # pcbnew spawn landed the board and a live connection.
        state["connected"] = True
        state["board_open"] = True
        iface.use_ipc = True
        iface.ipc_board_api = MagicMock()
        return True

    iface._try_auto_open_board = _fake_auto_open

    started = time.monotonic()
    ok, reason = iface.ensure_ipc(timeout_s=30.0)

    assert ok is True
    assert reason == ""
    assert heal_calls["n"] == 1
    # Bounded: nowhere near the 30 s poll budget.
    assert time.monotonic() - started < 1.0


def test_ensure_ipc_bare_pm_gate_when_self_heal_cannot_open_board(monkeypatch):
    """When the self-heal can't open the board (no board path, spawn failed,
    board never appeared), a running-but-unusable KiCad yields the structured
    needs_pcb_editor gate — not a generic timeout."""
    from kicad_interface import KiCADInterface
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)
    monkeypatch.setattr(KiCADProcessManager, "is_running", staticmethod(lambda: True))
    _no_cold_launch(monkeypatch)

    iface = _bare_iface()
    iface._try_enable_ipc_backend = lambda force=False: False
    iface._ipc_has_open_board_document = lambda: False
    iface._try_auto_open_board = lambda timeout_s=15.0: False

    started = time.monotonic()
    ok, reason = iface.ensure_ipc(timeout_s=30.0)

    assert ok is False
    assert reason == KiCADInterface._pcb_editor_gate_reason()
    # Bounded: no cold-launch poll loop.
    assert time.monotonic() - started < 1.0
    # require_ipc_board_op keys on the reason to emit needs_pcb_editor.
    out = iface.require_ipc_board_op()
    assert out["needs_pcb_editor"] is True


def test_ensure_ipc_bare_pm_respects_auto_open_cooldown(monkeypatch):
    """With the auto-open cooldown armed (a prior failed spawn), a gated call
    must fail fast with the gate — never re-forward a file-open / re-spawn, and
    never poll the launch loop."""
    import handlers.ui as ui_handler
    from kicad_interface import KiCADInterface
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)
    monkeypatch.setattr(KiCADProcessManager, "is_running", staticmethod(lambda: True))
    _no_cold_launch(monkeypatch)
    # Prove the real _try_auto_open_board bails on the cooldown before it would
    # ever forward a file-open.
    monkeypatch.setattr(
        ui_handler,
        "_forward_file_open_to_running_kicad",
        MagicMock(side_effect=AssertionError("must not forward during cooldown")),
    )

    iface = _bare_iface()
    iface._try_enable_ipc_backend = lambda force=False: False
    iface._ipc_has_open_board_document = lambda: False
    iface._current_board_path = lambda: None
    # Arm the cooldown as if a spawn just failed.
    iface._auto_open_cooldown_until = time.monotonic() + 60.0

    started = time.monotonic()
    ok, reason = iface.ensure_ipc(timeout_s=30.0)

    assert ok is False
    assert reason == KiCADInterface._pcb_editor_gate_reason()
    assert time.monotonic() - started < 0.5


def test_ensure_ipc_bare_pm_no_self_heal_when_autolaunch_disabled(monkeypatch):
    """KICAD_AUTO_LAUNCH=false disables the self-heal spawn entirely: a
    running-but-unusable KiCad yields the 'enable IPC / open editor' message
    and never attempts to open a board."""
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setenv("KICAD_AUTO_LAUNCH", "false")
    monkeypatch.setattr(KiCADProcessManager, "is_running", staticmethod(lambda: True))

    iface = _bare_iface()
    iface._try_enable_ipc_backend = lambda force=False: False
    iface._try_auto_open_board = MagicMock(side_effect=AssertionError("must not self-heal"))

    ok, reason = iface.ensure_ipc(timeout_s=30.0)

    assert ok is False
    assert "not reachable" in reason
    iface._try_auto_open_board.assert_not_called()


def test_ensure_ipc_no_self_heal_when_board_already_open(monkeypatch):
    """A live IPC connection with a board already open returns success on the
    fast path — it must never spawn a duplicate pcbnew via the self-heal."""
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)
    monkeypatch.setattr(KiCADProcessManager, "is_running", staticmethod(lambda: True))

    iface = _connected_iface()
    iface._ipc_has_open_board_document = lambda: True
    iface._try_auto_open_board = MagicMock(side_effect=AssertionError("must not spawn"))

    ok, reason = iface.ensure_ipc()

    assert ok is True
    assert reason == ""
    iface._try_auto_open_board.assert_not_called()


def test_ensure_ipc_bare_pm_run_action_bounded_without_spawn(monkeypatch):
    """A frame-agnostic caller (require_pcb_editor=False, e.g. run_action)
    against a bare not-ready PM must fail fast with a clear message and must
    NOT spawn a board editor (it doesn't need one) or poll the launch loop."""
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)
    monkeypatch.setattr(KiCADProcessManager, "is_running", staticmethod(lambda: True))
    _no_cold_launch(monkeypatch)

    iface = _bare_iface()
    iface._try_enable_ipc_backend = lambda force=False: False
    iface._try_auto_open_board = MagicMock(
        side_effect=AssertionError("must not spawn for run_action")
    )

    started = time.monotonic()
    ok, reason = iface.ensure_ipc(timeout_s=30.0, require_pcb_editor=False)

    assert ok is False
    assert "not ready to reply" in reason
    assert time.monotonic() - started < 0.5
    iface._try_auto_open_board.assert_not_called()
