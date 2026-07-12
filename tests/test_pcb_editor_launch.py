"""Launch/decision-matrix tests for opening the standalone PCB editor.

Bug (verified on KiCad 10.0.4): the IPC auto-launch/auto-open flow launched a
bare ``kicad`` project manager (``kicad <board.kicad_pcb>``), which never opens
a ``.kicad_pcb`` document over IPC — so every IPC-only board tool stayed gated.
The fix launches the *standalone PCB editor* (``pcbnew <board>``) instead,
which surfaces the board over IPC even alongside a running project manager.

These tests pin the resolution + launch decision matrix with mocks:

  - resolve pcbnew: sibling of the project manager / PATH / platform default /
    Flatpak fallback;
  - launch(): a .kicad_pcb goes to pcbnew, a .kicad_pro (or no path) to the
    project manager;
  - the ``~<name>.<ext>.lck`` per-file lock helper;
  - the KICAD_AUTO_LAUNCH=false opt-out disables auto-launch/auto-open.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _boom(*_a, **_k):
    raise AssertionError("this executable resolver must not be used")


# ---------------------------------------------------------------------------
# get_pcb_editor_path — resolution order
# ---------------------------------------------------------------------------
def test_pcb_editor_path_prefers_sibling_of_resolved_executable(monkeypatch, tmp_path):
    """A pcbnew binary next to the resolved kicad/pcbnew executable wins,
    so a non-PATH install keeps its bin dir together."""
    from utils.kicad_process import KiCADProcessManager

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "kicad").write_text("#!/bin/true\n")
    (bindir / "pcbnew").write_text("#!/bin/true\n")

    monkeypatch.setattr("utils.kicad_process.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        KiCADProcessManager, "get_executable_path", staticmethod(lambda: bindir / "kicad")
    )
    # shutil.which must not be needed — the sibling is found first.
    monkeypatch.setattr("utils.kicad_process.shutil.which", _boom)

    assert KiCADProcessManager.get_pcb_editor_path() == bindir / "pcbnew"


def test_pcb_editor_path_falls_back_to_path(monkeypatch, tmp_path):
    """No sibling pcbnew → use ``shutil.which('pcbnew')``."""
    from utils.kicad_process import KiCADProcessManager

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "kicad").write_text("#!/bin/true\n")  # no pcbnew sibling

    monkeypatch.setattr("utils.kicad_process.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        KiCADProcessManager, "get_executable_path", staticmethod(lambda: bindir / "kicad")
    )
    monkeypatch.setattr("utils.kicad_process.shutil.which", lambda name: "/somewhere/pcbnew")

    assert KiCADProcessManager.get_pcb_editor_path() == Path("/somewhere/pcbnew")


def test_pcb_editor_path_returns_none_when_nothing_found(monkeypatch):
    """No sibling, not on PATH, no platform default → None (so
    get_pcb_editor_command can try the Flatpak form)."""
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr("utils.kicad_process.platform.system", lambda: "Linux")
    monkeypatch.setattr(KiCADProcessManager, "get_executable_path", staticmethod(lambda: None))
    monkeypatch.setattr("utils.kicad_process.shutil.which", lambda name: None)
    # Neither platform-default path exists.
    monkeypatch.setattr(Path, "exists", lambda self: False)

    assert KiCADProcessManager.get_pcb_editor_path() is None


# ---------------------------------------------------------------------------
# get_pcb_editor_command — native, board-append, Flatpak fallback, None
# ---------------------------------------------------------------------------
def test_pcb_editor_command_native_appends_board(monkeypatch):
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr(
        KiCADProcessManager, "get_pcb_editor_path", staticmethod(lambda: Path("/usr/bin/pcbnew"))
    )
    cmd = KiCADProcessManager.get_pcb_editor_command(Path("/proj/board.kicad_pcb"))
    assert cmd == ["/usr/bin/pcbnew", "/proj/board.kicad_pcb"]

    # No board path → just the editor.
    assert KiCADProcessManager.get_pcb_editor_command() == ["/usr/bin/pcbnew"]


def test_pcb_editor_command_flatpak_fallback(monkeypatch):
    """No native pcbnew but KiCad is a Flatpak → the flatpak run form."""
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr(KiCADProcessManager, "get_pcb_editor_path", staticmethod(lambda: None))
    monkeypatch.setattr(KiCADProcessManager, "_flatpak_kicad_installed", staticmethod(lambda: True))

    cmd = KiCADProcessManager.get_pcb_editor_command(Path("/proj/board.kicad_pcb"))
    assert cmd == [
        "flatpak",
        "run",
        "--command=pcbnew",
        "org.kicad.KiCad",
        "/proj/board.kicad_pcb",
    ]


def test_pcb_editor_command_none_when_nothing_available(monkeypatch):
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr(KiCADProcessManager, "get_pcb_editor_path", staticmethod(lambda: None))
    monkeypatch.setattr(
        KiCADProcessManager, "_flatpak_kicad_installed", staticmethod(lambda: False)
    )

    assert KiCADProcessManager.get_pcb_editor_command(Path("/proj/board.kicad_pcb")) is None


# ---------------------------------------------------------------------------
# launch() routing — the core of the fix
# ---------------------------------------------------------------------------
def _patch_launch_common(monkeypatch):
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr(KiCADProcessManager, "is_running", staticmethod(lambda: False))
    monkeypatch.setattr(KiCADProcessManager, "ensure_ipc_api_enabled", staticmethod(lambda: True))


def _capture_popen(monkeypatch):
    spawned = {}

    def _fake(argv, **kwargs):
        spawned["argv"] = list(argv)
        spawned["kwargs"] = kwargs
        return MagicMock()

    monkeypatch.setattr("subprocess.Popen", _fake)
    return spawned


def test_launch_board_file_uses_standalone_pcb_editor(monkeypatch, tmp_path):
    """The headline fix: launching for a .kicad_pcb spawns the standalone PCB
    editor, NOT the project manager."""
    from utils.kicad_process import KiCADProcessManager

    _patch_launch_common(monkeypatch)
    monkeypatch.setattr(
        KiCADProcessManager,
        "get_pcb_editor_command",
        staticmethod(lambda board_path=None: ["/usr/bin/pcbnew", str(board_path)]),
    )
    # The project-manager resolver must not be consulted for a board.
    monkeypatch.setattr(KiCADProcessManager, "get_executable_path", staticmethod(_boom))
    spawned = _capture_popen(monkeypatch)

    board = tmp_path / "led.kicad_pcb"
    board.write_text("(kicad_pcb)\n")

    assert KiCADProcessManager.launch(board, wait_for_start=False) is True
    assert spawned["argv"] == ["/usr/bin/pcbnew", str(board)]


def test_launch_project_file_uses_project_manager(monkeypatch, tmp_path):
    """A .kicad_pro still opens the project manager (users expect the PM)."""
    from utils.kicad_process import KiCADProcessManager

    _patch_launch_common(monkeypatch)
    monkeypatch.setattr(
        KiCADProcessManager, "get_executable_path", staticmethod(lambda: Path("/usr/bin/kicad"))
    )
    # The PCB-editor resolver must not fire for a project file.
    monkeypatch.setattr(KiCADProcessManager, "get_pcb_editor_command", staticmethod(_boom))
    spawned = _capture_popen(monkeypatch)

    pro = tmp_path / "led.kicad_pro"
    pro.write_text("(kicad_project)\n")

    assert KiCADProcessManager.launch(pro, wait_for_start=False) is True
    assert spawned["argv"] == ["/usr/bin/kicad", str(pro)]


def test_launch_no_path_uses_project_manager(monkeypatch):
    from utils.kicad_process import KiCADProcessManager

    _patch_launch_common(monkeypatch)
    monkeypatch.setattr(
        KiCADProcessManager, "get_executable_path", staticmethod(lambda: Path("/usr/bin/kicad"))
    )
    monkeypatch.setattr(KiCADProcessManager, "get_pcb_editor_command", staticmethod(_boom))
    spawned = _capture_popen(monkeypatch)

    assert KiCADProcessManager.launch(None, wait_for_start=False) is True
    assert spawned["argv"] == ["/usr/bin/kicad"]


def test_launch_board_falls_back_to_pm_when_no_pcb_editor(monkeypatch, tmp_path):
    """If the PCB editor can't be resolved at all, launch() falls back to the
    project manager rather than failing outright."""
    from utils.kicad_process import KiCADProcessManager

    _patch_launch_common(monkeypatch)
    monkeypatch.setattr(
        KiCADProcessManager, "get_pcb_editor_command", staticmethod(lambda board_path=None: None)
    )
    monkeypatch.setattr(
        KiCADProcessManager, "get_executable_path", staticmethod(lambda: Path("/usr/bin/kicad"))
    )
    spawned = _capture_popen(monkeypatch)

    board = tmp_path / "led.kicad_pcb"
    board.write_text("(kicad_pcb)\n")

    assert KiCADProcessManager.launch(board, wait_for_start=False) is True
    assert spawned["argv"] == ["/usr/bin/kicad", str(board)]


def test_launch_short_circuits_when_already_running(monkeypatch):
    """No spawn when KiCad is already up — launch() returns True immediately."""
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr(KiCADProcessManager, "is_running", staticmethod(lambda: True))
    spawn_spy = MagicMock(side_effect=AssertionError("must not spawn when already running"))
    monkeypatch.setattr("subprocess.Popen", spawn_spy)

    assert KiCADProcessManager.launch(Path("/x/led.kicad_pcb"), wait_for_start=False) is True
    spawn_spy.assert_not_called()


# ---------------------------------------------------------------------------
# per-file lock helper
# ---------------------------------------------------------------------------
def test_board_lock_present_detects_kicad_lockfile(tmp_path):
    from handlers.ui import _board_lock_present

    board = tmp_path / "led.kicad_pcb"
    board.write_text("(kicad_pcb)\n")

    assert _board_lock_present(board) is False
    (tmp_path / "~led.kicad_pcb.lck").write_text('{"hostname":"h","username":"u"}')
    assert _board_lock_present(board) is True


# ---------------------------------------------------------------------------
# KICAD_AUTO_LAUNCH=false opt-out disables auto-launch AND auto-open
# ---------------------------------------------------------------------------
def test_auto_open_disabled_by_env(monkeypatch):
    from kicad_interface import KiCADInterface

    monkeypatch.setenv("KICAD_AUTO_LAUNCH", "false")
    assert KiCADInterface._auto_open_board_allowed() is False
    monkeypatch.setenv("KICAD_AUTO_LAUNCH", "true")
    assert KiCADInterface._auto_open_board_allowed() is True
    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)
    assert KiCADInterface._auto_open_board_allowed() is True


def test_try_auto_open_board_bails_when_disabled(monkeypatch):
    """With the opt-out set, _try_auto_open_board must not spawn or forward
    anything — it returns False so the gate response reaches the user."""
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    monkeypatch.setenv("KICAD_AUTO_LAUNCH", "false")
    monkeypatch.setattr(KiCADInterface, "_ipc_has_open_board_document", lambda self: False)

    spawn_spy = MagicMock(side_effect=AssertionError("must not spawn when auto-launch disabled"))
    monkeypatch.setattr("subprocess.Popen", spawn_spy)

    assert KiCADInterface._try_auto_open_board(iface) is False
    spawn_spy.assert_not_called()


def test_check_and_launch_kicad_respects_auto_launch_false(monkeypatch):
    """The launch_kicad_ui path: auto_launch=False never spawns."""
    from utils.kicad_process import KiCADProcessManager, check_and_launch_kicad

    monkeypatch.setattr(KiCADProcessManager, "is_running", staticmethod(lambda: False))
    spawn_spy = MagicMock(side_effect=AssertionError("must not spawn with auto_launch=False"))
    monkeypatch.setattr("subprocess.Popen", spawn_spy)

    out = check_and_launch_kicad(Path("/x/led.kicad_pcb"), auto_launch=False)
    assert out["running"] is False
    assert out["launched"] is False
    spawn_spy.assert_not_called()
