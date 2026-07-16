"""Regression: footprint-library queries must see the open project's libraries.

list_library_footprints / get_footprint_info / list_libraries / search_footprints
route through a LibraryManager that was built GLOBAL-ONLY at startup and never
re-scoped to the open project (the symbol side got use_project; the footprint
side never did). A footprint library registered only in the project's
fp-lib-table (``${KIPRJMOD}/*.pretty``) therefore read back empty —
list_library_footprints returned 0.

Also covers get_footprint_info's not-found path, which referenced unbound locals
(NameError) instead of returning a clean failure when the footprint wasn't found.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _make_project(tmp_path: Path) -> Path:
    """Project dir with a project fp-lib-table -> ${KIPRJMOD}/mylib.pretty/FOO."""
    proj = tmp_path / "proj"
    proj.mkdir()
    pretty = proj / "mylib.pretty"
    pretty.mkdir()
    (pretty / "FOO.kicad_mod").write_text('(footprint "FOO" (layer "F.Cu"))\n', encoding="utf-8")
    (proj / "fp-lib-table").write_text(
        "(fp_lib_table\n"
        '  (lib (name "mylib")(type "KiCad")'
        '(uri "${KIPRJMOD}/mylib.pretty")(options "")(descr ""))\n)\n',
        encoding="utf-8",
    )
    return proj


@pytest.fixture
def no_global_fp_table(monkeypatch):
    """Drop the real global fp-lib-table so tests see ONLY the project table,
    and clear the process-wide manager cache around the test."""
    import commands.library as lib

    monkeypatch.setattr(lib.LibraryManager, "_get_global_fp_lib_table", lambda self: None)
    lib._MANAGER_CACHE.clear()
    yield
    lib._MANAGER_CACHE.clear()


# ---------------------------------------------------------------------------
# LibraryManager scope
# ---------------------------------------------------------------------------
def test_project_scoped_manager_sees_project_library(no_global_fp_table, tmp_path):
    from commands.library import LibraryManager

    proj = _make_project(tmp_path)
    mgr = LibraryManager(project_path=proj)

    assert "mylib" in mgr.libraries
    assert mgr.list_footprints("mylib") == ["FOO"]


def test_global_only_manager_blind_to_project_library(no_global_fp_table, tmp_path):
    """The startup default (project_path=None) is exactly what produced the
    reported "list_library_footprints returns 0"."""
    from commands.library import LibraryManager

    _make_project(tmp_path)
    mgr = LibraryManager(project_path=None)

    assert "mylib" not in mgr.libraries
    assert mgr.list_footprints("mylib") == []


# ---------------------------------------------------------------------------
# iface re-scoping (the fix)
# ---------------------------------------------------------------------------
def test_refresh_footprint_library_repoints_manager(no_global_fp_table, tmp_path):
    from commands.component import ComponentCommands
    from commands.library import LibraryCommands, get_library_manager
    from kicad_interface import KiCADInterface

    proj = _make_project(tmp_path)
    iface = KiCADInterface.__new__(KiCADInterface)
    iface.footprint_library = get_library_manager(project_path=None)
    iface.library_commands = LibraryCommands(iface.footprint_library)
    iface.component_commands = ComponentCommands(None, iface.footprint_library)

    # Before: global-only manager can't see the project lib.
    assert iface.library_commands.list_library_footprints({"library": "mylib"})["footprints"] == []

    iface._refresh_footprint_library_for_project(proj)

    # After: re-scoped manager sees it.
    out = iface.library_commands.list_library_footprints({"library": "mylib"})
    assert out["footprints"] == ["FOO"]
    # All holders of the startup manager re-point together so project footprints
    # are placeable, not just listable.
    assert iface.component_commands.library_manager is iface.library_commands.library_manager
    assert iface.component_commands.library_manager is iface.footprint_library


def test_handle_command_lazy_scopes_from_board_path(no_global_fp_table, tmp_path, monkeypatch):
    """Pure-IPC: no open_project ran, so the project dir is only knowable from
    the live board path. handle_command must lazily scope the footprint lib
    before a footprint-library query."""
    from commands.library import LibraryCommands, get_library_manager
    from kicad_interface import KiCADInterface

    proj = _make_project(tmp_path)
    board_path = str(proj / "board.kicad_pcb")

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = False
    iface.ipc_backend = None
    iface.ipc_board_api = None
    iface.board = None
    iface._current_project_path = None
    iface.footprint_library = get_library_manager(project_path=None)
    iface.library_commands = LibraryCommands(iface.footprint_library)
    iface.command_routes = {
        "list_library_footprints": iface.library_commands.list_library_footprints
    }
    monkeypatch.setattr(KiCADInterface, "_current_board_path", lambda self: board_path)

    out = iface.handle_command("list_library_footprints", {"library": "mylib"})

    assert out["success"] is True
    assert out["footprints"] == ["FOO"]


# ---------------------------------------------------------------------------
# _project_dir_for_library_scope: live board wins over stale _current_project_path
# ---------------------------------------------------------------------------
def test_library_scope_prefers_live_board_over_stale_project_path(tmp_path):
    """open_project set project A, but the user switched to board B in the KiCad
    UI (IPC) — library scope must follow the live board (B), not stale A."""
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface._current_project_path = tmp_path / "projA"
    board_b = str(tmp_path / "projB" / "board.kicad_pcb")
    iface._current_board_path = lambda: board_b  # type: ignore[method-assign]

    assert iface._project_dir_for_library_scope() == tmp_path / "projB"


def test_library_scope_falls_back_to_project_path_without_board(tmp_path):
    """No live board reachable → use the explicitly-opened project."""
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface._current_project_path = tmp_path / "projA"
    iface._current_board_path = lambda: None  # type: ignore[method-assign]

    assert iface._project_dir_for_library_scope() == tmp_path / "projA"


def test_library_scope_keeps_project_when_board_is_in_subdir(tmp_path):
    """A board inside a subdir of the opened project must still scope to the
    project ROOT (where fp-lib-table/sym-lib-table live), not the board subdir —
    the live board belongs to the opened project, so don't naively take its
    parent."""
    from kicad_interface import KiCADInterface

    proj = tmp_path / "proj"
    (proj / "boards").mkdir(parents=True)
    iface = KiCADInterface.__new__(KiCADInterface)
    iface._current_project_path = proj
    board = str(proj / "boards" / "main.kicad_pcb")
    iface._current_board_path = lambda: board  # type: ignore[method-assign]

    assert iface._project_dir_for_library_scope() == proj


# ---------------------------------------------------------------------------
# get_footprint_info not-found: clean failure, not NameError
# ---------------------------------------------------------------------------
def test_get_footprint_info_not_found_returns_clean_failure(no_global_fp_table):
    from commands.library import LibraryCommands, LibraryManager

    cmds = LibraryCommands(LibraryManager(project_path=None))

    # Must not raise NameError (the old bug) — returns a structured failure.
    out = cmds.get_footprint_info({"footprint_name": "NoSuchLib:NoSuchFoot"})

    assert out["success"] is False
    assert out["message"] == "Footprint not found"


def test_get_footprint_info_found_in_project_lib(no_global_fp_table, tmp_path):
    from commands.library import LibraryCommands, LibraryManager

    proj = _make_project(tmp_path)
    cmds = LibraryCommands(LibraryManager(project_path=proj))

    out = cmds.get_footprint_info({"footprint_name": "mylib:FOO"})

    assert out["success"] is True
    assert out["info"]["name"] == "FOO"
    assert out["info"]["library"] == "mylib"


# ---------------------------------------------------------------------------
# C10: register_footprint_library must validate the path before writing an entry
# ---------------------------------------------------------------------------
def _fp_table_text(project_dir: Path) -> str:
    tbl = project_dir / "fp-lib-table"
    return tbl.read_text(encoding="utf-8") if tbl.exists() else ""


def test_register_footprint_library_rejects_nonexistent_pretty(tmp_path):
    """A nonexistent .pretty must be refused (LIBRARY_NOT_FOUND), not written
    as a dangling fp-lib-table entry."""
    from commands.footprint import FootprintCreator

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "mini.kicad_pro").write_text("{}", encoding="utf-8")
    missing = proj / "does_not_exist.pretty"

    out = FootprintCreator().register_footprint_library(
        library_path=str(missing),
        scope="project",
        project_path=str(proj / "mini.kicad_pro"),
    )

    assert out["success"] is False
    assert out["errorCode"] == "LIBRARY_NOT_FOUND"
    # Nothing dangling was written.
    assert "does_not_exist" not in _fp_table_text(proj)


def test_register_footprint_library_rejects_wrong_type_file(tmp_path):
    """A real file that isn't a .pretty directory (e.g. a .kicad_pcb) is a
    type error (INVALID_LIBRARY_TYPE), not a silent .pretty coercion."""
    from commands.footprint import FootprintCreator

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "mini.kicad_pro").write_text("{}", encoding="utf-8")
    board = proj / "mini.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")

    out = FootprintCreator().register_footprint_library(
        library_path=str(board),
        scope="project",
        project_path=str(proj / "mini.kicad_pro"),
    )

    assert out["success"] is False
    assert out["errorCode"] == "INVALID_LIBRARY_TYPE"
    assert "mini" not in _fp_table_text(proj) or "kicad_pcb" not in _fp_table_text(proj)


def test_register_footprint_library_accepts_existing_pretty(tmp_path):
    """A real .pretty directory registers and lands in the project fp-lib-table."""
    from commands.footprint import FootprintCreator

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "mini.kicad_pro").write_text("{}", encoding="utf-8")
    pretty = proj / "custom.pretty"
    pretty.mkdir()

    out = FootprintCreator().register_footprint_library(
        library_path=str(pretty),
        scope="project",
        project_path=str(proj / "mini.kicad_pro"),
    )

    assert out["success"] is True
    assert out.get("already_registered") is False
    assert '(name "custom")' in _fp_table_text(proj)
