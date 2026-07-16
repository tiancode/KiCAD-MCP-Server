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
ESCAPED_FIXTURE = Path(__file__).parent / "fixtures" / "escaped_quotes.kicad_sym"
UNBALANCED_FIXTURE = Path(__file__).parent / "fixtures" / "unbalanced_parens.kicad_sym"
LCSC_FIXTURE = Path(__file__).parent / "fixtures" / "lcsc_part.kicad_sym"


def _discover_stock_lib(filename: str):
    """Locate a stock ``.kicad_sym`` library cross-platform.

    Reuses production's ``PlatformHelper`` search patterns instead of hardcoding
    the Linux ``/usr/share/kicad/symbols`` path, so it resolves the macOS bundled
    libraries (``KiCad.app/.../SharedSupport/symbols``) and Windows installs too.
    Returns the first matching path (may or may not exist)."""
    import glob

    from utils.platform_helper import PlatformHelper

    for pattern in PlatformHelper.get_kicad_library_search_paths():
        for hit in glob.glob(pattern):
            if Path(hit).name == filename:
                return Path(hit)
    return Path("/nonexistent") / filename


SPICE_LIB = _discover_stock_lib("Simulation_SPICE.kicad_sym")


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


@pytest.mark.unit
class TestEscapedQuoteDescriptions:
    """F7: power-symbol descriptions embed escaped quotes, e.g.
    ``"... with name \\"+5V\\""``.  The old ``"([^"]*)"`` capture truncated
    them at the first inner quote (``... with name \\``); the fix tolerates
    the escapes and unescapes the captured text.  Full round-trip through
    list_symbols -> _parse_kicad_sym_file -> _extract_properties."""

    def _manager(self):
        manager = SymbolLibraryManager.__new__(SymbolLibraryManager)
        manager.project_path = None
        manager.libraries = {"escaped": str(ESCAPED_FIXTURE)}
        manager.symbol_cache = {}
        manager._cache_mtimes = {}
        manager._cache_dirty = False
        return manager

    def test_escaped_quote_description_round_trips(self):
        symbols = self._manager().list_symbols("escaped")
        plus5 = next(s for s in symbols if s.name == "+5V")
        assert plus5.description == 'Power symbol creates a global label with name "+5V"'

    def test_backslash_and_quote_description_round_trips(self):
        symbols = self._manager().list_symbols("escaped")
        pathy = next(s for s in symbols if s.name == "PATHY")
        assert pathy.description == r'Win path C:\Users and a "quoted" bit'

    def test_all_symbols_parsed_despite_escapes(self):
        """A truncated value must not swallow the rest of the block and
        drop the following symbol."""
        names = [s.name for s in self._manager().list_symbols("escaped")]
        assert names == ["+5V", "PATHY"]


@pytest.mark.unit
class TestUnbalancedParensInStrings:
    """The block walker counts paren depth to find each symbol's extent.

    KiCad ``.kicad_sym`` files embed unbalanced parens inside quoted string
    values — pin names such as ``"PA13(JTMS"`` / ``"PA14(JTCK"`` and
    descriptions like ``"... MCU (LQFP48"``.  A string-*unaware* counter
    mis-tracks depth on those, walks past the true block end (to EOF), logs
    "Malformed symbol block", and silently drops the symbol — which made
    whole libraries (MCU_ST_STM32H5, 73 symbols) unsearchable.  The walker
    is now string-aware; this pins that every symbol is recovered with the
    right name/description despite the tricky strings and any symbols that
    follow them.
    """

    def _manager(self):
        manager = SymbolLibraryManager.__new__(SymbolLibraryManager)
        manager.project_path = None
        manager.libraries = {"tricky": str(UNBALANCED_FIXTURE)}
        manager.symbol_cache = {}
        manager._cache_mtimes = {}
        manager._cache_dirty = False
        return manager

    def test_every_symbol_recovered(self):
        """No symbol is dropped even though the first one has two unbalanced
        ``(`` in pin-name strings (net +2 that the naive walker never
        unwinds, so it ran to EOF and skipped the symbol)."""
        names = [s.name for s in self._manager().list_symbols("tricky")]
        assert names == [
            "STM32H5xx_TRICKY",
            "ESCAPED_TRICKY",
            "PLAIN_C",
            "PLAIN_D",
            "PLAIN_E",
        ]

    def test_unbalanced_paren_symbol_description(self):
        symbols = self._manager().list_symbols("tricky")
        tricky = next(s for s in symbols if s.name == "STM32H5xx_TRICKY")
        assert tricky.description == "Arm Cortex-M33 MCU (LQFP48"

    def test_escaped_quote_and_paren_in_same_string(self):
        """Case (b): an escaped quote AND a paren in one value.  The block
        must stay correctly bounded (string-aware walk) and the value must
        round-trip with the escape decoded and the paren preserved."""
        symbols = self._manager().list_symbols("tricky")
        esc = next(s for s in symbols if s.name == "ESCAPED_TRICKY")
        assert esc.description == 'Active-low "reset" (see datasheet'

    def test_symbols_after_tricky_ones_intact(self):
        """The over-long block the naive walker produced for the tricky
        symbols must not corrupt the plainly-defined symbols that follow."""
        symbols = self._manager().list_symbols("tricky")
        by_name = {s.name: s for s in symbols}
        assert by_name["PLAIN_C"].description == "Plain resistor after the tricky ones"
        assert by_name["PLAIN_D"].description == "Capacitor, 100nF"
        assert by_name["PLAIN_E"].description == "LED (red)"

    def test_get_symbol_pins_string_aware_slice(self):
        """get_symbol_pins uses its own paren walker to slice one block;
        it must also skip string parens so the tricky symbol's pins parse."""
        pins = self._manager().get_symbol_pins("tricky", "STM32H5xx_TRICKY")
        assert pins is not None
        names = {p["name"] for p in pins}
        assert names == {"PA13(JTMS", "PA14(JTCK"}


@pytest.mark.unit
class TestLcscPartProperty:
    """easyeda2kicad writes the LCSC id as the property ``"LCSC Part"`` (not
    ``"LCSC"``), so reading only ``"LCSC"`` left every imported symbol with an
    empty ``lcsc_id`` and unsearchable by its LCSC number.  The parser now
    accepts both names."""

    def _manager(self):
        manager = SymbolLibraryManager.__new__(SymbolLibraryManager)
        manager.project_path = None
        manager.libraries = {"lcsc": str(LCSC_FIXTURE)}
        manager.symbol_cache = {}
        manager._cache_mtimes = {}
        manager._cache_dirty = False
        return manager

    def test_lcsc_part_property_populates_lcsc_id(self):
        symbols = self._manager().list_symbols("lcsc")
        part = next(s for s in symbols if s.name == "0603WAF1002T5E")
        assert part.lcsc_id == "C25804"

    def test_plain_lcsc_property_still_populates_lcsc_id(self):
        symbols = self._manager().list_symbols("lcsc")
        plain = next(s for s in symbols if s.name == "PLAIN_LCSC")
        assert plain.lcsc_id == "C11702"

    def test_searchable_by_lcsc_number(self):
        """The whole point: search_symbols('C25804') finds the imported part
        via its LCSC id (previously 0 results because lcsc_id was empty)."""
        results = self._manager().search_symbols("C25804")
        assert "lcsc:0603WAF1002T5E" in [s.full_ref for s in results]

    def test_lcsc_from_properties_helper(self):
        from commands.library_symbol._manager_parsing import _lcsc_from_properties

        assert _lcsc_from_properties({"LCSC Part": "C25804"}) == "C25804"
        assert _lcsc_from_properties({"LCSC": "C11702"}) == "C11702"
        # "LCSC Part" wins when both are present.
        assert _lcsc_from_properties({"LCSC": "C1", "LCSC Part": "C2"}) == "C2"
        assert _lcsc_from_properties({"Value": "R"}) == ""


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
            lib_sym._manager_loading,
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
            lib_sym._manager_loading,
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
        monkeypatch.setattr(lib_sym._manager_loading, "_DISK_CACHE_PATH", cache_path)

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


@pytest.mark.unit
class TestSearchSymbolsMultiToken:
    """Regression for the multi-token failure: ``search_symbols("VCC power",
    library="power")`` returned 0 hits because the full string was
    substring-matched against every field as one token.  Whitespace now
    splits the query into tokens with strict AND semantics."""

    def test_multi_token_matches_via_name_and_library_nickname(self):
        """``"VCC power"`` should find ``power:VCC`` — token 'VCC' hits the
        name, token 'power' hits the library nickname."""
        manager = _make_synthetic_manager(
            {
                "power": [
                    _info("VCC", "power", description="Power symbol"),
                    _info("+3V3", "power"),
                ],
                "Device": [_info("R", "Device", description="Resistor")],
            }
        )

        results = manager.search_symbols("VCC power")
        names = [s.full_ref for s in results]
        assert "power:VCC" in names
        assert names[0] == "power:VCC"

    def test_multi_token_with_explicit_library_filter(self):
        """The user's exact reproduction:
        ``search_symbols(query="VCC power", library="power")`` was returning 0;
        must now return power:VCC.  ``library="power"`` restricts scope
        and the ``power`` token incidentally also matches the library
        nickname for every candidate."""
        manager = _make_synthetic_manager(
            {
                "power": [_info("VCC", "power"), _info("+3V3", "power")],
                "Other": [_info("VCC_random", "Other")],
            }
        )

        results = manager.search_symbols("VCC power", library_filter="power")
        names = [s.full_ref for s in results]
        assert names == ["power:VCC"], (
            "Multi-token + library_filter must keep AND semantics: "
            "only power:VCC matches both tokens within the power library."
        )

    def test_token_with_no_match_anywhere_zeros_the_candidate(self):
        """``"VCC banana"`` must NOT return random VCC symbols — the
        'banana' token has to match SOMETHING (name/value/desc/lib/etc.)
        or the candidate is disqualified."""
        manager = _make_synthetic_manager(
            {"power": [_info("VCC", "power", description="Power symbol")]}
        )

        results = manager.search_symbols("VCC banana")
        assert results == []

    def test_multi_token_via_description(self):
        """Tokens can match different fields — one token's name match plus
        another token's description match is enough."""
        manager = _make_synthetic_manager(
            {
                "Logic": [
                    _info(
                        "74HC595",
                        "Logic",
                        description="Serial-in parallel-out shift register",
                    ),
                ]
            }
        )

        results = manager.search_symbols("74HC595 shift")
        names = [s.full_ref for s in results]
        assert "Logic:74HC595" in names

    def test_single_token_query_behaves_as_before(self):
        """Single-token queries (the existing behaviour) must keep their
        old ranking — exact name match outscores description-substring."""
        manager = _make_synthetic_manager(
            {
                "Device": [_info("LED", "Device", description="Light emitting diode")],
                "Logic": [_info("74HC", "Logic", description="some led-related description")],
            }
        )

        results = manager.search_symbols("LED")
        names = [s.full_ref for s in results]
        assert names[0] == "Device:LED"

    def test_extra_whitespace_does_not_create_empty_tokens(self):
        """Leading / trailing / repeated whitespace must not produce empty
        tokens (which would match everything)."""
        manager = _make_synthetic_manager(
            {"power": [_info("VCC", "power")], "Device": [_info("R", "Device")]}
        )

        results = manager.search_symbols("   VCC   power   ")
        names = [s.full_ref for s in results]
        assert names == ["power:VCC"]


# ---------------------------------------------------------------------------
# C10: register_symbol_library must validate the path before writing an entry
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestRegisterSymbolLibraryValidation:
    """register_symbol_library accepted nonexistent paths and wrong-type files,
    writing dangling sym-lib-table entries KiCAD later fails to parse (C10).
    It must now require an EXISTING .kicad_sym FILE."""

    def _sym_table_text(self, project_dir: Path) -> str:
        tbl = project_dir / "sym-lib-table"
        return tbl.read_text(encoding="utf-8") if tbl.exists() else ""

    def _project(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "mini.kicad_pro").write_text("{}", encoding="utf-8")
        return proj

    def test_rejects_nonexistent_symbol_lib(self, tmp_path):
        from commands.symbol_creator import SymbolCreator

        proj = self._project(tmp_path)
        out = SymbolCreator().register_symbol_library(
            library_path=str(proj / "nope.kicad_sym"),
            scope="project",
            project_path=str(proj / "mini.kicad_pro"),
        )
        assert out["success"] is False
        assert out["errorCode"] == "LIBRARY_NOT_FOUND"
        assert "nope" not in self._sym_table_text(proj)

    def test_rejects_wrong_type_file(self, tmp_path):
        """A .kicad_pcb passed as a symbol library is a type error."""
        from commands.symbol_creator import SymbolCreator

        proj = self._project(tmp_path)
        board = proj / "mini.kicad_pcb"
        board.write_text("(kicad_pcb)", encoding="utf-8")

        out = SymbolCreator().register_symbol_library(
            library_path=str(board),
            scope="project",
            project_path=str(proj / "mini.kicad_pro"),
        )
        assert out["success"] is False
        assert out["errorCode"] == "INVALID_LIBRARY_TYPE"
        assert not self._sym_table_text(proj).strip() or "kicad_pcb" not in self._sym_table_text(
            proj
        )

    def test_accepts_existing_kicad_sym(self, tmp_path):
        from commands.symbol_creator import SymbolCreator

        proj = self._project(tmp_path)
        lib = proj / "custom.kicad_sym"
        lib.write_text(
            "(kicad_symbol_lib (version 20241209) (generator kicad-mcp))\n",
            encoding="utf-8",
        )

        out = SymbolCreator().register_symbol_library(
            library_path=str(lib),
            scope="project",
            project_path=str(proj / "mini.kicad_pro"),
        )
        assert out["success"] is True
        assert out.get("already_registered") is False
        assert '(name "custom")' in self._sym_table_text(proj)
