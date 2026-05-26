"""
Library management for KiCAD symbols

Handles parsing sym-lib-table files, discovering symbols,
and providing search functionality for component selection.
"""

import atexit
import logging
import os
import pickle
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("kicad_interface")

# Bump when SymbolInfo fields or the cache structure change so older pickles
# are rejected automatically instead of producing stale/misshapen data.
_DISK_CACHE_VERSION = 1
_DISK_CACHE_PATH = Path.home() / ".kicad-mcp" / "cache" / "symbol_libraries.pickle"


@dataclass
class SymbolInfo:
    """Information about a symbol in a library"""

    name: str  # Symbol name (without library prefix)
    library: str  # Library nickname
    full_ref: str  # "Library:SymbolName"
    value: str = ""  # Value property
    description: str = ""  # Description property
    footprint: str = ""  # Footprint reference if present
    lcsc_id: str = ""  # LCSC property if present
    manufacturer: str = ""  # Manufacturer property
    mpn: str = ""  # Part/MPN property
    category: str = ""  # Category property
    datasheet: str = ""  # Datasheet URL
    stock: str = ""  # Stock (from JLCPCB libs)
    price: str = ""  # Price (from JLCPCB libs)
    lib_class: str = ""  # Basic/Preferred/Extended
    sim_pins: str = ""  # Sim.Pins pin mapping (e.g. "1=in+ 2=in- 3=vcc 4=vee 5=out")


class SymbolLibraryManager:
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

    def _warm_cache(self) -> None:
        """Pre-parse all symbol libraries so the first search is instant.

        Opt-in via KICAD_MCP_EAGER_SYMBOL_CACHE=1.  Without it, libraries are
        parsed lazily as ``list_symbols(nickname)`` is called.  Even with the
        flag set the disk cache short-circuits most parses, so the price
        ranges from "near-zero (cache hit)" to "30-120 s (cold disk, no
        cache)".  See the module docstring for the cache file location.
        """
        for nickname in list(self.libraries.keys()):
            try:
                self.list_symbols(nickname)
            except (OSError, ValueError, KeyError) as e:
                # best-effort prewarm — unreadable/missing/bad-syntax library
                # files are skipped silently.  Was `except Exception` which
                # also swallowed programmer bugs; tightened to file-IO and
                # parse failures only.
                logger.debug("Skipping unparseable library %s: %s", nickname, e)

    def _load_disk_cache(self) -> None:
        """Restore symbol_cache + _cache_mtimes from the on-disk pickle.

        Silently skipped if the cache file is missing, unreadable, or
        produced by an older version of the code.  list_symbols() is
        responsible for re-checking each entry's mtime against the current
        .kicad_sym file before serving from cache, so even if the disk
        cache was written months ago and a few libraries changed, we only
        re-parse what actually moved.
        """
        if not _DISK_CACHE_PATH.exists():
            logger.debug("No symbol disk cache at %s; starting cold.", _DISK_CACHE_PATH)
            return
        try:
            with _DISK_CACHE_PATH.open("rb") as fh:
                data = pickle.load(fh)
        except (OSError, pickle.UnpicklingError, EOFError, AttributeError) as e:
            logger.warning("Could not read symbol disk cache (%s); will rebuild.", e)
            return

        if not isinstance(data, dict) or data.get("version") != _DISK_CACHE_VERSION:
            logger.info(
                "Symbol disk cache version mismatch (got %r, want %d); rebuilding.",
                data.get("version") if isinstance(data, dict) else None,
                _DISK_CACHE_VERSION,
            )
            return

        symbol_cache = data.get("symbol_cache") or {}
        mtimes = data.get("mtimes") or {}
        if not isinstance(symbol_cache, dict) or not isinstance(mtimes, dict):
            logger.warning("Symbol disk cache has unexpected shape; rebuilding.")
            return

        self.symbol_cache = symbol_cache
        self._cache_mtimes = mtimes
        logger.info(
            "Restored %d libraries from symbol disk cache (%s)",
            len(self.symbol_cache),
            _DISK_CACHE_PATH,
        )

    def _save_disk_cache(self) -> None:
        """Persist symbol_cache + mtimes to disk for the next session.

        Best-effort.  Called via atexit.  No-op when the cache hasn't been
        touched (saves an unnecessary pickle write on PCB-only sessions
        that never invoke search_symbols).
        """
        if not self._cache_dirty:
            return
        try:
            _DISK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": _DISK_CACHE_VERSION,
                "symbol_cache": self.symbol_cache,
                "mtimes": self._cache_mtimes,
            }
            tmp = _DISK_CACHE_PATH.with_suffix(_DISK_CACHE_PATH.suffix + ".tmp")
            with tmp.open("wb") as fh:
                pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.replace(_DISK_CACHE_PATH)
            logger.info(
                "Saved symbol disk cache: %d libraries to %s",
                len(self.symbol_cache),
                _DISK_CACHE_PATH,
            )
        except (OSError, pickle.PicklingError) as e:
            logger.warning("Could not persist symbol disk cache: %s", e)

    def _load_libraries(self) -> None:
        """Load libraries from sym-lib-table files"""
        # Load global libraries
        global_table = self._get_global_sym_lib_table()
        if global_table and global_table.exists():
            logger.info(f"Loading global sym-lib-table from: {global_table}")
            self._parse_sym_lib_table(global_table)
        else:
            logger.warning(f"Global sym-lib-table not found at: {global_table}")

        # Load project-specific libraries if project path provided
        if self.project_path:
            project_table = self.project_path / "sym-lib-table"
            if project_table.exists():
                logger.info(f"Loading project sym-lib-table from: {project_table}")
                self._parse_sym_lib_table(project_table)

        logger.info(f"Loaded {len(self.libraries)} symbol libraries")

    def _get_global_sym_lib_table(self) -> Optional[Path]:
        """Get path to global sym-lib-table file"""
        # Try different possible locations (same as fp-lib-table but for symbols)
        kicad_config_paths = [
            Path.home() / ".config" / "kicad" / "10.0" / "sym-lib-table",
            Path.home() / ".config" / "kicad" / "9.0" / "sym-lib-table",
            Path.home() / ".config" / "kicad" / "8.0" / "sym-lib-table",
            Path.home() / ".config" / "kicad" / "sym-lib-table",
            # Windows paths
            Path.home() / "AppData" / "Roaming" / "kicad" / "10.0" / "sym-lib-table",
            Path.home() / "AppData" / "Roaming" / "kicad" / "9.0" / "sym-lib-table",
            Path.home() / "AppData" / "Roaming" / "kicad" / "8.0" / "sym-lib-table",
            # macOS paths
            Path.home() / "Library" / "Preferences" / "kicad" / "10.0" / "sym-lib-table",
            Path.home() / "Library" / "Preferences" / "kicad" / "9.0" / "sym-lib-table",
            Path.home() / "Library" / "Preferences" / "kicad" / "8.0" / "sym-lib-table",
        ]

        for path in kicad_config_paths:
            if path.exists():
                return path

        return None

    def _parse_sym_lib_table(self, table_path: Path) -> None:
        """
        Parse sym-lib-table file

        Format is S-expression (Lisp-like):
        (sym_lib_table
          (lib (name "Library_Name")(type KiCad)(uri "${KICAD9_SYMBOL_DIR}/Library.kicad_sym")(options "")(descr "Description"))
        )
        """
        try:
            with open(table_path, "r", encoding="utf-8") as f:
                content = f.read()

            # Simple regex-based parser for lib entries
            # Pattern: (lib (name "NAME")(type TYPE)(uri "URI")...)
            lib_pattern = r'\(lib\s+\(name\s+"?([^")\s]+)"?\)\s*\(type\s+"?([^")\s]+)"?\)\s*\(uri\s+"?([^")\s]+)"?'

            for match in re.finditer(lib_pattern, content, re.IGNORECASE):
                nickname = match.group(1)
                lib_type = match.group(2)
                uri = match.group(3)

                if lib_type.lower() == "table":
                    table_uri = uri
                    if os.path.isabs(table_uri) and os.path.isfile(table_uri):
                        logger.info(f"  Following Table reference: {nickname} -> {table_uri}")
                        self._parse_sym_lib_table(Path(table_uri))
                    else:
                        logger.warning(f"  Could not resolve Table URI: {table_uri}")
                    continue

                # Resolve environment variables in URI
                resolved_uri = self._resolve_uri(uri)

                if resolved_uri:
                    self.libraries[nickname] = resolved_uri
                    logger.debug(f"  Found library: {nickname} -> {resolved_uri}")
                else:
                    logger.debug(f"  Could not resolve URI for library {nickname}: {uri}")

        except (OSError, ValueError) as e:
            logger.exception(f"Error parsing sym-lib-table at {table_path}: {e}")

    def _resolve_uri(self, uri: str) -> Optional[str]:
        """
        Resolve environment variables and paths in library URI

        Handles:
        - ${KICAD9_SYMBOL_DIR} -> /Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols
        - ${KICAD9_3RD_PARTY} -> ~/Documents/KiCad/9.0/3rdparty
        - ${KIPRJMOD} -> project directory
        - Relative paths
        - Absolute paths
        """
        resolved = uri

        # Common KiCAD environment variables
        env_vars = {
            "KICAD10_SYMBOL_DIR": self._find_kicad_symbol_dir(),
            "KICAD9_SYMBOL_DIR": self._find_kicad_symbol_dir(),
            "KICAD8_SYMBOL_DIR": self._find_kicad_symbol_dir(),
            "KICAD_SYMBOL_DIR": self._find_kicad_symbol_dir(),
            "KICAD10_3RD_PARTY": self._find_3rd_party_dir(),
            "KICAD9_3RD_PARTY": self._find_3rd_party_dir(),
            "KICAD8_3RD_PARTY": self._find_3rd_party_dir(),
            "KICAD_3RD_PARTY": self._find_3rd_party_dir(),
            "KISYSSYM": self._find_kicad_symbol_dir(),
        }

        # Project directory
        if self.project_path:
            env_vars["KIPRJMOD"] = str(self.project_path)

        # Replace environment variables
        for var, value in env_vars.items():
            if value:
                resolved = resolved.replace(f"${{{var}}}", value)
                resolved = resolved.replace(f"${var}", value)

        # Expand ~ to home directory
        resolved = os.path.expanduser(resolved)

        # Convert to absolute path
        path = Path(resolved)

        # Check if path exists
        if path.exists():
            return str(path)
        else:
            logger.debug(f"    Path does not exist: {path}")
            return None

    def _find_kicad_symbol_dir(self) -> Optional[str]:
        """Find KiCAD symbol directory"""
        possible_paths = [
            "/usr/share/kicad/symbols",
            "/usr/local/share/kicad/symbols",
            "C:/Program Files/KiCad/9.0/share/kicad/symbols",
            "C:/Program Files/KiCad/8.0/share/kicad/symbols",
            "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols",
        ]

        # Check environment variable
        if "KICAD9_SYMBOL_DIR" in os.environ:
            possible_paths.insert(0, os.environ["KICAD9_SYMBOL_DIR"])
        if "KICAD8_SYMBOL_DIR" in os.environ:
            possible_paths.insert(0, os.environ["KICAD8_SYMBOL_DIR"])

        for path in possible_paths:
            if os.path.isdir(path):
                return path

        return None

    def _find_3rd_party_dir(self) -> Optional[str]:
        """Find KiCAD 3rd party library directory (PCM installed libs)"""
        possible_paths = [
            str(Path.home() / "Documents" / "KiCad" / "10.0" / "3rdparty"),
            str(Path.home() / "Documents" / "KiCad" / "9.0" / "3rdparty"),
            str(Path.home() / "Documents" / "KiCad" / "8.0" / "3rdparty"),
        ]

        # Check environment variable
        if "KICAD10_3RD_PARTY" in os.environ:
            possible_paths.insert(0, os.environ["KICAD10_3RD_PARTY"])
        if "KICAD9_3RD_PARTY" in os.environ:
            possible_paths.insert(0, os.environ["KICAD9_3RD_PARTY"])
        if "KICAD8_3RD_PARTY" in os.environ:
            possible_paths.insert(0, os.environ["KICAD8_3RD_PARTY"])
        if "KICAD_3RD_PARTY" in os.environ:
            possible_paths.insert(0, os.environ["KICAD_3RD_PARTY"])

        for path in possible_paths:
            if os.path.isdir(path):
                return path

        return None

    def _parse_kicad_sym_file(self, library_path: str, library_name: str) -> List[SymbolInfo]:
        """
        Parse a .kicad_sym file to extract symbol metadata

        Args:
            library_path: Path to the .kicad_sym file
            library_name: Nickname of the library

        Returns:
            List of SymbolInfo objects
        """
        symbols = []

        try:
            with open(library_path, "r", encoding="utf-8") as f:
                content = f.read()

            # Find all top-level symbol definitions
            # Pattern: (symbol "SYMBOL_NAME" ... ) at the top level
            # We need to find symbols that are direct children of kicad_symbol_lib
            # and not sub-symbols (which have names like "PARENT_0_1")

            # Simple approach: find all (symbol "NAME" and filter out sub-symbols
            symbol_pattern = r'\(symbol\s+"([^"]+)"'

            for match in re.finditer(symbol_pattern, content):
                symbol_name = match.group(1)

                # Skip sub-symbols (they contain _0_, _1_, etc. suffixes)
                if re.search(r"_\d+_\d+$", symbol_name):
                    continue

                # Find the start position of this symbol
                start_pos = match.start()

                # Walk forward tracking parenthesis depth to find the true end of the block
                depth = 0
                i = start_pos
                end_pos = start_pos
                while i < len(content):
                    ch = content[i]
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            end_pos = i + 1
                            break
                    i += 1

                if end_pos == start_pos:
                    logger.warning(
                        f"Malformed symbol block for '{symbol_name}' in {library_path}; skipping"
                    )
                    continue

                symbol_block = content[start_pos:end_pos]

                # Extract properties
                properties = self._extract_properties(symbol_block)

                symbol_info = SymbolInfo(
                    name=symbol_name,
                    library=library_name,
                    full_ref=f"{library_name}:{symbol_name}",
                    value=properties.get("Value", ""),
                    description=properties.get("Description", ""),
                    footprint=properties.get("Footprint", ""),
                    lcsc_id=properties.get("LCSC", ""),
                    manufacturer=properties.get("Manufacturer", ""),
                    mpn=properties.get("Part", properties.get("MPN", "")),
                    category=properties.get("Category", ""),
                    datasheet=properties.get("Datasheet", ""),
                    stock=properties.get("Stock", ""),
                    price=properties.get("Price", ""),
                    lib_class=properties.get("Class", ""),
                    sim_pins=properties.get("Sim.Pins", ""),
                )

                symbols.append(symbol_info)

            logger.debug(f"Parsed {len(symbols)} symbols from {library_name}")

        except (OSError, ValueError) as e:
            logger.exception(f"Error parsing symbol library {library_path}: {e}")

        return symbols

    def _extract_properties(self, symbol_block: str) -> Dict[str, str]:
        """Extract properties from a symbol block"""
        properties = {}

        # Pattern for properties: (property "KEY" "VALUE" ...)
        prop_pattern = r'\(property\s+"([^"]+)"\s+"([^"]*)"'

        for match in re.finditer(prop_pattern, symbol_block):
            key = match.group(1)
            value = match.group(2)
            properties[key] = value

        return properties

    def list_libraries(self) -> List[str]:
        """Get list of available library nicknames"""
        return list(self.libraries.keys())

    def get_library_path(self, nickname: str) -> Optional[str]:
        """Get filesystem path for a library nickname"""
        return self.libraries.get(nickname)

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

    def search_symbols(
        self, query: str, limit: int = 20, library_filter: Optional[str] = None
    ) -> List[SymbolInfo]:
        """
        Search for symbols matching a query

        Args:
            query: Search query (matches name, LCSC ID, description, category, manufacturer)
            limit: Maximum number of results to return
            library_filter: Optional library name pattern to filter by

        Returns:
            List of SymbolInfo objects sorted by relevance
        """
        results = []
        query_lower = query.lower()

        # Determine which libraries to search
        libraries_to_search: list[str] = list(self.libraries.keys())
        if library_filter:
            filter_lower = library_filter.lower()
            libraries_to_search = [
                lib for lib in libraries_to_search if filter_lower in lib.lower()
            ]

        for library_nickname in libraries_to_search:
            symbols = self.list_symbols(library_nickname)

            for symbol in symbols:
                score = self._score_match(query_lower, symbol)
                if score > 0:
                    results.append((score, symbol))

                    if len(results) >= limit * 3:  # Get extra for sorting
                        break

            if len(results) >= limit * 3:
                break

        # Sort by score (descending) and return top results
        results.sort(key=lambda x: x[0], reverse=True)
        return [symbol for _, symbol in results[:limit]]

    def _score_match(self, query: str, symbol: SymbolInfo) -> int:
        """
        Score how well a symbol matches a query

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

        return score

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


class SymbolLibraryCommands:
    """Command handlers for symbol library operations"""

    def __init__(self, library_manager: Optional[SymbolLibraryManager] = None):
        """Initialize with optional library manager"""
        self.library_manager = library_manager or SymbolLibraryManager()

    @staticmethod
    def _derive_project_path(params: Dict) -> Optional[Path]:
        """Derive a project directory from caller-supplied params.

        Accepts an explicit project directory or .kicad_pro file via projectPath,
        or any related file path (schematicPath/boardPath) — in which case the
        nearest ancestor containing sym-lib-table or a .kicad_pro is used.
        """
        for key in ("projectPath", "project_path"):
            value = params.get(key)
            if value:
                p = Path(value).expanduser()
                if p.suffix == ".kicad_pro" or p.is_file():
                    p = p.parent
                return p

        for key in ("schematicPath", "boardPath"):
            value = params.get(key)
            if value:
                start = Path(value).expanduser().parent
                for ancestor in [start, *start.parents]:
                    if (ancestor / "sym-lib-table").exists() or list(ancestor.glob("*.kicad_pro")):
                        return ancestor
                return start

        return None

    def use_project(self, project_path: Optional[Path]) -> None:
        """Switch the underlying manager to load project-scope libraries.

        Callers (e.g. open_project / create_project) use this to make
        `<project>/sym-lib-table` visible to subsequent search/list/info calls
        without requiring every caller to pass projectPath.
        """
        if project_path is None:
            return
        if self.library_manager.project_path == project_path:
            return
        logger.info(f"Rebuilding SymbolLibraryManager for project: {project_path}")
        self.library_manager = SymbolLibraryManager(project_path=project_path)

    def _ensure_manager_for(self, params: Dict) -> None:
        """Rebuild the library manager if the caller's project differs."""
        self.use_project(self._derive_project_path(params))

    def list_symbol_libraries(self, params: Dict) -> Dict:
        """List all available symbol libraries"""
        try:
            self._ensure_manager_for(params)
            libraries = self.library_manager.list_libraries()
            return {"success": True, "libraries": libraries, "count": len(libraries)}
        except (OSError, ValueError) as e:
            logger.exception(f"Error listing symbol libraries: {e}")
            return {
                "success": False,
                "message": "Failed to list symbol libraries",
                "errorDetails": str(e),
            }

    def search_symbols(self, params: Dict) -> Dict:
        """Search for symbols by query"""
        try:
            query = params.get("query", "")
            if not query:
                return {"success": False, "message": "Missing query parameter"}

            self._ensure_manager_for(params)

            limit = params.get("limit", 20)
            library_filter = params.get("library")

            results = self.library_manager.search_symbols(query, limit, library_filter)

            return {
                "success": True,
                "symbols": [asdict(s) for s in results],
                "count": len(results),
                "query": query,
            }
        except (OSError, ValueError) as e:
            logger.exception(f"Error searching symbols: {e}")
            return {"success": False, "message": "Failed to search symbols", "errorDetails": str(e)}

    def list_library_symbols(self, params: Dict) -> Dict:
        """List all symbols in a specific library"""
        try:
            library = params.get("library")
            if not library:
                return {"success": False, "message": "Missing library parameter"}

            self._ensure_manager_for(params)

            # Check if library exists in sym-lib-table
            if library not in self.library_manager.libraries:
                available_libs = list(self.library_manager.libraries.keys())
                return {
                    "success": False,
                    "message": f"Library '{library}' not found in sym-lib-table",
                    "errorDetails": f"Library '{library}' is not registered in your KiCad symbol library table. "
                    f"Found {len(available_libs)} libraries. "
                    f"Please add this library to your sym-lib-table file, or use one of the available libraries.",
                    "available_libraries_count": len(available_libs),
                    "suggestion": "Use 'list_symbol_libraries' to see all available libraries",
                }

            symbols = self.library_manager.list_symbols(library)

            return {
                "success": True,
                "library": library,
                "symbols": [asdict(s) for s in symbols],
                "count": len(symbols),
            }
        except (OSError, ValueError) as e:
            logger.exception(f"Error listing library symbols: {e}")
            return {
                "success": False,
                "message": "Failed to list library symbols",
                "errorDetails": str(e),
            }

    def get_symbol_info(self, params: Dict) -> Dict:
        """Get information about a specific symbol"""
        try:
            symbol_spec = params.get("symbol")
            if not symbol_spec:
                return {"success": False, "message": "Missing symbol parameter"}

            self._ensure_manager_for(params)

            result = self.library_manager.find_symbol(symbol_spec)

            if result:
                return {"success": True, "symbol_info": asdict(result)}
            else:
                return {"success": False, "message": f"Symbol not found: {symbol_spec}"}

        except (OSError, ValueError) as e:
            logger.exception(f"Error getting symbol info: {e}")
            return {
                "success": False,
                "message": "Failed to get symbol info",
                "errorDetails": str(e),
            }


if __name__ == "__main__":
    # Test the symbol library manager
    import json

    logging.basicConfig(level=logging.INFO)

    manager = SymbolLibraryManager()

    print(f"\nFound {len(manager.libraries)} symbol libraries:")
    for name in list(manager.libraries.keys())[:10]:
        print(f"  - {name}")
    if len(manager.libraries) > 10:
        print(f"  ... and {len(manager.libraries) - 10} more")

    # Test search
    if manager.libraries:
        print("\n\nSearching for 'ESP32':")
        results = manager.search_symbols("ESP32", limit=5)
        for symbol in results:
            print(f"  - {symbol.full_ref}: {symbol.description or symbol.value}")
            if symbol.lcsc_id:
                print(f"      LCSC: {symbol.lcsc_id}")
