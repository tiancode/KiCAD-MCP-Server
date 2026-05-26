"""
Regression tests for symbol library parsing.

Covers:
- The 5000-char heuristic bug where PJFET properties bled into the OPAMP block.
- The sim_pins field exposed from Sim.Pins properties.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.library_symbol import SymbolLibraryCommands, SymbolLibraryManager

FIXTURE = Path(__file__).parent / "fixtures" / "Simulation_SPICE_minimal.kicad_sym"
SPICE_LIB = Path("/usr/share/kicad/symbols/Simulation_SPICE.kicad_sym")


def _manager_for_fixture() -> SymbolLibraryManager:
    manager = SymbolLibraryManager.__new__(SymbolLibraryManager)
    manager.project_path = None
    manager.libraries = {"Simulation_SPICE": str(FIXTURE)}
    manager.symbol_cache = {}
    # Disk-cache instance state — list_symbols() validates and updates
    # these even on bare fixture managers built without __init__.
    manager._cache_mtimes = {}
    manager._cache_dirty = False
    return manager


@pytest.mark.unit
class TestSymbolLibraryManagerParsing:
    """Unit tests using the minimal fixture file (no disk I/O to system libs)."""

    def test_both_symbols_present(self):
        manager = _manager_for_fixture()
        symbols = manager.list_symbols("Simulation_SPICE")
        names = [s.name for s in symbols]
        assert "OPAMP" in names
        assert "PJFET" in names
        assert len(symbols) == 2

    def test_opamp_description_is_not_pjfet(self):
        manager = _manager_for_fixture()
        symbols = manager.list_symbols("Simulation_SPICE")
        opamp = next(s for s in symbols if s.name == "OPAMP")
        assert "JFET" not in opamp.description

    def test_opamp_description_correct(self):
        manager = _manager_for_fixture()
        symbols = manager.list_symbols("Simulation_SPICE")
        opamp = next(s for s in symbols if s.name == "OPAMP")
        assert opamp.description == "Operational amplifier, single"

    def test_opamp_value_correct(self):
        manager = _manager_for_fixture()
        symbols = manager.list_symbols("Simulation_SPICE")
        opamp = next(s for s in symbols if s.name == "OPAMP")
        assert opamp.value == "${SIM.PARAMS}"

    def test_opamp_sim_pins_exposed(self):
        manager = _manager_for_fixture()
        symbols = manager.list_symbols("Simulation_SPICE")
        opamp = next(s for s in symbols if s.name == "OPAMP")
        assert opamp.sim_pins == "1=in+ 2=in- 3=vcc 4=vee 5=out"

    def test_pjfet_description_correct(self):
        manager = _manager_for_fixture()
        symbols = manager.list_symbols("Simulation_SPICE")
        pjfet = next(s for s in symbols if s.name == "PJFET")
        assert pjfet.description == "P-JFET transistor, for simulation only"

    def test_pjfet_value_correct(self):
        manager = _manager_for_fixture()
        symbols = manager.list_symbols("Simulation_SPICE")
        pjfet = next(s for s in symbols if s.name == "PJFET")
        assert pjfet.value == "PJFET"

    def test_pjfet_sim_pins_exposed(self):
        manager = _manager_for_fixture()
        symbols = manager.list_symbols("Simulation_SPICE")
        pjfet = next(s for s in symbols if s.name == "PJFET")
        assert pjfet.sim_pins == "1=D 2=G 3=S"


@pytest.mark.integration
class TestGetSymbolInfoHandler:
    """Integration tests against the real system Simulation_SPICE library."""

    def test_opamp_via_commands_handler(self):
        if not SPICE_LIB.exists():
            pytest.skip(f"System library not found: {SPICE_LIB}")
        manager = SymbolLibraryManager.__new__(SymbolLibraryManager)
        manager.project_path = None
        manager.libraries = {"Simulation_SPICE": str(SPICE_LIB)}
        manager.symbol_cache = {}
        manager._cache_mtimes = {}
        manager._cache_dirty = False
        commands = SymbolLibraryCommands(library_manager=manager)
        result = commands.get_symbol_info({"symbol": "Simulation_SPICE:OPAMP"})
        assert result["success"] is True
        info = result["symbol_info"]
        assert info["description"] == "Operational amplifier, single"
        assert "JFET" not in info["description"]
        assert info["value"] == "${SIM.PARAMS}"
        assert info["sim_pins"] == "1=in+ 2=in- 3=vcc 4=vee 5=out"

    def test_pjfet_via_commands_handler(self):
        if not SPICE_LIB.exists():
            pytest.skip(f"System library not found: {SPICE_LIB}")
        manager = SymbolLibraryManager.__new__(SymbolLibraryManager)
        manager.project_path = None
        manager.libraries = {"Simulation_SPICE": str(SPICE_LIB)}
        manager.symbol_cache = {}
        manager._cache_mtimes = {}
        manager._cache_dirty = False
        commands = SymbolLibraryCommands(library_manager=manager)
        result = commands.get_symbol_info({"symbol": "Simulation_SPICE:PJFET"})
        assert result["success"] is True
        info = result["symbol_info"]
        assert info["description"] == "P-JFET transistor, for simulation only"
        assert info["value"] == "PJFET"


@pytest.mark.unit
class TestSymbolDiskCache:
    """Regression tests for the lazy + persistent symbol-library cache.

    The default startup path now skips _warm_cache (was 30-120 s on a real
    KiCAD install with 200+ libraries); cached symbol data is restored from
    ~/.kicad-mcp/cache/symbol_libraries.pickle and validated per-library by
    mtime on list_symbols().  Eager warming is opt-in via
    KICAD_MCP_EAGER_SYMBOL_CACHE=1.
    """

    def test_default_init_skips_warm_cache(self, monkeypatch, tmp_path):
        """KiCADInterface() startup must not block on _warm_cache by default."""
        monkeypatch.delenv("KICAD_MCP_EAGER_SYMBOL_CACHE", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        import commands.library_symbol as lib_sym

        monkeypatch.setattr(
            lib_sym,
            "_DISK_CACHE_PATH",
            tmp_path / ".kicad-mcp" / "cache" / "symbol_libraries.pickle",
        )
        # _warm_cache must not be called from __init__.
        warmed = {"count": 0}
        orig_warm = lib_sym.SymbolLibraryManager._warm_cache

        def spy(self):
            warmed["count"] += 1
            return orig_warm(self)

        monkeypatch.setattr(lib_sym.SymbolLibraryManager, "_warm_cache", spy)
        lib_sym.SymbolLibraryManager()
        assert warmed["count"] == 0, "warm_cache must be opt-in"

    def test_eager_env_var_triggers_warm_cache(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KICAD_MCP_EAGER_SYMBOL_CACHE", "1")
        monkeypatch.setenv("HOME", str(tmp_path))
        import commands.library_symbol as lib_sym

        monkeypatch.setattr(
            lib_sym,
            "_DISK_CACHE_PATH",
            tmp_path / ".kicad-mcp" / "cache" / "symbol_libraries.pickle",
        )
        warmed = {"count": 0}

        def spy(self):
            warmed["count"] += 1

        monkeypatch.setattr(lib_sym.SymbolLibraryManager, "_warm_cache", spy)
        lib_sym.SymbolLibraryManager()
        assert warmed["count"] == 1, "warm_cache must be called when env var is set"

    def test_list_symbols_reparses_when_source_mtime_advances(self, tmp_path):
        """A library whose .kicad_sym was modified since cache write must re-parse."""
        import os
        import shutil

        import commands.library_symbol as lib_sym

        lib_file = tmp_path / "Simulation_SPICE.kicad_sym"
        shutil.copy(FIXTURE, lib_file)

        mgr = SymbolLibraryManager.__new__(SymbolLibraryManager)
        mgr.project_path = None
        mgr.libraries = {"Simulation_SPICE": str(lib_file)}
        mgr.symbol_cache = {}
        mgr._cache_mtimes = {}
        mgr._cache_dirty = False

        # First call parses and records the source mtime.
        first = mgr.list_symbols("Simulation_SPICE")
        assert len(first) > 0
        assert "Simulation_SPICE" in mgr._cache_mtimes

        # Touch the file so its mtime advances; cache entry must be invalidated.
        new_mtime = mgr._cache_mtimes["Simulation_SPICE"] + 10_000_000_000  # +10s
        os.utime(lib_file, ns=(new_mtime, new_mtime))

        # Track parse calls
        parse_calls = {"count": 0}
        orig = lib_sym.SymbolLibraryManager._parse_kicad_sym_file

        def spy(self, lib_path, lib_name):
            parse_calls["count"] += 1
            return orig(self, lib_path, lib_name)

        lib_sym.SymbolLibraryManager._parse_kicad_sym_file = spy  # type: ignore
        try:
            second = mgr.list_symbols("Simulation_SPICE")
        finally:
            lib_sym.SymbolLibraryManager._parse_kicad_sym_file = orig  # type: ignore

        assert parse_calls["count"] == 1, "stale cache entry must re-parse"
        assert len(second) == len(first)

    def test_disk_cache_roundtrip(self, monkeypatch, tmp_path):
        """Write disk cache, load it from a fresh instance, expect no parsing."""
        import commands.library_symbol as lib_sym

        cache_path = tmp_path / ".kicad-mcp" / "cache" / "symbol_libraries.pickle"
        monkeypatch.setattr(lib_sym, "_DISK_CACHE_PATH", cache_path)

        mgr1 = SymbolLibraryManager.__new__(SymbolLibraryManager)
        mgr1.project_path = None
        mgr1.libraries = {"Simulation_SPICE": str(FIXTURE)}
        mgr1.symbol_cache = {}
        mgr1._cache_mtimes = {}
        mgr1._cache_dirty = False
        mgr1.list_symbols("Simulation_SPICE")  # populates both tiers
        mgr1._save_disk_cache()
        assert cache_path.exists(), "atexit save must have written the pickle"

        # Fresh manager — its _load_disk_cache should pre-populate symbol_cache.
        mgr2 = SymbolLibraryManager.__new__(SymbolLibraryManager)
        mgr2.project_path = None
        mgr2.libraries = {"Simulation_SPICE": str(FIXTURE)}
        mgr2.symbol_cache = {}
        mgr2._cache_mtimes = {}
        mgr2._cache_dirty = False
        mgr2._load_disk_cache()
        assert "Simulation_SPICE" in mgr2.symbol_cache, "disk cache must restore entry"

        # list_symbols on the restored entry must NOT call the parser.
        parse_calls = {"count": 0}
        orig = lib_sym.SymbolLibraryManager._parse_kicad_sym_file

        def spy(self, lib_path, lib_name):
            parse_calls["count"] += 1
            return orig(self, lib_path, lib_name)

        lib_sym.SymbolLibraryManager._parse_kicad_sym_file = spy  # type: ignore
        try:
            symbols = mgr2.list_symbols("Simulation_SPICE")
        finally:
            lib_sym.SymbolLibraryManager._parse_kicad_sym_file = orig  # type: ignore

        assert parse_calls["count"] == 0, "warm disk cache must serve without parsing"
        assert len(symbols) > 0
