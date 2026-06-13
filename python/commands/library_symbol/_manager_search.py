"""Symbol search: query planning, execution, and scoring.

Split out of the former monolithic commands/library_symbol.py.
"""

import heapq
import logging
from typing import List, Optional, Tuple

from ._models import SymbolInfo, _SearchPlan

logger = logging.getLogger("kicad_interface")


class SearchMixin:
    def split_library_qualifier(self, query: str) -> Tuple[str, Optional[str]]:
        """Split a ``"Library:Name"`` query into ``(name_part, library_prefix)``.

        Returns the library prefix only when it actually matches at least
        one library nickname (case-insensitive substring) — otherwise the
        colon is treated as part of the literal query so unrelated inputs
        like ``"LM358:DR"`` keep their old behavior.

        Returns ``(query, None)`` when there's no colon, when either side
        is empty, or when the left side doesn't match any known library.
        """
        if ":" not in query:
            return query, None
        left, _, right = query.partition(":")
        left = left.strip()
        right = right.strip()
        if not left or not right:
            return query, None
        left_lower = left.lower()
        if not any(left_lower in nickname.lower() for nickname in self.libraries):
            return query, None
        return right, left

    def plan_search(self, query: str, library_filter: Optional[str] = None) -> "_SearchPlan":
        """Resolve a raw query into a complete search plan.

        Single source of truth for: the name part that gets scored, the
        library scope to search, the inline-colon prefix that was parsed
        (so the caller can surface it back to the agent), and whether
        the library filter excluded everything (so the caller can warn
        instead of silently returning 0).

        ``library_filter`` is treated as the *scope* and overrides any
        inline ``Library:`` prefix, but the inline prefix is *still
        stripped* from the name part — otherwise ``query='Device:LED'
        library='JLCPCB'`` would feed the literal ``'Device:LED'`` to the
        scorer, which never matches because no field contains ``':'``.
        The override is reported via ``inline_prefix`` so the response
        layer can tell the agent what happened.
        """
        name_query, inline_prefix = self.split_library_qualifier(query)
        effective_library = library_filter or inline_prefix

        all_libraries = list(self.libraries.keys())
        if effective_library:
            filter_lower = effective_library.lower()
            # Prefer an exact nickname match when one exists — otherwise
            # "Device" would also pull in "Device_2" / "Device_Extras",
            # silently widening the result set.
            exact = [lib for lib in all_libraries if lib.lower() == filter_lower]
            libraries_searched = (
                exact if exact else [lib for lib in all_libraries if filter_lower in lib.lower()]
            )
        else:
            libraries_searched = all_libraries

        return _SearchPlan(
            name_query=name_query,
            effective_library=effective_library,
            inline_prefix=inline_prefix,
            libraries_searched=libraries_searched,
            library_filter_matched_nothing=(
                effective_library is not None and not libraries_searched
            ),
        )

    def execute_search_plan(self, plan: "_SearchPlan", limit: int) -> List[SymbolInfo]:
        """Score symbols under ``plan`` and return the top ``limit`` by score.

        Uses ``heapq.nlargest`` so broad queries (e.g. ``"R"`` or ``"a"``)
        run in O(N log K) time and O(K) memory rather than building a
        full per-match list and sorting it — that's the difference
        between ~50 ms and ~500 ms on a stock + JLCPCB install with
        ~200k indexed symbols.
        """
        query_lower = plan.name_query.lower()
        if not query_lower:
            return []
        # Tokenize on whitespace.  Multi-token queries used to compare the
        # full string against every field as one substring, so the natural
        # ``"VCC power"`` returned 0 hits even when ``power:VCC`` was
        # available.  We now score each token independently with strict
        # AND semantics: any token that finds no match anywhere on the
        # symbol disqualifies the candidate.
        tokens = query_lower.split()
        if not tokens:
            return []

        def candidates():
            for library_nickname in plan.libraries_searched:
                for symbol in self.list_symbols(library_nickname):
                    score = self._score_match(tokens, symbol)
                    if score > 0:
                        yield (score, symbol)

        top = heapq.nlargest(limit, candidates(), key=lambda pair: pair[0])
        return [symbol for _, symbol in top]

    def search_symbols(
        self, query: str, limit: int = 20, library_filter: Optional[str] = None
    ) -> List[SymbolInfo]:
        """Search for symbols matching a query.

        Supports two query forms:

          - ``"Name"`` — fuzzy match `Name` against symbol name / value /
            description / LCSC ID / MPN in every library (subject to
            ``library_filter``).
          - ``"Library:Name"`` — same fuzzy match against `Name`,
            restricted to libraries whose nickname contains `Library`
            (case-insensitive).  Even when ``library_filter`` is *also*
            supplied, the colon prefix is stripped from the name part
            (the explicit filter wins as the library scope), so
            ``query='Device:LED' library='JLCPCB'`` searches JLCPCB for
            ``'LED'`` rather than the un-matchable literal
            ``'Device:LED'``.

        Scoring keeps exact-name matches at score 500, far above the
        score-50 description-substring band, so ``query="LED"`` finds
        ``Device:LED`` rather than 60 ``74LSxxx`` parts whose description
        happens to contain "led" as a substring of "controlled" /
        "settled" / "compiled".  The previous early-break that capped
        results at ``limit * 3`` is gone — broad queries are now bounded
        by ``heapq.nlargest`` instead of by giving up after the first
        library to fill the budget with fuzzy hits.
        """
        return self.execute_search_plan(self.plan_search(query, library_filter), limit)

    def _score_match(self, tokens: List[str], symbol: SymbolInfo) -> int:
        """Sum per-token scores, with strict AND semantics across tokens.

        Multi-token queries used to substring-match the full string (e.g.
        ``"VCC power"`` against each field), which never matched a
        symbol named ``VCC`` in library ``power``.  Each whitespace token
        is now scored independently and any token that finds NO match
        zeroes the candidate — so ``"VCC banana"`` doesn't accidentally
        return every VCC variant.
        """
        total = 0
        for tok in tokens:
            sub = self._score_token(tok, symbol)
            if sub == 0:
                return 0
            total += sub
        return total

    def _score_token(self, query: str, symbol: SymbolInfo) -> int:
        """
        Score how well a symbol matches a single query token

        Returns:
            Score (0 = no match, higher = better match)
        """
        score = 0

        # Exact LCSC ID match - highest priority
        if symbol.lcsc_id and symbol.lcsc_id.lower() == query:
            score += 1000

        # Exact name match
        if symbol.name.lower() == query:
            score += 500

        # Exact value match
        if symbol.value.lower() == query:
            score += 400

        # Partial name match
        if query in symbol.name.lower():
            score += 100

        # Partial value match
        if query in symbol.value.lower():
            score += 80

        # Description match
        if query in symbol.description.lower():
            score += 50

        # MPN match
        if symbol.mpn and query in symbol.mpn.lower():
            score += 70

        # Manufacturer match
        if symbol.manufacturer and query in symbol.manufacturer.lower():
            score += 30

        # Category match
        if symbol.category and query in symbol.category.lower():
            score += 20

        # Library-nickname match.  Lets multi-token queries like
        # ``"VCC power"`` succeed: the ``power`` token matches the
        # library while ``VCC`` matches the symbol name.  Low weight so
        # it ranks below real name / value / desc hits.
        if symbol.library and query in symbol.library.lower():
            score += 25

        return score
