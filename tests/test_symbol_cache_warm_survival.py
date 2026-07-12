"""The background symbol warm must survive a manager rebuild (open_project).

Regression for the perf bug where ``open_project`` -> ``use_project()`` rebuilt
the ``SymbolLibraryManager`` and discarded the in-memory cache the background
warm had populated — so the common flow (open a project, then search) still paid
a full ~17 s cold parse on the first search.

Fix: a process-wide, path-keyed shared parsed-symbol store that every manager
publishes into and hydrates from at construction, plus a persist-early flush of
the disk pickle when the warm completes. This file pins:

  (a) warm -> use_project() -> search reuses the warmed libraries (no re-parse)
  (b) warm completion persists the pickle immediately (not only at atexit)
  (c) project-specific libraries still load correctly after the reuse
  (d) an external .kicad_sym edit after the warm still invalidates that library

pcbnew is stubbed globally by tests/conftest.py; the shared store is reset per
test by the autouse fixture there. No real KiCad needed.
"""

from __future__ import annotations

import os
import pickle
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

import commands.library_symbol as lib_sym  # noqa: E402
import commands.library_symbol._core as lib_core  # noqa: E402
from commands.library_symbol import (  # noqa: E402
    SymbolLibraryCommands,
    SymbolLibraryManager,
    start_background_symbol_warm,
)


def _write_lib(path: Path, symbols) -> Path:
    parts = ["(kicad_symbol_lib (version 20231120) (generator test)"]
    for sym in symbols:
        parts.append(
            f'  (symbol "{sym}" '
            f'(property "Reference" "U" (at 0 0 0)) '
            f'(property "Value" "{sym}" (at 0 0 0)))'
        )
    parts.append(")")
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def _global_env(monkeypatch, tmp_path):
    """Set up two global .kicad_sym libraries + a project sym-lib-table.

    Returns (proj_dir, {nickname: path}). Isolates the disk pickle to tmp and
    points the manager's global-table lookup at a table listing GlobA/GlobB.
    """
    monkeypatch.delenv("KICAD_MCP_EAGER_SYMBOL_CACHE", raising=False)
    monkeypatch.delenv("KICAD_MCP_BG_SYMBOL_WARM", raising=False)
    monkeypatch.setattr(
        lib_sym._manager_loading,
        "_DISK_CACHE_PATH",
        tmp_path / ".kicad-mcp" / "cache" / "symbol_libraries.pickle",
    )

    symdir = tmp_path / "symbols"
    symdir.mkdir()
    glob_a = _write_lib(symdir / "GlobA.kicad_sym", ["GLOBSYMA"])
    glob_b = _write_lib(symdir / "GlobB.kicad_sym", ["GLOBSYMB"])

    config = tmp_path / "config"
    config.mkdir()
    global_table = config / "sym-lib-table"
    global_table.write_text(
        "(sym_lib_table\n"
        f'  (lib (name "GlobA")(type "KiCad")(uri "{glob_a}")(options "")(descr ""))\n'
        f'  (lib (name "GlobB")(type "KiCad")(uri "{glob_b}")(options "")(descr ""))\n'
        ")\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        SymbolLibraryManager, "_get_global_sym_lib_table", lambda self: global_table
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
    return proj, {"GlobA": str(glob_a), "GlobB": str(glob_b)}


@pytest.mark.unit
class TestWarmSurvivesManagerRebuild:
    def test_use_project_reuses_warmed_global_libs(self, monkeypatch, tmp_path):
        """(a)+(c): after warm, open_project's rebuild reuses the warmed global
        libraries (0 re-parses) while the project library still loads."""
        proj, globs = _global_env(monkeypatch, tmp_path)

        default_mgr = SymbolLibraryManager(project_path=None)
        assert {"GlobA", "GlobB"} <= set(default_mgr.libraries)
        assert "mylib" not in default_mgr.libraries  # global-only scope

        default_mgr._warm_cache()  # parses + publishes GlobA/GlobB to shared store
        assert {"GlobA", "GlobB"} <= set(default_mgr.symbol_cache)

        cmds = SymbolLibraryCommands(default_mgr)

        # Record every parse from here on (after the warm).
        parsed: list = []
        orig_parse = SymbolLibraryManager._parse_kicad_sym_file

        def _spy(self, path, name):
            parsed.append(name)
            return orig_parse(self, path, name)

        monkeypatch.setattr(SymbolLibraryManager, "_parse_kicad_sym_file", _spy)

        cmds.use_project(proj)  # rebuild for project scope
        assert cmds.library_manager is not default_mgr
        assert {"GlobA", "GlobB", "mylib"} <= set(cmds.library_manager.libraries)

        # (a) A global search must hit the reused cache: GlobA/GlobB NOT re-parsed.
        res = cmds.search_symbols({"query": "GLOBSYMA"})
        assert res["success"] is True
        assert any(s["name"] == "GLOBSYMA" for s in res["symbols"]), res
        assert "GlobA" not in parsed, f"warmed global lib re-parsed after rebuild: {parsed}"
        assert "GlobB" not in parsed, f"warmed global lib re-parsed after rebuild: {parsed}"

        # (c) The project-specific library still loads correctly (it was not
        # warmed, so it parses lazily — exactly once).
        res2 = cmds.search_symbols({"query": "WIDGET"})
        assert res2["success"] is True
        assert any(s["name"] == "WIDGET" for s in res2["symbols"]), res2
        assert parsed.count("mylib") == 1

        # Neutralize atexit flushes so the throwaway libs never touch the real cache.
        default_mgr._cache_dirty = False
        cmds.library_manager._cache_dirty = False

    def test_external_edit_after_warm_still_invalidates(self, monkeypatch, tmp_path):
        """(d): editing a .kicad_sym after the warm forces a re-parse of ONLY
        that library on the next rebuilt manager; siblings stay reused."""
        proj, globs = _global_env(monkeypatch, tmp_path)

        default_mgr = SymbolLibraryManager(project_path=None)
        default_mgr._warm_cache()
        warm_mtime = default_mgr._cache_mtimes["GlobA"]

        # Edit GlobA on disk (new symbol) and advance its mtime past the warm.
        _write_lib(Path(globs["GlobA"]), ["GLOBSYMA", "GLOBSYMA2"])
        new_mtime = warm_mtime + 10_000_000_000  # +10 s, coarse-fs safe
        os.utime(globs["GlobA"], ns=(new_mtime, new_mtime))

        cmds = SymbolLibraryCommands(default_mgr)

        parsed: list = []
        orig_parse = SymbolLibraryManager._parse_kicad_sym_file

        def _spy(self, path, name):
            parsed.append(name)
            return orig_parse(self, path, name)

        monkeypatch.setattr(SymbolLibraryManager, "_parse_kicad_sym_file", _spy)

        cmds.use_project(proj)  # rebuild; hydration must skip the edited GlobA
        res = cmds.search_symbols({"query": "GLOBSYMA2"})
        assert res["success"] is True
        assert any(s["name"] == "GLOBSYMA2" for s in res["symbols"]), res

        assert "GlobA" in parsed, "edited library must re-parse (mtime moved)"
        assert "GlobB" not in parsed, "untouched sibling must stay reused"

        default_mgr._cache_dirty = False
        cmds.library_manager._cache_dirty = False


@pytest.mark.unit
class TestWarmPersistsPickleImmediately:
    def test_background_warm_flushes_disk_cache_on_completion(self, monkeypatch, tmp_path):
        """(b): start_background_symbol_warm persists the pickle as soon as the
        warm completes, not only at atexit."""
        monkeypatch.delenv("KICAD_MCP_EAGER_SYMBOL_CACHE", raising=False)
        monkeypatch.delenv("KICAD_MCP_BG_SYMBOL_WARM", raising=False)
        cache_path = tmp_path / ".kicad-mcp" / "cache" / "symbol_libraries.pickle"
        monkeypatch.setattr(lib_sym._manager_loading, "_DISK_CACHE_PATH", cache_path)

        lib_a = _write_lib(tmp_path / "LibA.kicad_sym", ["R"])
        # Bare (__new__) manager: no atexit registration, so it can't touch the
        # real cache; the persist-early path is exercised via the bg-warm _run.
        mgr = SymbolLibraryManager.__new__(SymbolLibraryManager)
        mgr.project_path = None
        mgr.libraries = {"LibA": str(lib_a)}
        mgr.symbol_cache = {}
        mgr._cache_mtimes = {}
        mgr._cache_dirty = False

        monkeypatch.setattr(lib_core, "get_symbol_library_manager", lambda: mgr)

        assert not cache_path.exists()
        thread = start_background_symbol_warm()
        assert thread is not None
        thread.join(timeout=5)
        assert not thread.is_alive()

        assert cache_path.exists(), "warm completion must persist the pickle immediately"
        data = pickle.loads(cache_path.read_bytes())
        assert data["version"] == 2, "disk cache version must stay 2 (old-pickle rejection)"
        assert "LibA" in data["symbol_cache"], "persisted pickle must contain the warmed lib"

        mgr._cache_dirty = False
