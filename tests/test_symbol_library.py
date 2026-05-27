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


# ---------------------------------------------------------------------------
# search_symbols: colon-prefix parsing + exact-name-beats-description ranking
# ---------------------------------------------------------------------------


def _make_synthetic_manager(library_contents):
    """Build a SymbolLibraryManager whose libraries serve canned SymbolInfo
    lists with no disk I/O.  list_symbols is monkey-patched on the
    instance so the mtime/cache plumbing is bypassed."""
    from commands.library_symbol import SymbolInfo, SymbolLibraryManager  # noqa: F401

    manager = SymbolLibraryManager.__new__(SymbolLibraryManager)
    manager.project_path = None
    manager.libraries = {nickname: f"/fake/{nickname}.kicad_sym" for nickname in library_contents}
    manager.symbol_cache = dict(library_contents)
    manager._cache_mtimes = {}
    manager._cache_dirty = False

    def _list(nickname):
        return library_contents.get(nickname, [])

    manager.list_symbols = _list  # type: ignore[method-assign]
    return manager


def _info(name, library, description="", value=""):
    from commands.library_symbol import SymbolInfo

    return SymbolInfo(
        name=name,
        library=library,
        full_ref=f"{library}:{name}",
        value=value,
        description=description,
    )


@pytest.mark.unit
class TestSearchSymbolsColonSyntax:
    """Regression for 'search_symbols 关键词匹配严重错乱' — Library:Symbol
    queries used to silently return 0 because the colon never appears in
    any scored field, and short queries like 'LED' got buried under
    description-substring hits."""

    def test_library_colon_name_query_finds_exact_symbol(self):
        manager = _make_synthetic_manager(
            {
                "Device": [_info("LED", "Device", description="Light emitting diode")],
                "Amplifier_Audio": [
                    _info("SSM2018", "Amplifier_Audio", description="Audio level controlled amp"),
                ],
            }
        )

        results = manager.search_symbols("Device:LED")

        assert [s.full_ref for s in results] == [
            "Device:LED"
        ], "Library:Symbol query must find the exact symbol in the named library"

    def test_library_colon_short_name_query_restricts_search(self):
        """`Device:R` should return Device:R, not 200 substring matches."""
        manager = _make_synthetic_manager(
            {
                "Device": [
                    _info("R", "Device", description="Resistor"),
                    _info("C", "Device", description="Capacitor"),
                ],
                "Amplifier_Audio": [
                    _info("SSM2018", "Amplifier_Audio", description="quaR-something"),
                    # noise that would substring-match 'r' in description
                ],
            }
        )

        results = manager.search_symbols("Device:R")
        names = [s.full_ref for s in results]
        assert "Device:R" in names
        # Must not include the Amplifier_Audio noise — it's outside the
        # library filter, regardless of how the score sorts.
        assert all(s.library == "Device" for s in results)

    def test_split_library_qualifier_falls_back_when_prefix_unknown(self):
        """`LM358:DR` must NOT be reinterpreted as library=LM358 — there
        is no such library.  The whole query passes through as a fuzzy
        name search (which finds nothing here, matching the historical
        behavior for inputs that happen to contain ':')."""
        manager = _make_synthetic_manager({"Device": [_info("R", "Device")]})

        name, prefix = manager.split_library_qualifier("LM358:DR")

        assert name == "LM358:DR"
        assert prefix is None

    def test_exact_name_match_beats_description_substring(self):
        """`LED` query must return Device:LED first, not the description-
        substring noise that used to fill the limit*3 budget before the
        exact match was reached.  This is the early-break regression."""
        # Inject a library whose name iterates BEFORE 'Device' in dict
        # order, packed with description-substring hits.  Dict insertion
        # order is preserved in Python 3.7+, so this faithfully
        # reproduces the production iteration order.
        noisy_lib = [
            _info(f"74LS{i:03d}", "Logic_TTL", description="controlled flip-flop with led pin")
            for i in range(80)
        ]
        manager = _make_synthetic_manager(
            {
                "Logic_TTL": noisy_lib,
                "Device": [_info("LED", "Device", description="Light emitting diode")],
            }
        )

        results = manager.search_symbols("LED", limit=20)

        names = [s.full_ref for s in results]
        assert "Device:LED" in names, (
            "Exact-name match must be returned; previously the early-break "
            "consumed the budget on Logic_TTL description-substring hits "
            "and never reached the Device library."
        )
        # The exact match must come first (score 500 vs score 50).
        assert names[0] == "Device:LED"

    def test_explicit_library_filter_overrides_scope_but_still_strips_prefix(self):
        """`query='Device:LED' library='Amplifier_Audio'`:

          - library scope = Amplifier_Audio (explicit param wins)
          - name part      = 'LED' (the inline 'Device:' is stripped)

        Previously the literal 'Device:LED' was fed to the scorer; no
        field contains ':' so the result was silently 0 — the very same
        shape of bug the colon-parsing fix was meant to address, just on
        the explicit-filter path."""
        manager = _make_synthetic_manager(
            {
                "Device": [_info("LED", "Device", description="Light emitting diode")],
                "Amplifier_Audio": [
                    _info("AD8628_LED_driver", "Amplifier_Audio", description="LED driver"),
                ],
            }
        )

        results = manager.search_symbols("Device:LED", library_filter="Amplifier_Audio")

        assert [s.full_ref for s in results] == ["Amplifier_Audio:AD8628_LED_driver"], (
            "explicit filter must scope to Amplifier_Audio AND the inline "
            "'Device:' prefix must be stripped so 'LED' actually matches"
        )

    def test_exact_nickname_match_preferred_over_substring(self):
        """`Device:R` must hit the library named exactly `Device`, not
        also pull in `Device_Extras` / `Device_2` which would silently
        widen the result set."""
        manager = _make_synthetic_manager(
            {
                "Device": [_info("R", "Device", description="Resistor")],
                "Device_Extras": [_info("R_packarray", "Device_Extras")],
            }
        )

        results = manager.search_symbols("Device:R")

        assert all(s.library == "Device" for s in results)


@pytest.mark.unit
class TestSearchSymbolsHandlerInterpretation:
    """The handler must surface the parsed library/name so agents can
    confirm the colon parse matched what they meant — without that, an
    unhelpful 0-result response looks like 'symbol doesn't exist' even
    when the real cause is a typo in the library prefix."""

    def test_response_includes_interpretation_when_colon_parsed(self):
        from commands.library_symbol import SymbolLibraryCommands

        cmds = SymbolLibraryCommands.__new__(SymbolLibraryCommands)
        cmds.library_manager = _make_synthetic_manager(
            {"Device": [_info("LED", "Device", description="Light emitting diode")]}
        )
        cmds._ensure_manager_for = lambda params: None  # type: ignore[attr-defined]

        response = cmds.search_symbols({"query": "Device:LED"})

        assert response["success"] is True
        assert response["count"] == 1
        assert response["interpretation"] == {
            "parsedAs": "library:name",
            "library": "Device",
            "name": "LED",
        }

    def test_response_warns_when_library_filter_matches_nothing(self):
        from commands.library_symbol import SymbolLibraryCommands

        cmds = SymbolLibraryCommands.__new__(SymbolLibraryCommands)
        cmds.library_manager = _make_synthetic_manager(
            {"Device": [_info("LED", "Device", description="Light emitting diode")]}
        )
        cmds._ensure_manager_for = lambda params: None  # type: ignore[attr-defined]

        response = cmds.search_symbols({"query": "LED", "library": "NoSuchLibrary"})

        assert response["success"] is True
        assert response["count"] == 0
        assert "warning" in response
        assert "NoSuchLibrary" in response["warning"]
        assert "list_symbol_libraries" in response["warning"]

    def test_response_has_no_interpretation_for_plain_query(self):
        """Plain 'ESP32'-style queries shouldn't gain noise fields."""
        from commands.library_symbol import SymbolLibraryCommands

        cmds = SymbolLibraryCommands.__new__(SymbolLibraryCommands)
        cmds.library_manager = _make_synthetic_manager(
            {"Device": [_info("LED", "Device", description="Light emitting diode")]}
        )
        cmds._ensure_manager_for = lambda params: None  # type: ignore[attr-defined]

        response = cmds.search_symbols({"query": "LED"})

        assert response["success"] is True
        assert "interpretation" not in response
        assert "warning" not in response


# ---------------------------------------------------------------------------
# Edge cases the first pass missed (gap-sweep findings)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSearchSymbolsEdgeCases:
    def test_combined_library_filter_with_colon_prefix_strips_and_overrides(self):
        """`query='Device:LED' library='JLCPCB'`:
        - searches JLCPCB libraries
        - searches for 'LED' (NOT the un-matchable literal 'Device:LED')
        - response includes interpretation.note explaining the override
        """
        from commands.library_symbol import SymbolLibraryCommands

        cmds = SymbolLibraryCommands.__new__(SymbolLibraryCommands)
        cmds.library_manager = _make_synthetic_manager(
            {
                "Device": [_info("LED", "Device", description="Light emitting diode")],
                "JLCPCB-LEDs": [
                    _info("WS2812B", "JLCPCB-LEDs", description="addressable RGB LED"),
                ],
            }
        )
        cmds._ensure_manager_for = lambda params: None  # type: ignore[attr-defined]

        response = cmds.search_symbols({"query": "Device:LED", "library": "JLCPCB"})

        assert response["success"] is True
        # Searched JLCPCB-LEDs (library filter), found WS2812B via 'LED'
        # description match — proves the prefix was stripped.
        assert {s["full_ref"] for s in response["symbols"]} == {"JLCPCB-LEDs:WS2812B"}
        interp = response["interpretation"]
        assert interp["library"] == "JLCPCB"
        assert interp["name"] == "LED"
        assert "Device" in interp["note"]
        assert "JLCPCB" in interp["note"]

    def test_query_with_empty_name_after_colon_falls_back_to_literal(self):
        """`query='Device:'` (empty name part) must not crash and must
        not silently treat itself as "all symbols in Device" — that's
        what list_library_symbols is for.  Falls back to fuzzy match of
        the literal 'Device:', which matches nothing in normal data."""
        manager = _make_synthetic_manager(
            {"Device": [_info("LED", "Device", description="Light emitting diode")]}
        )

        name, prefix = manager.split_library_qualifier("Device:")

        assert name == "Device:"
        assert prefix is None

        results = manager.search_symbols("Device:")
        assert results == []

    def test_query_with_empty_prefix_before_colon_falls_back_to_literal(self):
        """`query=':LED'` (empty library prefix) must not split into
        ('', 'LED').  Falls back to fuzzy match of ':LED'."""
        manager = _make_synthetic_manager(
            {"Device": [_info("LED", "Device", description="Light emitting diode")]}
        )

        name, prefix = manager.split_library_qualifier(":LED")

        assert name == ":LED"
        assert prefix is None

    def test_broad_short_query_returns_exact_match_first(self):
        """Single-letter `'R'` query: the old early-break could let
        description-substring hits fill the budget before the exact
        Device:R was scored.  The heapq.nlargest path must keep the
        exact match (score 500) on top across 10k+ noise symbols."""
        # 5000 symbols whose description contains 'r' (matches "transistor")
        noise = [
            _info(f"Q{i}", "Transistor_BJT", description="bipolar transistor NPN")
            for i in range(5000)
        ]
        manager = _make_synthetic_manager(
            {
                "Transistor_BJT": noise,
                "Device": [_info("R", "Device", description="Resistor")],
            }
        )

        results = manager.search_symbols("R", limit=5)

        assert results[0].full_ref == "Device:R"
        # Even with 5000 noise hits, the heap keeps just the top 5.
        assert len(results) == 5

    def test_handler_interpretation_carries_override_note_when_both_supplied(self):
        """Regression for the gap: previously `query='Device:LED'
        library='JLCPCB'` returned no interpretation at all because the
        early-out compared interpreted_library to library_filter.  The
        plan-based handler must surface the inline_prefix override."""
        from commands.library_symbol import SymbolLibraryCommands

        cmds = SymbolLibraryCommands.__new__(SymbolLibraryCommands)
        cmds.library_manager = _make_synthetic_manager(
            {
                "Device": [_info("LED", "Device", description="Light emitting diode")],
                "JLCPCB-LEDs": [_info("WS2812B", "JLCPCB-LEDs", description="LED")],
            }
        )
        cmds._ensure_manager_for = lambda params: None  # type: ignore[attr-defined]

        response = cmds.search_symbols({"query": "Device:LED", "library": "JLCPCB"})

        assert "interpretation" in response
        assert "note" in response["interpretation"]
