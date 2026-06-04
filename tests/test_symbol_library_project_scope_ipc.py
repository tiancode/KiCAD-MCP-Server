"""Regression: symbol-library queries lazy-scope to the open project too.

The symbol handlers already scope from caller params (projectPath / schematicPath
/ boardPath via _ensure_manager_for), but in pure-IPC use — KiCad has the board
open, no open_project ran, no path param — nothing scoped them, so project
sym-lib-table entries were invisible. handle_command now scopes the symbol
manager from the live board path before a symbol-library query, mirroring the
footprint fix. use_project is idempotent so this is cheap per query.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _write_lib(path: Path, symbols):
    parts = ["(kicad_symbol_lib (version 20231120) (generator test)"]
    for sym in symbols:
        parts.append(f'  (symbol "{sym}" (pin_numbers (hide yes)) (pin_names (hide yes))')
        parts.append('    (property "Reference" "U" (at 0 0 0))')
        parts.append(f'    (property "Value" "{sym}" (at 0 0 0))')
        parts.append("  )")
    parts.append(")")
    path.write_text("\n".join(parts), encoding="utf-8")


def _bare_iface(symbol_commands, board_path, monkeypatch):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = False
    iface.ipc_backend = None
    iface.ipc_board_api = None
    iface.board = None
    iface._current_project_path = None
    iface.symbol_library_commands = symbol_commands
    iface.command_routes = {
        "list_symbol_libraries": symbol_commands.list_symbol_libraries,
        "list_library_symbols": symbol_commands.list_library_symbols,
    }
    monkeypatch.setattr(KiCADInterface, "_current_board_path", lambda self: board_path)
    return iface


# ---------------------------------------------------------------------------
# Wiring: dispatch hook scopes from the live board path
# ---------------------------------------------------------------------------
def test_symbol_query_calls_use_project_with_board_dir(monkeypatch, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    board_path = str(proj / "board.kicad_pcb")

    cmds = MagicMock()
    cmds.list_symbol_libraries = lambda params: {"success": True, "libraries": []}
    iface = _bare_iface(cmds, board_path, monkeypatch)

    out = iface.handle_command("list_symbol_libraries", {})

    assert out["success"] is True
    cmds.use_project.assert_called_once_with(proj)


def test_symbol_query_no_scope_without_board_or_project(monkeypatch):
    cmds = MagicMock()
    cmds.list_symbol_libraries = lambda params: {"success": True, "libraries": []}
    iface = _bare_iface(cmds, None, monkeypatch)  # no board path

    out = iface.handle_command("list_symbol_libraries", {})

    assert out["success"] is True
    cmds.use_project.assert_not_called()  # global browsing left undisturbed


# ---------------------------------------------------------------------------
# End-to-end: a project-only symbol library becomes visible over pure IPC
# ---------------------------------------------------------------------------
def test_symbol_query_sees_project_lib_end_to_end(monkeypatch, tmp_path):
    import commands.library_symbol as lib_sym
    from commands.library_symbol import SymbolLibraryCommands, SymbolLibraryManager

    # Empty global table so only the project sym-lib-table is in play.
    monkeypatch.setattr(SymbolLibraryManager, "_get_global_sym_lib_table", lambda self: None)
    # Isolate the on-disk symbol cache to tmp so the test neither reads nor
    # rewrites the real ~/.kicad-mcp cache (established pattern, test_symbol_library).
    monkeypatch.setattr(
        lib_sym, "_DISK_CACHE_PATH", tmp_path / ".kicad-mcp" / "cache" / "symbol_libraries.pickle"
    )

    proj = tmp_path / "proj"
    proj.mkdir()
    _write_lib(proj / "mylib.kicad_sym", ["WIDGET"])
    (proj / "sym-lib-table").write_text(
        "(sym_lib_table\n"
        '  (lib (name "mylib")(type "KiCad")'
        '(uri "${KIPRJMOD}/mylib.kicad_sym")(options "")(descr ""))\n)\n',
        encoding="utf-8",
    )
    board_path = str(proj / "board.kicad_pcb")

    cmds = SymbolLibraryCommands(SymbolLibraryManager(project_path=None))
    # Global-only manager can't see the project library.
    assert "mylib" not in cmds.library_manager.libraries

    iface = _bare_iface(cmds, board_path, monkeypatch)
    out = iface.handle_command("list_library_symbols", {"library": "mylib"})
    # Parsing mylib dirtied the (now project-scoped) manager; its atexit flush
    # runs after monkeypatch reverts, so neutralize it here to avoid rewriting
    # the real ~/.kicad-mcp symbol cache with this throwaway lib.
    cmds.library_manager._cache_dirty = False

    assert out["success"] is True, out
    assert "WIDGET" in [s["name"] for s in out["symbols"]]
