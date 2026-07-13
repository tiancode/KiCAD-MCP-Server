"""E2E round-6 P5 / P8: truthful, non-sticky KiCad detection + IPC fallback.

P5 (major): ``get_backend_info`` reported ``kicad_running: true`` (and told the
user to enable IPC in a nonexistent window) while ``manage_kicad_ui`` correctly
said running:false — because the two used DIFFERENT detectors.  ``is_running``
used a loose ``pgrep -f "KiCad|pcbnew"`` substring match that flagged the MCP
server, kicad-cli subprocesses, and unrelated processes whose command line
merely mentioned KiCad (e.g. an agent whose system prompt discusses KiCad).
``get_process_info`` used a stricter ``ps`` filter.  Both macOS branches now
share ONE strict argv[0]-basename detector so they can't contradict.

P8 (minor): after KiCad closed (process + socket gone), ``use_ipc`` /
``ipc_board_api`` stayed set, so query_copper kept routing to the IPC fast path
and returned a sticky PCB_EDITOR_REQUIRED, refusing the file fallback.
``_try_enable_ipc_backend`` now notices the dropped connection and reverts to
SWIG.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from utils.kicad_process import KiCADProcessManager  # noqa: E402


# ---------------------------------------------------------------------------
# P5: strict macOS detection — no false positives, no contradiction
# ---------------------------------------------------------------------------
# A realistic `ps -axo pid=,args=` dump: an MCP server, an unrelated agent whose
# prompt mentions KiCad, a kicad-cli subprocess, a shell in a KiCad repo — and
# (in the RUNNING variant) a real KiCad GUI frame.
_PS_NO_KICAD = (
    "  501 /usr/bin/python /Users/x/KiCAD-MCP-Server/python/kicad_interface.py\n"
    "  777 claude -p --model fable --append-system-prompt KiCad原理图修复与BOM比对 pcbnew\n"
    "  888 /bin/zsh -c cd /Users/x/kicad-project && ls\n"
    "  999 /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli pcb drc board.kicad_pcb\n"
)
_PS_WITH_PCBNEW = _PS_NO_KICAD + (
    "  1234 /Applications/KiCad/KiCad.app/Contents/Applications/"
    "pcbnew.app/Contents/MacOS/pcbnew /tmp/demo.kicad_pcb\n"
)
_PS_WITH_PROJECT_MANAGER = _PS_NO_KICAD + (
    "  1200 /Applications/KiCad/KiCad.app/Contents/MacOS/kicad /tmp/demo.kicad_pro\n"
)


def _mock_ps(monkeypatch, stdout: str):
    monkeypatch.setattr("utils.kicad_process.platform.system", lambda: "Darwin")

    def _run(cmd, capture_output=False, text=False, **kw):
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr("utils.kicad_process.subprocess.run", _run)


def test_darwin_is_running_false_when_only_noise(monkeypatch):
    """The exact P5 false positive: no KiCad GUI, but the process table has an
    MCP server, a kicad-cli subprocess, and an agent whose prompt mentions
    KiCad.  is_running() must be False."""
    _mock_ps(monkeypatch, _PS_NO_KICAD)
    assert KiCADProcessManager.is_running() is False
    assert KiCADProcessManager.get_process_info() == []
    assert KiCADProcessManager.is_pcb_editor_running() is False


def test_darwin_is_running_true_for_real_pcbnew(monkeypatch):
    _mock_ps(monkeypatch, _PS_WITH_PCBNEW)
    assert KiCADProcessManager.is_running() is True
    assert KiCADProcessManager.is_pcb_editor_running() is True
    procs = KiCADProcessManager.get_process_info()
    assert len(procs) == 1
    assert procs[0]["name"] == "pcbnew"


def test_darwin_project_manager_counts_as_running_but_not_pcb_editor(monkeypatch):
    _mock_ps(monkeypatch, _PS_WITH_PROJECT_MANAGER)
    assert KiCADProcessManager.is_running() is True
    # A bare project manager is not the pcbnew frame.
    assert KiCADProcessManager.is_pcb_editor_running() is False


def test_darwin_is_running_and_process_info_agree(monkeypatch):
    """The core P5 contradiction fix: the boolean and the list never disagree."""
    for dump in (_PS_NO_KICAD, _PS_WITH_PCBNEW, _PS_WITH_PROJECT_MANAGER):
        _mock_ps(monkeypatch, dump)
        assert KiCADProcessManager.is_running() == bool(
            KiCADProcessManager.get_process_info()
        )


def test_darwin_kicad_cli_alone_is_not_running(monkeypatch):
    """A kicad-cli invocation (ERC/DRC/export) from inside the app bundle must
    NOT register as a running KiCad GUI."""
    _mock_ps(
        monkeypatch,
        "  999 /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli version\n",
    )
    assert KiCADProcessManager.is_running() is False


# ---------------------------------------------------------------------------
# P5: socket check keys on api.sock, never the lingering api.lock
# ---------------------------------------------------------------------------
def test_ipc_socket_live_ignores_lock_file(monkeypatch):
    import shutil
    import socket as _socket
    import tempfile

    # AF_UNIX paths are length-limited (~104 chars on macOS), so bind under a
    # short /tmp dir rather than pytest's long tmp_path.
    d = tempfile.mkdtemp(dir="/tmp")
    try:
        monkeypatch.setattr("utils.kicad_process.platform.system", lambda: "Darwin")
        monkeypatch.setattr(
            KiCADProcessManager, "_ipc_socket_dirs", staticmethod(lambda: [d])
        )

        # Only the lock file exists (the state that lingers after KiCad exits).
        Path(d, "api.lock").write_text("")
        assert KiCADProcessManager.is_ipc_socket_live() is False

        # A real AF_UNIX socket present → live.
        srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            srv.bind(str(Path(d, "api.sock")))
            assert KiCADProcessManager.is_ipc_socket_live() is True
        finally:
            srv.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# P5: get_backend_info guidance is launch-oriented when KiCad is not running
# ---------------------------------------------------------------------------
def _swig_iface():
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = False
    iface.ipc_backend = None
    iface.ipc_board_api = None
    iface._ipc_writes_pending = False
    iface._swig_writes_landed = False
    iface._try_enable_ipc_backend = lambda force=False: False
    iface._backend_status = lambda: {
        "backend": "swig",
        "realtime_sync": False,
        "ipc_connected": False,
        "capabilities": {},
        "unavailable_tools": ["add_segment"],
    }
    return iface


def test_get_backend_info_not_running_is_launch_oriented(monkeypatch):
    from handlers.ui import handle_get_backend_info

    monkeypatch.setattr(KiCADProcessManager, "is_running", lambda: False)
    monkeypatch.setattr(KiCADProcessManager, "is_ipc_socket_live", lambda: False)

    out = handle_get_backend_info(_swig_iface(), {})

    assert out["kicad_running"] is False
    assert out["ipcSocketPresent"] is False
    assert "manage_kicad_ui(action=launch)" in out["message"]
    # Must NOT tell the user to enable IPC in a nonexistent Preferences window.
    assert "Preferences" not in out["message"]


def test_get_backend_info_running_no_socket_notes_server_off(monkeypatch):
    from handlers.ui import handle_get_backend_info

    monkeypatch.setattr(KiCADProcessManager, "is_running", lambda: True)
    monkeypatch.setattr(KiCADProcessManager, "is_ipc_socket_live", lambda: False)

    out = handle_get_backend_info(_swig_iface(), {})

    assert out["kicad_running"] is True
    assert "Preferences" in out["message"]
    assert "IPC socket" in out["message"]  # the socket-derived hint


# ---------------------------------------------------------------------------
# P8: _try_enable_ipc_backend reverts to SWIG when the IPC connection drops
# ---------------------------------------------------------------------------
def _ipc_iface_disconnected():
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    backend = MagicMock()
    backend.is_connected.return_value = False  # KiCad closed, socket gone
    iface.ipc_backend = backend
    iface.ipc_board_api = MagicMock()  # stale handle
    return iface


def test_dropped_connection_reverts_use_ipc(monkeypatch):
    """The sticky state root: after KiCad closes, use_ipc / ipc_board_api must
    be cleared so the dispatcher stops routing to the dead IPC fast path."""
    from kicad_interface import KiCADInterface

    monkeypatch.setattr("kicad_interface.KICAD_BACKEND", "auto", raising=False)
    monkeypatch.setattr(KiCADProcessManager, "is_running", lambda: False)

    iface = _ipc_iface_disconnected()
    ok = KiCADInterface._try_enable_ipc_backend(iface, force=False)

    assert ok is False
    assert iface.use_ipc is False
    assert iface.ipc_board_api is None


def test_query_traces_falls_back_to_swig_after_kicad_closed(monkeypatch):
    """End-to-end P8: with KiCad gone, query_traces (an IPC-capable read) must
    fall back to the SWIG handler and NOT return a sticky PCB_EDITOR_REQUIRED."""
    from kicad_interface import KiCADInterface

    monkeypatch.setattr(KiCADProcessManager, "is_running", lambda: False)

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    backend = MagicMock()
    backend.is_connected.return_value = False
    iface.ipc_backend = backend
    iface.ipc_board_api = MagicMock()
    iface._ipc_writes_pending = False
    iface._swig_writes_landed = False
    iface.board = MagicMock()

    swig_called = {"n": 0}

    def _swig_query(params):
        swig_called["n"] += 1
        return {"success": True, "traceCount": 0, "traces": []}

    iface.command_routes = {"query_traces": _swig_query}
    # get_pcb_overview-style reload hook is not registered here.
    iface._swig_board_backed_commands = set()

    out = KiCADInterface.handle_command(iface, "query_traces", {})

    assert out["success"] is True
    assert "needs_pcb_editor" not in out
    assert out.get("errorCode") != "PCB_EDITOR_REQUIRED"
    assert swig_called["n"] == 1  # the SWIG file/board handler served it
    assert iface.use_ipc is False  # reverted
