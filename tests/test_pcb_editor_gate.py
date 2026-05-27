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
