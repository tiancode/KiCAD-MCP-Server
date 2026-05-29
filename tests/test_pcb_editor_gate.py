"""Regression tests for the PCB-editor gate (IPC board-op short-circuit).

Covers three previously-shipped misbehaviours:

  1. ``is_pcb_editor_running`` lost the editor across a package upgrade because
     ``/proc/<pid>/exe`` resolves to ``"/usr/bin/pcbnew (deleted)"`` while the
     old binary is still mmap'd — basename equality then failed.
  2. ``require_ipc_board_op`` re-probed the process list after ``ensure_ipc``,
     so any non-editor IPC failure that happened to coincide with
     ``is_running() and not is_pcb_editor_running()`` (notably the
     ``KICAD_BACKEND=swig`` opt-out) got mis-reported as 'open the PCB editor'.
  3. Handler ``_require_ipc`` fed ``require_ipc_board_op``'s already-formatted
     ``'IPC backend not available: <reason>'`` into ``_ipc_unavailable``, which
     re-prefixed it — producing doubly-nested 'IPC backend not available'
     envelopes.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


# ---------------------------------------------------------------------------
# Fix #1 — is_pcb_editor_running handles "<exe> (deleted)" symlink targets
# ---------------------------------------------------------------------------
def test_is_pcb_editor_running_survives_pacman_upgrade(monkeypatch):
    """After a package upgrade mid-session the /proc/<pid>/exe symlink reads
    '/usr/bin/pcbnew (deleted)' until the process restarts.  The gate must
    still recognise this as the PCB editor or every IPC board op will be
    blocked from then on."""
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr("utils.kicad_process.platform.system", lambda: "Linux")
    monkeypatch.setattr("utils.kicad_process.os.listdir", lambda path: ["42"])
    monkeypatch.setattr(
        "utils.kicad_process.os.readlink",
        lambda path: "/usr/bin/pcbnew (deleted)" if path == "/proc/42/exe" else "",
    )

    assert KiCADProcessManager.is_pcb_editor_running() is True


def test_is_pcb_editor_running_ignores_unrelated_deleted_binaries(monkeypatch):
    """The (deleted) suffix strip must not paper over a different binary."""
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr("utils.kicad_process.platform.system", lambda: "Linux")
    monkeypatch.setattr("utils.kicad_process.os.listdir", lambda path: ["42"])
    monkeypatch.setattr(
        "utils.kicad_process.os.readlink",
        lambda path: "/usr/bin/eeschema (deleted)" if path == "/proc/42/exe" else "",
    )

    assert KiCADProcessManager.is_pcb_editor_running() is False


def test_linux_kicad_pids_survives_pacman_upgrade(monkeypatch):
    """``is_running()`` consumes ``_linux_kicad_pids``; same fix applies."""
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr("utils.kicad_process.platform.system", lambda: "Linux")
    monkeypatch.setattr("utils.kicad_process.os.listdir", lambda path: ["7", "13"])
    monkeypatch.setattr(
        "utils.kicad_process.os.readlink",
        lambda path: {
            "/proc/7/exe": "/usr/bin/kicad (deleted)",
            "/proc/13/exe": "/usr/bin/bash",
        }.get(path, ""),
    )

    assert KiCADProcessManager.is_running() is True


# ---------------------------------------------------------------------------
# Fix #2 — require_ipc_board_op branches on ensure_ipc's actual reason
# ---------------------------------------------------------------------------
def _bare_iface():
    """Construct a KiCADInterface without running its real __init__."""
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = False
    iface.ipc_board_api = None
    iface.ipc_backend = None
    return iface


def test_require_ipc_board_op_kicad_backend_swig_does_not_say_open_editor(monkeypatch):
    """KICAD_BACKEND=swig with project manager up used to trip the
    is_running() && !is_pcb_editor_running() heuristic and be reported as
    'open the PCB editor'.  The real fix is to unset the env var — the
    gate must not lie about what's wrong."""
    from kicad_interface import KiCADInterface
    from utils.kicad_process import KiCADProcessManager

    iface = _bare_iface()
    monkeypatch.setattr(
        KiCADInterface,
        "ensure_ipc",
        lambda self, **kw: (False, "KICAD_BACKEND=swig; IPC is disabled by configuration"),
    )
    # These would have steered the old recompute path to the editor branch.
    monkeypatch.setattr(KiCADProcessManager, "is_running", lambda: True)
    monkeypatch.setattr(KiCADProcessManager, "is_pcb_editor_running", lambda: False)

    out = iface.require_ipc_board_op()

    assert out.get("needs_pcb_editor") is not True
    assert "_ipc_reason" in out
    assert "KICAD_BACKEND" in out["_ipc_reason"]


def test_require_ipc_board_op_editor_gate_passes_through(monkeypatch):
    """When ensure_ipc returns the canonical editor-gate reason, the
    response carries needs_pcb_editor: True (the recoverable-state flag
    agents key on to prompt the user)."""
    from kicad_interface import KiCADInterface

    iface = _bare_iface()
    monkeypatch.setattr(
        KiCADInterface,
        "ensure_ipc",
        lambda self, **kw: (False, KiCADInterface._pcb_editor_gate_reason()),
    )

    out = iface.require_ipc_board_op()

    assert out["needs_pcb_editor"] is True
    assert out["success"] is False


def test_require_ipc_board_op_does_not_recompute_process_state(monkeypatch):
    """Race-fix regression: if ensure_ipc decided 'editor not open' but the
    user opens pcbnew in the gap before require_ipc_board_op runs its own
    check, the old code fell through to a contradictory generic envelope.
    The new branch is reason-string-based, so it doesn't matter what the
    process state looks like after ensure_ipc returns."""
    from kicad_interface import KiCADInterface
    from utils.kicad_process import KiCADProcessManager

    iface = _bare_iface()
    monkeypatch.setattr(
        KiCADInterface,
        "ensure_ipc",
        lambda self, **kw: (False, KiCADInterface._pcb_editor_gate_reason()),
    )
    # Simulate the race: editor "opened" between ensure_ipc and the gate
    # response.  Old code: is_running=True, is_pcb_editor_running=True →
    # not-editor branch → wrong envelope.  New code: keys on the reason.
    calls = {"is_running": 0, "is_pcb_editor_running": 0}

    def _is_running():
        calls["is_running"] += 1
        return True

    def _is_pcb_editor_running():
        calls["is_pcb_editor_running"] += 1
        return True

    monkeypatch.setattr(KiCADProcessManager, "is_running", _is_running)
    monkeypatch.setattr(KiCADProcessManager, "is_pcb_editor_running", _is_pcb_editor_running)

    out = iface.require_ipc_board_op()

    assert out["needs_pcb_editor"] is True
    # No extra process checks in the post-ensure_ipc decision.
    assert calls["is_running"] == 0
    assert calls["is_pcb_editor_running"] == 0


# ---------------------------------------------------------------------------
# Fix #3 — handler _require_ipc wraps raw reason, not pre-formatted message
# ---------------------------------------------------------------------------
def _iface_for_handler():
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = False
    iface.ipc_board_api = None
    iface.ipc_backend = MagicMock()
    return iface


def test_board_meta_require_ipc_message_is_not_double_prefixed(monkeypatch):
    """Old shape: 'Board metadata commands require... (IPC backend not
    available: <reason>)' — the parenthetical re-prefixes 'IPC backend not
    available'.  Fix: handler wraps the raw reason only."""
    from handlers import board_meta
    from kicad_interface import KiCADInterface

    iface = _iface_for_handler()
    monkeypatch.setattr(
        KiCADInterface,
        "require_ipc_board_op",
        lambda self, **kw: {"success": False, "_ipc_reason": "socket refused"},
    )

    out = board_meta._require_ipc(iface)

    assert out["success"] is False
    msg = out["message"]
    # The raw reason is present once, not nested in an envelope that also
    # says "IPC backend not available".
    assert "socket refused" in msg
    assert msg.count("IPC backend") == 1, msg


def test_selection_require_ipc_message_is_not_double_prefixed(monkeypatch):
    from handlers import selection
    from kicad_interface import KiCADInterface

    iface = _iface_for_handler()
    monkeypatch.setattr(
        KiCADInterface,
        "require_ipc_board_op",
        lambda self, **kw: {"success": False, "_ipc_reason": "socket refused"},
    )

    out = selection._require_ipc(iface)

    assert "socket refused" in out["message"]
    assert out["message"].count("IPC backend") == 1


def test_shapes_require_ipc_message_is_not_double_prefixed(monkeypatch):
    from handlers import shapes
    from kicad_interface import KiCADInterface

    iface = _iface_for_handler()
    monkeypatch.setattr(
        KiCADInterface,
        "require_ipc_board_op",
        lambda self, **kw: {"success": False, "_ipc_reason": "socket refused"},
    )

    out = shapes._require_ipc(iface)

    assert "socket refused" in out["message"]
    assert out["message"].count("IPC backend") == 1


def test_transactions_require_ipc_message_is_not_double_prefixed(monkeypatch):
    from handlers import transactions
    from kicad_interface import KiCADInterface

    iface = _iface_for_handler()
    monkeypatch.setattr(
        KiCADInterface,
        "require_ipc_board_op",
        lambda self, **kw: {"success": False, "_ipc_reason": "socket refused"},
    )

    out = transactions._require_ipc(iface)

    assert "socket refused" in out["message"]
    assert out["message"].count("IPC backend") == 1


def test_ipc_handler_require_ipc_message_is_not_double_prefixed(monkeypatch):
    from handlers import ipc as ipc_handler
    from kicad_interface import KiCADInterface

    iface = _iface_for_handler()
    monkeypatch.setattr(
        KiCADInterface,
        "require_ipc_board_op",
        lambda self, **kw: {"success": False, "_ipc_reason": "socket refused"},
    )

    out = ipc_handler._require_ipc(iface)

    assert "socket refused" in out["message"]
    assert out["message"].count("IPC backend") == 1


# ---------------------------------------------------------------------------
# Fix #4 — gate uses kipy.get_open_documents() instead of process existence
# ---------------------------------------------------------------------------
# Real-world failure mode: kicad's project manager pre-loads pcbnew as a
# kiway worker, so the binary IS a running process — but no .kicad_pcb
# document is loaded.  The old gate (which only checked
# is_pcb_editor_running) let calls through and ``get_board_info`` /
# ``move_component`` silently returned 0×0 / generic failure.  Switch to
# ``_ipc_has_open_board_document`` so the gate fires on that exact state.
def _kicad_with_docs(docs: list):
    """Build a stand-in ipc_backend whose kipy returns the given docs list."""
    backend = MagicMock()
    backend.is_connected = MagicMock(return_value=True)
    backend._kicad = MagicMock()
    backend._kicad.get_open_documents = MagicMock(return_value=docs)
    return backend


class _Doc:
    """Stand-in for a kipy DocumentSpecifier with a .path attribute."""

    def __init__(self, path: str):
        self.path = path


def test_open_board_document_detection_finds_kicad_pcb_in_open_docs():
    from kicad_interface import KiCADInterface

    iface = _bare_iface()
    iface.ipc_backend = _kicad_with_docs([_Doc("/tmp/demo.kicad_pcb")])

    assert KiCADInterface._ipc_has_open_board_document(iface) is True


def test_open_board_document_detection_returns_false_when_only_schematic():
    from kicad_interface import KiCADInterface

    iface = _bare_iface()
    iface.ipc_backend = _kicad_with_docs([_Doc("/tmp/demo.kicad_sch")])

    assert KiCADInterface._ipc_has_open_board_document(iface) is False


def test_open_board_document_detection_returns_false_when_no_docs_open():
    """The user's real-world failure: pcbnew is running as a kiway worker
    but no board is loaded, so kipy returns an empty document list. The
    old process-check gate let calls through; this is the new ground truth."""
    from kicad_interface import KiCADInterface

    iface = _bare_iface()
    iface.ipc_backend = _kicad_with_docs([])

    assert KiCADInterface._ipc_has_open_board_document(iface) is False


def test_open_board_document_detection_handles_kipy_errors_gracefully():
    """If get_open_documents itself throws (kipy stale, version mismatch),
    fail closed — assume no document is open, gate the next call."""
    from kicad_interface import KiCADInterface

    iface = _bare_iface()
    iface.ipc_backend = MagicMock()
    iface.ipc_backend.is_connected = MagicMock(return_value=True)
    iface.ipc_backend._kicad = MagicMock()
    iface.ipc_backend._kicad.get_open_documents = MagicMock(side_effect=RuntimeError("kipy stale"))

    assert KiCADInterface._ipc_has_open_board_document(iface) is False


def test_handle_command_ipc_fastpath_gates_when_no_board_doc_open(monkeypatch):
    """End-to-end: ``get_board_info`` used to silently return 0×0 in this
    state. Now the dispatch sees no .kicad_pcb in get_open_documents and
    short-circuits with needs_pcb_editor:true."""
    from kicad_interface import KiCADInterface

    iface = _bare_iface()
    iface.use_ipc = True
    iface.ipc_board_api = MagicMock()
    iface.ipc_backend = _kicad_with_docs([])  # No PCB doc open
    # The old process-based gate would have been bypassed here:
    monkeypatch.setattr(
        "utils.kicad_process.KiCADProcessManager.is_pcb_editor_running",
        lambda: True,
    )

    out = KiCADInterface.handle_command(iface, "get_board_info", {})

    assert out["success"] is False
    assert out["needs_pcb_editor"] is True
    assert out["command"] == "get_board_info"
    assert ".kicad_pcb" in out["message"]


# ---------------------------------------------------------------------------
# _autolaunch_for_project surfaces pcbDocumentOpen so the agent doesn't
# discover the missing board the hard way (silent empty results / "Failed
# to move component")
# ---------------------------------------------------------------------------
def test_autolaunch_marks_pcb_document_open_false_with_warning(monkeypatch, tmp_path):
    """After auto-launching the project manager, IPC attaches immediately
    but no PCB editor frame is loaded — _autolaunch_for_project must say
    so loudly instead of reporting a misleading 'ipcAttached: true' alone."""
    from handlers import project as project_handler
    from kicad_interface import KiCADInterface

    project_file = tmp_path / "demo.kicad_pro"
    project_file.write_text("(kicad_project)\n", encoding="utf-8")
    (tmp_path / "demo.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")

    monkeypatch.setattr(
        project_handler,
        "check_and_launch_kicad",
        lambda path, auto_launch=True: {
            "running": True,
            "launched": True,
            "processes": [],
            "message": "KiCAD launched",
        },
    )
    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)
    monkeypatch.delenv("KICAD_BACKEND", raising=False)

    iface = _bare_iface()
    iface._try_enable_ipc_backend = lambda force=False: True
    iface._current_board_path = lambda: None
    monkeypatch.setattr(KiCADInterface, "_ipc_has_open_board_document", lambda self: False)

    result = project_handler._autolaunch_for_project(iface, project_file, {})

    assert result["ipcAttached"] is True
    assert result["pcbDocumentOpen"] is False
    assert result["warning"] is not None
    assert "no .kicad_pcb document open" in result["warning"]


def test_autolaunch_marks_pcb_document_open_true_when_kipy_reports_it(monkeypatch, tmp_path):
    """The complementary positive case: when KiCad has the board loaded,
    pcbDocumentOpen surfaces True and no warning fires."""
    from handlers import project as project_handler
    from kicad_interface import KiCADInterface

    project_file = tmp_path / "demo.kicad_pro"
    project_file.write_text("(kicad_project)\n", encoding="utf-8")
    board_file = tmp_path / "demo.kicad_pcb"
    board_file.write_text("(kicad_pcb)\n", encoding="utf-8")

    monkeypatch.setattr(
        project_handler,
        "check_and_launch_kicad",
        lambda path, auto_launch=True: {
            "running": True,
            "launched": False,
            "processes": [],
            "message": "KiCAD already running",
        },
    )
    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)
    monkeypatch.delenv("KICAD_BACKEND", raising=False)

    iface = _bare_iface()
    iface._try_enable_ipc_backend = lambda force=False: True
    iface._current_board_path = lambda: str(board_file)
    monkeypatch.setattr(KiCADInterface, "_ipc_has_open_board_document", lambda self: True)

    result = project_handler._autolaunch_for_project(iface, project_file, {})

    assert result["ipcAttached"] is True
    assert result["pcbDocumentOpen"] is True
    assert result["warning"] is None


# ---------------------------------------------------------------------------
# Best-effort PCB-editor auto-open via IPC TOOL_ACTION + non-contradictory
# manual-recovery warning when it fails.
# ---------------------------------------------------------------------------
def _autolaunch_setup(monkeypatch, tmp_path):
    """Shared boilerplate: KiCad already running, IPC ready to attach, board
    file present alongside the .kicad_pro."""
    from handlers import project as project_handler

    project_file = tmp_path / "demo.kicad_pro"
    project_file.write_text("(kicad_project)\n", encoding="utf-8")
    (tmp_path / "demo.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")

    monkeypatch.setattr(
        project_handler,
        "check_and_launch_kicad",
        lambda path, auto_launch=True: {
            "running": True,
            "launched": False,
            "processes": [],
            "message": "KiCAD already running",
        },
    )
    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)
    monkeypatch.delenv("KICAD_BACKEND", raising=False)
    return project_file


def test_autolaunch_uses_run_action_to_open_pcb_editor(monkeypatch, tmp_path):
    """Happy path for the new retry: pcbDocumentOpen starts False, the
    first run_action candidate succeeds, the next has-board check returns
    True, and no warning fires."""
    from handlers import project as project_handler
    from kicad_interface import KiCADInterface

    project_file = _autolaunch_setup(monkeypatch, tmp_path)
    iface = _bare_iface()
    iface._try_enable_ipc_backend = lambda force=False: True
    iface._current_board_path = lambda: None

    # The has-board check returns False before run_action, True after.
    poll_count = {"n": 0}

    def _has_doc(self):
        poll_count["n"] += 1
        return poll_count["n"] >= 2  # first call: False; second call: True

    monkeypatch.setattr(KiCADInterface, "_ipc_has_open_board_document", _has_doc)

    invoked_actions = []
    iface.ipc_backend = MagicMock()
    iface.ipc_backend.is_connected = lambda: True

    def _fake_run_action(action):
        invoked_actions.append(action)
        return {"success": True, "action": action, "status": 1, "statusName": "RAS_OK"}

    iface.ipc_backend.run_action = _fake_run_action

    result = project_handler._autolaunch_for_project(iface, project_file, {})

    assert result["ipcAttached"] is True
    assert result["pcbDocumentOpen"] is True
    assert result["pcbEditorAutoOpenAttempted"] is True
    assert result["pcbEditorAutoOpened"] == invoked_actions[0]
    # First candidate succeeded, so we stopped — no further actions tried.
    assert len(invoked_actions) == 1
    assert result["warning"] is None


def test_autolaunch_warning_has_no_self_contradicting_advice(monkeypatch, tmp_path):
    """User report: the previous warning said "call open_project with the
    .kicad_pcb path" even when the caller HAD just done that.  The new
    warning must (1) not repeat the same call as the recovery step, and
    (2) tell the user the actual manual recovery (project manager → PCB
    icon)."""
    from handlers import project as project_handler
    from kicad_interface import KiCADInterface

    project_file = _autolaunch_setup(monkeypatch, tmp_path)
    iface = _bare_iface()
    iface._try_enable_ipc_backend = lambda force=False: True
    iface._current_board_path = lambda: None

    # _ipc_has_open_board_document always returns False — simulates the
    # case where the PM is up but no PCB editor frame ever opens.
    monkeypatch.setattr(KiCADInterface, "_ipc_has_open_board_document", lambda self: False)

    # Every run_action candidate is rejected by this KiCad version
    # (RAS_INVALID).
    tried = []
    iface.ipc_backend = MagicMock()
    iface.ipc_backend.is_connected = lambda: True

    def _all_reject(action):
        tried.append(action)
        return {"success": False, "action": action, "status": 0, "statusName": "RAS_INVALID"}

    iface.ipc_backend.run_action = _all_reject

    result = project_handler._autolaunch_for_project(iface, project_file, {})

    assert result["pcbDocumentOpen"] is False
    assert result["pcbEditorAutoOpenAttempted"] is True
    assert "pcbEditorAutoOpened" not in result
    # Multiple candidates tried before giving up.
    assert len(tried) >= 2
    warning = result["warning"]
    assert warning is not None
    # The bug we're locking in: the new warning MUST NOT re-suggest
    # the same call that already failed.
    assert "call open_project with" not in warning.lower()
    assert "open_project with the .kicad_pcb" not in warning
    # And it must give the actual manual recovery step.
    assert "double-click" in warning.lower()
    assert "pcb editor" in warning.lower()
    assert "project manager" in warning.lower()


def test_autolaunch_run_action_skipped_when_ipc_not_connected(monkeypatch, tmp_path):
    """If ipc_backend isn't connected (unusual — but we got here via
    _try_enable_ipc_backend returning True yet the backend reports
    disconnected), skip the run_action retry rather than crashing."""
    from handlers import project as project_handler
    from kicad_interface import KiCADInterface

    project_file = _autolaunch_setup(monkeypatch, tmp_path)
    iface = _bare_iface()
    iface._try_enable_ipc_backend = lambda force=False: True
    iface._current_board_path = lambda: None
    monkeypatch.setattr(KiCADInterface, "_ipc_has_open_board_document", lambda self: False)

    iface.ipc_backend = MagicMock()
    iface.ipc_backend.is_connected = lambda: False
    iface.ipc_backend.run_action = MagicMock(side_effect=AssertionError("must not be called"))

    result = project_handler._autolaunch_for_project(iface, project_file, {})

    assert result["pcbDocumentOpen"] is False
    assert result["pcbEditorAutoOpenAttempted"] is True
    # We attempted (set the flag) but didn't actually call run_action.
    iface.ipc_backend.run_action.assert_not_called()
    # Still produces the helpful manual-recovery warning.
    assert "double-click" in result["warning"].lower()
