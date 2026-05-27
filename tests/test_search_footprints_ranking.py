"""Regression tests for search_footprints ranking.

User report: searching ``LED_D5.0mm`` returned only the variants
(``LED_D5.0mm-3``, ``LED_D5.0mm-3_Horizontal``, ``LED_D5.0mm-4_RGB``,
...) and never the plain ``LED_D5.0mm`` itself — it was buried after
the variants and the result was truncated at ``limit`` before reaching
it.  The new ranking guarantees exact matches and shorter-name
candidates come first.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _manager_with_footprints(library_contents):
    """Construct a LibraryManager whose libraries[] returns the given
    footprint lists when list_footprints(nickname) is called."""
    from commands.library import LibraryManager

    mgr = LibraryManager.__new__(LibraryManager)
    mgr.libraries = {nick: f"/fake/{nick}.pretty" for nick in library_contents}
    mgr.list_footprints = lambda nickname: list(  # type: ignore[method-assign]
        library_contents.get(nickname, [])
    )
    return mgr


@pytest.mark.unit
class TestSearchFootprintsRanking:
    def test_exact_match_lands_first_even_when_variants_exist(self):
        """The user's exact reproduction: ``LED_D5.0mm`` plus a dozen
        variants in the same library.  Plain ``LED_D5.0mm`` MUST be the
        first result."""
        mgr = _manager_with_footprints(
            {
                "LED_THT": [
                    "LED_D5.0mm-3",
                    "LED_D5.0mm-3_Horizontal",
                    "LED_D5.0mm-4_RGB",
                    "LED_D5.0mm-4_RGB_Wide",
                    "LED_D5.0mm",  # ← user's target
                    "LED_D5.0mm_Horizontal",
                ],
            }
        )

        results = mgr.search_footprints("LED_D5.0mm")

        assert results, "search must return at least the exact match"
        assert results[0]["footprint"] == "LED_D5.0mm"
        assert results[0]["full_name"] == "LED_THT:LED_D5.0mm"

    def test_exact_match_not_dropped_by_limit(self):
        """Old bug: if the exact match was deep in dict iteration order
        and limit=20 filled with prefix-only variants, the exact match
        was lost.  Ranking now guarantees it makes the cut."""
        # 25 variants ahead of the exact match in iteration order.
        variants = [f"LED_D5.0mm-{i}_Variant" for i in range(25)]
        mgr = _manager_with_footprints(
            {
                "LED_THT": variants + ["LED_D5.0mm"],
            }
        )

        results = mgr.search_footprints("LED_D5.0mm", limit=5)

        names = [r["footprint"] for r in results]
        assert "LED_D5.0mm" in names
        assert names[0] == "LED_D5.0mm"
        assert len(results) == 5

    def test_prefix_matches_come_before_substring_matches(self):
        mgr = _manager_with_footprints(
            {
                "LibA": [
                    "MyChip_QFP32",  # prefix match for "MyChip"
                    "MyChip",  # exact
                    "SomethingElse_with_MyChip_in_middle",  # substring only
                ],
            }
        )

        results = mgr.search_footprints("MyChip")

        names = [r["footprint"] for r in results]
        assert names[0] == "MyChip"  # exact
        # Substring-only match must come AFTER any prefix match.
        prefix_idx = names.index("MyChip_QFP32")
        substring_idx = names.index("SomethingElse_with_MyChip_in_middle")
        assert prefix_idx < substring_idx

    def test_shorter_name_wins_within_a_band(self):
        """Within the same match-quality band, the shorter footprint
        ranks higher — proxy for "less-specific variant".  Locked in so
        future ranking changes don't accidentally re-bury exact-stem
        matches behind very long suffixed variants."""
        mgr = _manager_with_footprints(
            {
                "Lib": [
                    "R_0603_1608Metric_VeryLongOverrideName",
                    "R_0603_1608Metric_Pad1",
                    "R_0603_1608Metric",  # shortest
                ],
            }
        )

        results = mgr.search_footprints("R_0603")
        names = [r["footprint"] for r in results]
        assert names == [
            "R_0603_1608Metric",
            "R_0603_1608Metric_Pad1",
            "R_0603_1608Metric_VeryLongOverrideName",
        ]

    def test_regex_metachars_in_pattern_are_escaped(self):
        """``LED_D5.0mm`` previously matched ``LED_D5X0mm`` because the
        ``.`` was treated as regex "any char".  Escape it so the search
        is literal except for ``*``."""
        mgr = _manager_with_footprints(
            {
                "Lib": [
                    "LED_D5.0mm",  # literal dot
                    "LED_D5X0mm",  # would falsely match if dot were regex
                ],
            }
        )

        results = mgr.search_footprints("LED_D5.0mm")
        names = [r["footprint"] for r in results]
        assert "LED_D5.0mm" in names
        assert "LED_D5X0mm" not in names

    def test_wildcard_star_still_works(self):
        """``*`` is still the user-facing wildcard even after the regex
        metachar escape."""
        mgr = _manager_with_footprints(
            {
                "Lib": [
                    "R_0603_1608Metric",
                    "R_0805_2012Metric",
                    "C_0603_1608Metric",
                ],
            }
        )

        results = mgr.search_footprints("R_*Metric")
        names = [r["footprint"] for r in results]
        assert set(names) == {"R_0603_1608Metric", "R_0805_2012Metric"}

    def test_library_scope_prefix_boosts_matching_library(self):
        """``LED_THT:LED_D5.0mm`` should still find ``LED_D5.0mm`` in
        ``LED_THT`` first even if other libraries have a similarly
        named footprint."""
        mgr = _manager_with_footprints(
            {
                "OtherLib": ["LED_D5.0mm"],
                "LED_THT": ["LED_D5.0mm", "LED_D5.0mm-3"],
            }
        )

        results = mgr.search_footprints("LED_THT:LED_D5.0mm")
        assert results[0]["library"] == "LED_THT"
        assert results[0]["footprint"] == "LED_D5.0mm"

    def test_empty_pattern_returns_empty_list(self):
        mgr = _manager_with_footprints({"Lib": ["something"]})

        assert mgr.search_footprints("") == []
