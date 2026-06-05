"""Public SymbolLibraryManager class (composed from mixins) + factory.

Split out of the former monolithic commands/library_symbol.py; the public
API and behaviour are unchanged.
"""

import atexit
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from commands.library import _load_cached_manager

from ._manager_loading import LoadingMixin
from ._manager_parsing import ParsingMixin
from ._manager_query import QueryMixin
from ._manager_search import SearchMixin
from ._models import SymbolInfo  # noqa: F401  (re-exported via package __init__)

logger = logging.getLogger("kicad_interface")


class SymbolLibraryManager(LoadingMixin, ParsingMixin, SearchMixin, QueryMixin):
    """
    Manages KiCAD symbol libraries

    Parses sym-lib-table files (both global and project-specific),
    indexes available symbols, and provides search functionality.
    """

    def __init__(self, project_path: Optional[Path] = None):
        """
        Initialize symbol library manager

        Args:
            project_path: Optional path to project directory for project-specific libraries
        """
        self.project_path = project_path
        self.libraries: Dict[str, str] = {}  # nickname -> path mapping
        self.symbol_cache: Dict[str, List[SymbolInfo]] = {}  # library -> [SymbolInfo]
        # Source-file mtime_ns at the time the matching symbol_cache entry was
        # parsed.  list_symbols() compares against the current mtime to decide
        # whether the cache is still fresh.  Persisted alongside symbol_cache
        # so a session can reuse parses from previous sessions.
        self._cache_mtimes: Dict[str, int] = {}
        self._cache_dirty = False  # whether to flush to disk at shutdown
        self._load_libraries()
        # Restore previously parsed libraries from disk, if any.  This is what
        # turns a cold start into a warm one: instead of re-parsing 200+
        # .kicad_sym files (30-120 s) we read a single pickle (< 200 ms).
        self._load_disk_cache()

        # Eager full-warm is now opt-in: KICAD_MCP_EAGER_SYMBOL_CACHE=1.  The
        # default lazy path costs nothing at startup and parses per-library on
        # first list_symbols(nickname) call, which is bounded by what the
        # user actually searches.  Combined with the disk cache above, even
        # `search_symbols` over many libraries is fast after the first run.
        if os.environ.get("KICAD_MCP_EAGER_SYMBOL_CACHE") == "1":
            self._warm_cache()

        # Persist anything we parsed at shutdown so the next run starts hot.
        atexit.register(self._save_disk_cache)


# Process-wide cache of SymbolLibraryManager instances, keyed by project scope.
# Shares the caching/invalidation core with the footprint side (see
# commands.library._load_cached_manager). Project switches / explicit refreshes
# go through SymbolLibraryCommands, which rebuilds its own manager — this cache
# only backs the default construction.
_SYMBOL_MANAGER_CACHE: Dict[Optional[str], Tuple[SymbolLibraryManager, Dict[str, int]]] = {}


def get_symbol_library_manager(project_path: Optional[Path] = None) -> SymbolLibraryManager:
    """Return a cached :class:`SymbolLibraryManager` for the given project scope."""
    return _load_cached_manager(_SYMBOL_MANAGER_CACHE, SymbolLibraryManager, project_path)
