"""E2E round-6 P6b: the macOS launcher must find the nested pcbnew.app binary.

On macOS the GUI binaries live in a nested app bundle:
  * project manager: /Applications/KiCad/KiCad.app/Contents/MacOS/kicad
  * PCB editor:      /Applications/KiCad/KiCad.app/Contents/Applications/
                     pcbnew.app/Contents/MacOS/pcbnew   (NESTED .app)

The old platform-default candidate lists pointed at
``/Applications/KiCad/KiCad.app/Contents/MacOS/pcbnew`` and
``/Applications/KiCad/pcbnew.app/...`` — neither exists — so the spawn fallback
reported "KiCad PCB editor / project manager executable not found".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from utils.kicad_process import KiCADProcessManager  # noqa: E402

_MACOS_ROOT = "/Applications/KiCad/KiCad.app/Contents"
_NESTED_PCBNEW = f"{_MACOS_ROOT}/Applications/pcbnew.app/Contents/MacOS/pcbnew"
_KICAD_PM = f"{_MACOS_ROOT}/MacOS/kicad"


def _only_these_exist(monkeypatch, existing: set):
    monkeypatch.setattr("utils.kicad_process.platform.system", lambda: "Darwin")
    monkeypatch.setattr("utils.kicad_process.shutil.which", lambda name: None)
    monkeypatch.setattr(Path, "exists", lambda self: str(self) in existing)


def test_pcb_editor_path_uses_nested_bundle(monkeypatch):
    """get_pcb_editor_path must return the nested pcbnew.app binary."""
    _only_these_exist(monkeypatch, {_NESTED_PCBNEW})
    # No sibling resolution (get_executable_path returns None here).
    monkeypatch.setattr(KiCADProcessManager, "get_executable_path", staticmethod(lambda: None))

    assert KiCADProcessManager.get_pcb_editor_path() == Path(_NESTED_PCBNEW)


def test_executable_path_finds_nested_pcbnew_when_no_project_manager(monkeypatch):
    """When only the nested pcbnew exists (no `kicad`), get_executable_path
    still resolves an executable instead of returning None."""
    _only_these_exist(monkeypatch, {_NESTED_PCBNEW})
    assert KiCADProcessManager.get_executable_path() == Path(_NESTED_PCBNEW)


def test_executable_path_prefers_project_manager(monkeypatch):
    _only_these_exist(monkeypatch, {_KICAD_PM, _NESTED_PCBNEW})
    assert KiCADProcessManager.get_executable_path() == Path(_KICAD_PM)


def test_pcb_editor_command_opens_board_with_nested_binary(monkeypatch):
    """The end-to-end fix: get_pcb_editor_command appends the board to the
    nested pcbnew binary so `pcbnew <board>` serves it over IPC."""
    _only_these_exist(monkeypatch, {_NESTED_PCBNEW})
    monkeypatch.setattr(KiCADProcessManager, "get_executable_path", staticmethod(lambda: None))

    cmd = KiCADProcessManager.get_pcb_editor_command(Path("/tmp/demo.kicad_pcb"))
    assert cmd == [_NESTED_PCBNEW, "/tmp/demo.kicad_pcb"]


def test_old_wrong_macos_paths_are_gone(monkeypatch):
    """Regression guard: the two non-existent paths must never be returned."""
    wrong = {
        f"{_MACOS_ROOT}/MacOS/pcbnew",
        "/Applications/KiCad/pcbnew.app/Contents/MacOS/pcbnew",
    }
    # Pretend ONLY the wrong paths exist — resolution must still yield None,
    # proving the code no longer lists them.
    _only_these_exist(monkeypatch, wrong)
    monkeypatch.setattr(KiCADProcessManager, "get_executable_path", staticmethod(lambda: None))
    assert KiCADProcessManager.get_pcb_editor_path() is None
