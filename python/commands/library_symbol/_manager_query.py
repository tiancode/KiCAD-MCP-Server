"""Library / symbol listing, info, and pin lookups.

Split out of the former monolithic commands/library_symbol.py.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from ._models import SymbolInfo

logger = logging.getLogger("kicad_interface")


class QueryMixin:
    def list_libraries(self) -> List[str]:
        """Get list of available library nicknames"""
        return list(self.libraries.keys())

    def table_signature(self) -> Dict[str, int]:
        """Return {path: mtime_ns} for every sym-lib-table consulted at load.

        Missing files map to -1 so creating a previously-absent project
        table also counts as a change.  Callers compare signatures to
        decide whether to rebuild the manager — see
        SymbolLibraryCommands._ensure_manager_for.
        """
        sig: Dict[str, int] = {}
        for path in getattr(self, "_table_paths", []):
            try:
                sig[str(path)] = path.stat().st_mtime_ns
            except OSError:
                sig[str(path)] = -1
        return sig

    def list_symbols(self, library_nickname: str) -> List[SymbolInfo]:
        """
        List all symbols in a library.

        Uses a two-tier cache:

          1. in-memory `self.symbol_cache` — populated this session, or
             restored at __init__ from the on-disk pickle.
          2. per-library mtime in `self._cache_mtimes` — validates each
             cached entry against the source .kicad_sym file's current
             mtime_ns.  A library that was edited (e.g. by the KiCAD UI
             or a PCM update) since the cache was written is silently
             re-parsed.

        Cache misses fall through to the regex-based parser and are
        written back to both tiers (in-memory now, disk at atexit).
        """
        library_path = self.libraries.get(library_nickname)
        if not library_path:
            logger.warning(f"Library not found: {library_nickname}")
            return []

        # Hot path: cache entry exists AND the source file hasn't moved.
        if library_nickname in self.symbol_cache:
            try:
                current_mtime = os.stat(library_path).st_mtime_ns
            except OSError:
                # File disappeared — fall through to the parser which will
                # also fail and log; don't serve a stale cache for a file
                # that no longer exists.
                current_mtime = None
            if (
                current_mtime is not None
                and self._cache_mtimes.get(library_nickname) == current_mtime
            ):
                return self.symbol_cache[library_nickname]
            logger.debug(
                "Symbol cache stale for %s; re-parsing (mtime moved).",
                library_nickname,
            )

        # Cache miss or stale — parse and refresh both tiers.
        symbols = self._parse_kicad_sym_file(library_path, library_nickname)
        self.symbol_cache[library_nickname] = symbols
        try:
            self._cache_mtimes[library_nickname] = os.stat(library_path).st_mtime_ns
        except OSError:
            # Drop any stale mtime so a future call re-checks instead of
            # serving from a cache entry with no validation anchor.
            self._cache_mtimes.pop(library_nickname, None)
        self._cache_dirty = True
        return symbols

    def get_symbol_info(self, library_nickname: str, symbol_name: str) -> Optional[SymbolInfo]:
        """
        Get information about a specific symbol

        Args:
            library_nickname: Library name
            symbol_name: Symbol name

        Returns:
            SymbolInfo or None if not found
        """
        symbols = self.list_symbols(library_nickname)

        for symbol in symbols:
            if symbol.name == symbol_name:
                return symbol

        return None

    def get_symbol_pins(
        self, library_nickname: str, symbol_name: str
    ) -> Optional[List[Dict[str, Any]]]:
        """Return the pin definitions of one library symbol in LOCAL coords.

        Locates the named symbol's block in ``library_nickname``'s
        ``.kicad_sym`` file, parses just that block with sexpdata, and
        runs ``PinLocator.parse_symbol_definition`` over it.  Each pin
        carries ``{number, name, x, y, angle, length, type}`` where
        ``(x, y)`` is the pin endpoint in the symbol's own coordinate
        frame (the symbol anchor passed to ``add_schematic_component``
        is added on top, and the symbol's rotation rotates these
        offsets) — this is what callers need to plan placement before
        the symbol is on the schematic.

        Returns ``None`` when the library or symbol can't be located.
        Reuses the regex-slice approach from ``_parse_kicad_sym_file``
        so pin extraction doesn't pay the full-library parse cost.
        """
        library_path = self.libraries.get(library_nickname)
        if not library_path:
            return None
        try:
            with open(library_path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            logger.warning(f"Could not read {library_path}: {e}")
            return None

        import sexpdata
        from commands.pin_locator import PinLocator

        def _slice_symbol_block(name: str) -> Optional[str]:
            """Slice ``(symbol "name" …)`` from content by walking paren depth."""
            needle = f'(symbol "{name}"'
            start = content.find(needle)
            if start == -1:
                return None
            d = 0
            j = start
            while j < len(content):
                ch = content[j]
                if ch == "(":
                    d += 1
                elif ch == ")":
                    d -= 1
                    if d == 0:
                        return content[start : j + 1]
                j += 1
            logger.warning(f"Malformed symbol block for '{name}' in {library_path}")
            return None

        def _extends_parent(sexp: Any) -> Optional[str]:
            """Return the parent name from a top-level ``(extends "parent")``."""
            for item in sexp:
                if (
                    isinstance(item, list)
                    and len(item) >= 2
                    and item[0] == sexpdata.Symbol("extends")
                ):
                    return str(item[1]).strip('"')
            return None

        def _pins_for(name: str, seen: set) -> Optional[Dict[str, Dict[str, Any]]]:
            """Parse pins for ``name``, following ``extends`` when it has none.

            KiCad stores pin/graphic geometry only on the BASE symbol; a
            derived symbol (``(extends "parent")`` — e.g.
            ``Regulator_Linear:AMS1117-3.3`` extends ``AP1117-15``) carries
            just overridden properties and NO pins of its own. Without
            following the chain such symbols report 0 pins, which breaks
            placement planning and wiring for regulators, transistors,
            opamps and most multi-variant parts.
            """
            block = _slice_symbol_block(name)
            if block is None:
                return None
            try:
                sexp = sexpdata.loads(block)
            except Exception as e:
                logger.warning(f"Could not parse symbol block for {name}: {e}")
                return None
            pins = PinLocator.parse_symbol_definition(sexp)
            if pins:
                return pins
            parent = _extends_parent(sexp)
            if parent and parent not in seen:
                seen.add(parent)
                return _pins_for(parent, seen)
            return pins  # empty dict — symbol genuinely defines no pins

        pins_dict = _pins_for(symbol_name, {symbol_name})
        if pins_dict is None:
            return None

        # Return as a list sorted by pin number for stable output.  Try
        # numeric sort first; fall back to lexicographic for alphanumeric
        # pins (e.g. ``A1`` / ``B12`` on BGAs).
        def _sort_key(p: Dict[str, Any]) -> Tuple[int, Any]:
            num = p.get("number", "")
            try:
                return (0, int(num))
            except (TypeError, ValueError):
                return (1, str(num))

        return sorted(pins_dict.values(), key=_sort_key)

    def find_symbol(self, symbol_spec: str) -> Optional[SymbolInfo]:
        """
        Find a symbol by specification

        Supports multiple formats:
        - "Library:Symbol" (e.g., "Device:R")
        - "Symbol" (searches all libraries)

        Args:
            symbol_spec: Symbol specification

        Returns:
            SymbolInfo or None if not found
        """
        if ":" in symbol_spec:
            # Format: Library:Symbol
            library_nickname, symbol_name = symbol_spec.split(":", 1)
            return self.get_symbol_info(library_nickname, symbol_name)
        else:
            # Search all libraries
            for library_nickname in self.libraries.keys():
                result = self.get_symbol_info(library_nickname, symbol_spec)
                if result:
                    return result

            return None
