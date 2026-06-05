"""Unit tests for the pure helpers in commands.library_symbol.

Safety net ahead of an internal refactor of the 1353-line library_symbol.py.
Covers the deterministic logic a split could silently change: property
extraction, library-qualifier splitting, search-plan resolution, relevance
scoring, and project-path derivation — plus API-surface guards over the
public methods of both SymbolLibraryManager and SymbolLibraryCommands.

The manager is built with ``__new__`` (no __init__) so these tests touch no
disk / sym-lib-table; only the attributes a given method needs are set.
pcbnew is stubbed globally by tests/conftest.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.library_symbol import (  # noqa: E402
    SymbolInfo,
    SymbolLibraryCommands,
    SymbolLibraryManager,
)


def _bare_manager(libraries=None) -> SymbolLibraryManager:
    """A manager with no __init__ side effects; only ``libraries`` populated."""
    mgr = SymbolLibraryManager.__new__(SymbolLibraryManager)
    mgr.libraries = dict(libraries or {})
    return mgr


def _symbol(**overrides) -> SymbolInfo:
    base = dict(name="LED", library="Device", full_ref="Device:LED")
    base.update(overrides)
    return SymbolInfo(**base)


# ---------------------------------------------------------------------------
# _extract_properties
# ---------------------------------------------------------------------------


class TestExtractProperties:
    def test_extracts_key_value_pairs(self):
        block = (
            '(symbol "R" (property "Reference" "R") '
            '(property "Value" "10k") (property "Datasheet" ""))'
        )
        props = _bare_manager()._extract_properties(block)
        assert props == {"Reference": "R", "Value": "10k", "Datasheet": ""}

    def test_no_properties_returns_empty(self):
        assert _bare_manager()._extract_properties('(symbol "R")') == {}


# ---------------------------------------------------------------------------
# split_library_qualifier  (uses self.libraries)
# ---------------------------------------------------------------------------


class TestSplitLibraryQualifier:
    @pytest.fixture
    def mgr(self):
        return _bare_manager({"Device": "/d", "JLCPCB": "/j"})

    def test_known_prefix_is_split(self, mgr):
        assert mgr.split_library_qualifier("Device:LED") == ("LED", "Device")

    def test_case_insensitive_substring_prefix(self, mgr):
        assert mgr.split_library_qualifier("dev:R") == ("R", "dev")

    def test_unknown_prefix_kept_literal(self, mgr):
        assert mgr.split_library_qualifier("LM358:DR") == ("LM358:DR", None)

    def test_no_colon(self, mgr):
        assert mgr.split_library_qualifier("LED") == ("LED", None)

    def test_empty_side_kept_literal(self, mgr):
        assert mgr.split_library_qualifier("Device:") == ("Device:", None)


# ---------------------------------------------------------------------------
# plan_search  (uses self.libraries)
# ---------------------------------------------------------------------------


class TestPlanSearch:
    @pytest.fixture
    def mgr(self):
        return _bare_manager({"Device": "/d", "Device_2": "/d2", "JLCPCB": "/j"})

    def test_inline_prefix_scopes_to_exact_library(self, mgr):
        plan = mgr.plan_search("Device:LED")
        assert plan.name_query == "LED"
        assert plan.inline_prefix == "Device"
        assert plan.effective_library == "Device"
        assert plan.libraries_searched == ["Device"]  # exact, not Device_2

    def test_explicit_filter_overrides_inline_prefix(self, mgr):
        plan = mgr.plan_search("Device:LED", library_filter="JLCPCB")
        assert plan.name_query == "LED"  # prefix still stripped from name
        assert plan.inline_prefix == "Device"
        assert plan.effective_library == "JLCPCB"
        assert plan.libraries_searched == ["JLCPCB"]

    def test_no_filter_searches_all(self, mgr):
        plan = mgr.plan_search("LED")
        assert plan.effective_library is None
        assert set(plan.libraries_searched) == {"Device", "Device_2", "JLCPCB"}

    def test_filter_matching_nothing_is_flagged(self, mgr):
        plan = mgr.plan_search("LED", library_filter="Nonexistent")
        assert plan.libraries_searched == []
        assert plan.library_filter_matched_nothing is True


# ---------------------------------------------------------------------------
# _score_token / _score_match  (pure scoring)
# ---------------------------------------------------------------------------


class TestScoring:
    @pytest.fixture
    def mgr(self):
        return _bare_manager()

    def test_exact_lcsc_dominates(self, mgr):
        sym = _symbol(name="X", lcsc_id="C2286")
        assert mgr._score_token("c2286", sym) == 1000

    def test_exact_name_also_counts_partial(self, mgr):
        # exact (+500) and partial (+100) both fire for an exact name hit.
        sym = _symbol(name="LED", description="diode")
        assert mgr._score_token("led", sym) == 600

    def test_description_only_match(self, mgr):
        sym = _symbol(name="X", value="", description="a red led indicator")
        assert mgr._score_token("led", sym) == 50

    def test_no_match_is_zero(self, mgr):
        assert mgr._score_token("banana", _symbol(name="LED", description="diode")) == 0

    def test_score_match_sums_tokens(self, mgr):
        sym = _symbol(name="LED", description="small diode")
        assert mgr._score_match(["led", "diode"], sym) == 650  # 600 + 50

    def test_score_match_strict_and(self, mgr):
        sym = _symbol(name="LED", description="small diode")
        assert mgr._score_match(["led", "banana"], sym) == 0


# ---------------------------------------------------------------------------
# SymbolLibraryCommands._derive_project_path  (staticmethod)
# ---------------------------------------------------------------------------


class TestDeriveProjectPath:
    def test_none_when_no_keys(self):
        assert SymbolLibraryCommands._derive_project_path({}) is None

    def test_kicad_pro_file_uses_parent_dir(self):
        got = SymbolLibraryCommands._derive_project_path(
            {"projectPath": "/tmp/proj/board.kicad_pro"}
        )
        assert got == Path("/tmp/proj")

    def test_nonexistent_dir_returned_as_is(self):
        got = SymbolLibraryCommands._derive_project_path({"projectPath": "/tmp/some/dir"})
        assert got == Path("/tmp/some/dir")

    def test_related_path_walks_to_project_marker(self, tmp_path):
        (tmp_path / "sym-lib-table").write_text("(sym_lib_table)")
        sub = tmp_path / "sub"
        sub.mkdir()
        got = SymbolLibraryCommands._derive_project_path(
            {"schematicPath": str(sub / "x.kicad_sch")}
        )
        assert got == tmp_path


# ---------------------------------------------------------------------------
# Public API surfaces — guards for the upcoming internal refactor.
# Update deliberately when adding/removing a public method.
# ---------------------------------------------------------------------------

EXPECTED_MANAGER_METHODS = {
    "execute_search_plan",
    "find_symbol",
    "get_symbol_info",
    "get_symbol_pins",
    "list_libraries",
    "list_symbols",
    "plan_search",
    "search_symbols",
    "split_library_qualifier",
    "table_signature",
}

EXPECTED_COMMANDS_METHODS = {
    "get_symbol_info",
    "list_library_symbols",
    "list_symbol_libraries",
    "refresh_symbol_libraries",
    "search_symbols",
    "use_project",
}


def _public_methods(cls):
    return {name for name in dir(cls) if not name.startswith("_") and callable(getattr(cls, name))}


class TestPublicApiSurface:
    def test_manager_methods_unchanged(self):
        assert _public_methods(SymbolLibraryManager) == EXPECTED_MANAGER_METHODS

    def test_commands_methods_unchanged(self):
        assert _public_methods(SymbolLibraryCommands) == EXPECTED_COMMANDS_METHODS
