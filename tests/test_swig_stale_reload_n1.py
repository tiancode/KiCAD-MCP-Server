"""N1 regression: the SWIG in-memory board reloads when disk has newer content.

An IPC-routed save (``ipc_save_board``, the ``save_project`` fast path, or a
user Ctrl+S in KiCad) rewrites the ``.kicad_pcb`` while the MCP's SWIG board
object still holds the pre-save content — SWIG-served reads then returned
stale data with no flag (``get_pcb_overview`` reported 42 components vs disk's
38).  The dispatcher now runs ``_reload_swig_board_if_disk_changed`` before
every SWIG-path board command: an IPC save IS an external edit from the SWIG
board's perspective.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from kicad_interface import KiCADInterface  # noqa: E402


def _make_iface(tmp_path, *, content=b"(kicad_pcb rev1)\n"):
    """Interface with a fake SWIG board bound to a real temp file."""
    board_path = tmp_path / "demo.kicad_pcb"
    board_path.write_bytes(content)

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = False
    iface.ipc_backend = None
    iface.ipc_board_api = None
    board = MagicMock(name="swig_board")
    board.GetFileName.return_value = str(board_path)
    iface.board = board
    iface.project_commands = MagicMock()
    iface.command_routes = {}
    iface._last_auto_save_status = None
    iface._ipc_writes_pending = False
    iface._swig_writes_landed = False
    iface._ipc_change_callback_registered = False
    # Record the baseline signature the way open_project does.
    iface._record_board_signature(str(board_path))
    return iface, board_path


# ---------------------------------------------------------------------------
# _reload_swig_board_if_disk_changed unit behaviour
# ---------------------------------------------------------------------------
def test_no_reload_when_signature_matches(tmp_path, monkeypatch):
    iface, board_path = _make_iface(tmp_path)
    load_spy = MagicMock()
    monkeypatch.setattr(KiCADInterface, "_safe_load_board", load_spy)

    out = iface._reload_swig_board_if_disk_changed()

    assert out is None
    load_spy.assert_not_called()


def test_reload_when_disk_content_changed(tmp_path, monkeypatch):
    iface, board_path = _make_iface(tmp_path)
    old_board = iface.board
    # Simulate an IPC/KiCad save landing newer content on disk.
    board_path.write_bytes(b"(kicad_pcb rev2 -- saved by KiCad)\n")

    fresh_board = MagicMock(name="reloaded_board")
    fresh_board.GetFileName.return_value = str(board_path)
    monkeypatch.setattr(KiCADInterface, "_safe_load_board", lambda self, p: fresh_board)
    monkeypatch.setattr(KiCADInterface, "_update_command_handlers", lambda self: None)

    out = iface._reload_swig_board_if_disk_changed()

    assert out == {"swigReloadedFromDisk": True}
    assert iface.board is fresh_board
    assert iface.board is not old_board
    assert iface.project_commands.board is fresh_board
    # Signature re-recorded: a second call is a no-op.
    assert iface._reload_swig_board_if_disk_changed() is None


def test_touch_only_mtime_bump_refreshes_signature_without_reload(tmp_path, monkeypatch):
    import os

    iface, board_path = _make_iface(tmp_path)
    load_spy = MagicMock()
    monkeypatch.setattr(KiCADInterface, "_safe_load_board", load_spy)
    # Advance mtime without changing content (external `touch`).
    st = board_path.stat()
    os.utime(board_path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))

    out = iface._reload_swig_board_if_disk_changed()

    assert out is None
    load_spy.assert_not_called()
    # Recorded mtime refreshed so the next call takes the stat fast path.
    assert iface._board_disk_signature[0] == board_path.stat().st_mtime_ns


def test_no_reload_when_swig_has_unsaved_edits(tmp_path, monkeypatch):
    """A refused auto-save means SWIG memory holds real unsaved edits —
    reloading would silently discard them, so the reload must be skipped."""
    iface, board_path = _make_iface(tmp_path)
    old_board = iface.board
    board_path.write_bytes(b"(kicad_pcb rev2 external)\n")
    iface._last_auto_save_status = {
        "saved": False,
        "warning": "Auto-save refused ...",
        "diskChangedExternally": True,
        "memChangesUnsaved": True,
    }
    load_spy = MagicMock()
    monkeypatch.setattr(KiCADInterface, "_safe_load_board", load_spy)

    out = iface._reload_swig_board_if_disk_changed()

    assert out is None
    load_spy.assert_not_called()
    assert iface.board is old_board


def test_no_reload_without_baseline_signature(tmp_path, monkeypatch):
    iface, board_path = _make_iface(tmp_path)
    iface._board_disk_signature = None
    board_path.write_bytes(b"(kicad_pcb rev2)\n")
    load_spy = MagicMock()
    monkeypatch.setattr(KiCADInterface, "_safe_load_board", load_spy)

    assert iface._reload_swig_board_if_disk_changed() is None
    load_spy.assert_not_called()


def test_failed_reload_keeps_old_board(tmp_path, monkeypatch):
    iface, board_path = _make_iface(tmp_path)
    old_board = iface.board
    board_path.write_bytes(b"(kicad_pcb rev2)\n")
    monkeypatch.setattr(KiCADInterface, "_safe_load_board", lambda self, p: None)

    out = iface._reload_swig_board_if_disk_changed()

    assert out is None
    assert iface.board is old_board


# ---------------------------------------------------------------------------
# Save callback no longer masks the divergence
# ---------------------------------------------------------------------------
def test_ipc_save_leaves_signature_stale_so_reload_fires(tmp_path, monkeypatch):
    """End-to-end at the flag level: IPC save event → signature NOT refreshed
    → next reload check detects the newer disk content and reloads."""
    iface, board_path = _make_iface(tmp_path)
    iface._ipc_writes_pending = True

    # KiCad writes the file, then kipy's save fires the change callback.
    board_path.write_bytes(b"(kicad_pcb rev2 -- ipc save)\n")
    iface._on_ipc_change("save", {})
    assert iface._ipc_writes_pending is False

    fresh_board = MagicMock(name="reloaded")
    fresh_board.GetFileName.return_value = str(board_path)
    monkeypatch.setattr(KiCADInterface, "_safe_load_board", lambda self, p: fresh_board)
    monkeypatch.setattr(KiCADInterface, "_update_command_handlers", lambda self: None)

    out = iface._reload_swig_board_if_disk_changed()

    assert out == {"swigReloadedFromDisk": True}
    assert iface.board is fresh_board


# ---------------------------------------------------------------------------
# Dispatcher integration
# ---------------------------------------------------------------------------
def _dispatch_iface(tmp_path):
    iface, board_path = _make_iface(tmp_path)
    # Minimal set: pretend find_component is a board-backed identity route.
    iface._swig_board_backed_commands = {"find_component", "get_pcb_overview"}
    return iface, board_path


def test_dispatcher_reloads_before_swig_board_read(tmp_path, monkeypatch):
    iface, board_path = _dispatch_iface(tmp_path)
    board_path.write_bytes(b"(kicad_pcb rev2 -- ipc save)\n")

    fresh_board = MagicMock(name="reloaded")
    fresh_board.GetFileName.return_value = str(board_path)
    monkeypatch.setattr(KiCADInterface, "_safe_load_board", lambda self, p: fresh_board)
    monkeypatch.setattr(KiCADInterface, "_update_command_handlers", lambda self: None)

    seen = {}

    def fake_find(params):
        seen["board_at_call"] = iface.board
        return {"success": True, "components": []}

    iface.command_routes = {"find_component": fake_find}

    result = iface.handle_command("find_component", {"reference": "MH5"})

    assert result["success"] is True
    assert result["swigReloadedFromDisk"] is True
    assert seen["board_at_call"] is fresh_board


def test_dispatcher_no_marker_when_disk_unchanged(tmp_path):
    iface, board_path = _dispatch_iface(tmp_path)
    iface.command_routes = {"find_component": lambda p: {"success": True, "components": []}}

    result = iface.handle_command("find_component", {})

    assert result["success"] is True
    assert "swigReloadedFromDisk" not in result


def test_dispatcher_skips_check_for_non_board_commands(tmp_path, monkeypatch):
    """A command outside _swig_board_backed_commands must not trigger the
    check (schematic/library/UI ops don't read the SWIG board)."""
    iface, board_path = _dispatch_iface(tmp_path)
    board_path.write_bytes(b"(kicad_pcb rev2)\n")
    reload_spy = MagicMock(return_value=None)
    monkeypatch.setattr(KiCADInterface, "_reload_swig_board_if_disk_changed", reload_spy)
    iface.command_routes = {"list_schematic_components": lambda p: {"success": True}}

    result = iface.handle_command("list_schematic_components", {})

    assert result["success"] is True
    reload_spy.assert_not_called()


# ---------------------------------------------------------------------------
# _swig_board_backed_commands membership (built in __init__; pin the shape
# via a class-level reconstruction to avoid running the heavy __init__).
# ---------------------------------------------------------------------------
def test_board_mutating_commands_are_board_backed_by_construction():
    """The set union in __init__ starts from _BOARD_MUTATING_COMMANDS; pin the
    invariant the dispatcher relies on for mutation-side consistency."""
    src_path = Path(__file__).parent.parent / "python" / "kicad_interface.py"
    src = src_path.read_text(encoding="utf-8")
    assert "self._swig_board_backed_commands = set(self._BOARD_MUTATING_COMMANDS)" in src
    # Explicit extras that aren't identity routes on board-backed objects.
    for extra in ('"add_board_text"', '"add_copper_pour"', '"save_project"', '"get_pcb_overview"'):
        assert extra in src.split("_swig_board_backed_commands.update", 1)[1].split(")", 1)[0]
