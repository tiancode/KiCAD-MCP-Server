"""
Library management for KiCAD symbols

Handles parsing sym-lib-table files, discovering symbols,
and providing search functionality for component selection.
"""

import atexit
import heapq
import logging
import os
import pickle
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


@dataclass(frozen=True)
class _SearchPlan:
    """Resolved inputs for one symbol search.

    Built by ``SymbolLibraryManager.plan_search`` and consumed by both
    the executor and the response layer so they can't disagree about
    what was searched.  The fields are independent so the response layer
    can distinguish "no library matched the explicit filter" (warn) from
    "inline prefix parsed and used as scope" (just report it).
    """

    name_query: str
    effective_library: Optional[str]
    inline_prefix: Optional[str]
    libraries_searched: List[str] = field(default_factory=list)
    library_filter_matched_nothing: bool = False


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
        # Track which sym-lib-table files were consulted so callers (the
        # signature check in SymbolLibraryCommands) can detect external
        # edits and rebuild the manager.  Includes the project table even
        # when it's missing, so creating one later triggers a rebuild.
        self._table_paths: List[Path] = []

        # Load global libraries
        global_table = self._get_global_sym_lib_table()
        if global_table:
            self._table_paths.append(global_table)
        if global_table and global_table.exists():
            logger.info(f"Loading global sym-lib-table from: {global_table}")
            self._parse_sym_lib_table(global_table)
        else:
            logger.warning(f"Global sym-lib-table not found at: {global_table}")

        # Load project-specific libraries if project path provided
        if self.project_path:
            project_table = self.project_path / "sym-lib-table"
            self._table_paths.append(project_table)
            if project_table.exists():
                logger.info(f"Loading project sym-lib-table from: {project_table}")
                self._parse_sym_lib_table(project_table)

        # Track which entries came from the table vs the directory-scan
        # fallback so list_symbol_libraries can flag the latter to callers.
        self._table_library_nicknames: set[str] = set(self.libraries.keys())
        self._fallback_library_nicknames: set[str] = set()

        # Fallback: when the sym-lib-table yields zero usable libraries
        # (typical on Flatpak/bwrap where the default table redirects to a
        # sandbox-internal /app/... path the host can't see), scan the
        # known symbol directories directly and surface every .kicad_sym
        # file as a virtual library entry.  This is what list_symbol_libraries
        # ends up returning when A1 from MCP_FEEDBACK.md applies.
        if not self.libraries:
            discovered = self._discover_libraries_by_scan()
            if discovered:
                logger.warning(
                    "sym-lib-table yielded 0 libraries; falling back to "
                    "directory scan and discovered %d .kicad_sym files",
                    len(discovered),
                )
                self.libraries.update(discovered)
                self._fallback_library_nicknames.update(discovered.keys())

        logger.info(f"Loaded {len(self.libraries)} symbol libraries")

    def _discover_libraries_by_scan(self) -> Dict[str, str]:
        """Scan the known symbol directories for .kicad_sym files.

        Used as the fallback when sym-lib-table is empty/unreadable (e.g.
        Flatpak default config points the table at a sandbox-only path).
        Nicknames are taken from the file stem; duplicates are suffixed
        with their parent directory so we don't lose entries.
        """
        discovered: Dict[str, str] = {}
        roots: List[Path] = []
        for finder in (self._find_kicad_symbol_dir, self._find_3rd_party_dir):
            try:
                p = finder()
            except OSError:
                p = None
            if p:
                roots.append(Path(p))

        if self.project_path:
            roots.append(self.project_path)

        for root in roots:
            if not root.is_dir():
                continue
            try:
                for sym_file in sorted(root.rglob("*.kicad_sym")):
                    nickname = sym_file.stem
                    if nickname in discovered or nickname in self.libraries:
                        # Disambiguate by parent dir name when stems collide.
                        nickname = f"{sym_file.parent.name}__{sym_file.stem}"
                    discovered[nickname] = str(sym_file)
            except OSError as e:
                logger.debug("Symbol scan in %s failed: %s", root, e)

        return discovered

    def _get_global_sym_lib_table(self) -> Optional[Path]:
        """Get path to global sym-lib-table file."""
        # Match fp-lib-table's lookup so a Flatpak/sandbox install finds
        # both tables.  See library.py:_get_global_fp_lib_table for the
        # canonical comment about the .var/app sandbox path.
        linux_bases = [
            Path.home() / ".config" / "kicad",
            Path.home() / ".var" / "app" / "org.kicad.KiCad" / "config" / "kicad",
        ]
        windows_bases = [Path.home() / "AppData" / "Roaming" / "kicad"]
        macos_bases = [
            Path.home() / "Library" / "Preferences" / "kicad",
            Path.home()
            / "Library"
            / "Containers"
            / "org.kicad.KiCad"
            / "Data"
            / "Library"
            / "Preferences"
            / "kicad",
        ]

        kicad_config_paths: List[Path] = []
        for base in linux_bases + windows_bases + macos_bases:
            for version in ("10.0", "9.0", "8.0"):
                kicad_config_paths.append(base / version / "sym-lib-table")
            kicad_config_paths.append(base / "sym-lib-table")

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
        """Find KiCAD symbol directory."""
        possible_paths = [
            "/usr/share/kicad/symbols",
            "/usr/local/share/kicad/symbols",
            "C:/Program Files/KiCad/10.0/share/kicad/symbols",
            "C:/Program Files/KiCad/9.0/share/kicad/symbols",
            "C:/Program Files/KiCad/8.0/share/kicad/symbols",
            "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols",
        ]

        # Environment variable takes precedence.
        for var in ("KICAD10_SYMBOL_DIR", "KICAD9_SYMBOL_DIR", "KICAD8_SYMBOL_DIR"):
            if var in os.environ:
                possible_paths.insert(0, os.environ[var])

        # Flatpak: symbols ship as a separate Library.Symbols runtime extension.
        # See library.py:_find_kicad_footprint_dir for the same pattern.
        try:
            flatpak_glob = sorted(
                Path("/var/lib/flatpak/runtime/org.kicad.KiCad.Library.Symbols").glob(
                    "*/stable/*/files/symbols"
                )
            )
            if flatpak_glob:
                possible_paths.append(str(flatpak_glob[-1]))
        except OSError:
            pass

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

        # Locate "(symbol \"<name>\" " and walk paren depth to the closing ).
        needle = f'(symbol "{symbol_name}"'
        start_pos = content.find(needle)
        if start_pos == -1:
            return None
        depth = 0
        end_pos = start_pos
        i = start_pos
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
                f"Malformed symbol block for '{symbol_name}' in {library_path}"
            )
            return None

        try:
            import sexpdata

            from commands.pin_locator import PinLocator

            sexp = sexpdata.loads(content[start_pos:end_pos])
        except Exception as e:
            logger.warning(f"Could not parse symbol block for {symbol_name}: {e}")
            return None

        pins_dict = PinLocator.parse_symbol_definition(sexp)

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

    def _table_changed(self) -> bool:
        """Return True if any consulted sym-lib-table has been edited/created
        since the current manager loaded it.  This is the cheap-check that
        lets users edit their sym-lib-table from KiCad's GUI (or by hand)
        mid-session without restarting the MCP server."""
        try:
            current = self.library_manager.table_signature()
        except AttributeError:
            return False
        previous = getattr(self, "_last_table_signature", None)
        if previous is None:
            self._last_table_signature = current
            return False
        if current != previous:
            self._last_table_signature = current
            return True
        return False

    def _ensure_manager_for(self, params: Dict) -> None:
        """Rebuild the library manager if the caller's project differs OR
        any sym-lib-table mtime has moved since the last load."""
        project = self._derive_project_path(params)
        if project is not None:
            self.use_project(project)
        if self._table_changed():
            logger.info("sym-lib-table mtime changed; rebuilding SymbolLibraryManager")
            self.library_manager = SymbolLibraryManager(
                project_path=self.library_manager.project_path
            )
            self._last_table_signature = self.library_manager.table_signature()

    def refresh_symbol_libraries(self, params: Dict) -> Dict:
        """Force-rebuild the SymbolLibraryManager, re-reading every
        sym-lib-table from disk.  Use this after editing the table outside
        the MCP server (e.g. from KiCad's GUI, or by hand to work around
        Flatpak's default template-redirection table)."""
        try:
            project = self._derive_project_path(params) or self.library_manager.project_path
            self.library_manager = SymbolLibraryManager(project_path=project)
            self._last_table_signature = self.library_manager.table_signature()
            libraries = self.library_manager.list_libraries()
            fallback = sorted(getattr(self.library_manager, "_fallback_library_nicknames", set()))
            result: Dict = {
                "success": True,
                "message": f"Rebuilt symbol library index ({len(libraries)} libraries)",
                "count": len(libraries),
                "libraries": libraries,
            }
            if fallback:
                result["source"] = "directory_scan_fallback"
                result["fallback_libraries"] = fallback
            return result
        except (OSError, ValueError) as e:
            logger.exception(f"Error refreshing symbol libraries: {e}")
            return {
                "success": False,
                "message": "Failed to refresh symbol libraries",
                "errorDetails": str(e),
            }

    def list_symbol_libraries(self, params: Dict) -> Dict:
        """List all available symbol libraries"""
        try:
            self._ensure_manager_for(params)
            libraries = self.library_manager.list_libraries()
            fallback = sorted(getattr(self.library_manager, "_fallback_library_nicknames", set()))
            result: Dict = {
                "success": True,
                "libraries": libraries,
                "count": len(libraries),
            }
            if fallback:
                # Signal to the caller that the sym-lib-table was unusable
                # and these names came from a filesystem scan; the URIs
                # won't be in any project's lib-table so symbol references
                # can't be inserted by `add_schematic_component` directly.
                result["source"] = "directory_scan_fallback"
                result["fallback_libraries"] = fallback
                result["warning"] = (
                    "sym-lib-table was empty or unreachable (typical on "
                    "Flatpak/bwrap installs); these libraries were "
                    "auto-discovered by scanning the symbol directory. "
                    "Add them to your sym-lib-table or call "
                    "refresh_symbol_libraries after fixing the table to "
                    "make them addressable from add_schematic_component."
                )
            return result
        except Exception as e:
            # Catch broad so the surfaced error tells the agent *what* failed
            # — earlier (OSError, ValueError) let KeyError/AttributeError/etc.
            # propagate and bubble up as the dispatcher's generic
            # "Error handling command" message, which the TS layer then
            # rendered as "Unknown error".
            import traceback as _tb

            logger.exception(f"Error listing symbol libraries: {e}")
            return {
                "success": False,
                "message": f"Failed to list symbol libraries: {type(e).__name__}: {e}",
                "errorDetails": _tb.format_exc(),
                "exceptionType": type(e).__name__,
            }

    def search_symbols(self, params: Dict) -> Dict:
        """Search for symbols by query.

        Recognizes both ``"Name"`` and ``"Library:Name"`` query forms.
        Builds one ``_SearchPlan`` via the manager (so the handler and
        the executor see the same parsed name / library scope) and uses
        it both to run the search and to compose any ``interpretation``
        or ``warning`` fields in the response.
        """
        try:
            query = params.get("query", "")
            if not query:
                return {"success": False, "message": "Missing query parameter"}

            self._ensure_manager_for(params)

            limit = params.get("limit", 20)
            library_filter = params.get("library")

            plan = self.library_manager.plan_search(query, library_filter)
            results = self.library_manager.execute_search_plan(plan, limit)

            response: Dict[str, Any] = {
                "success": True,
                "symbols": [asdict(s) for s in results],
                "count": len(results),
                "query": query,
            }

            if plan.inline_prefix is not None:
                # An inline "Library:Name" prefix was parsed — tell the
                # agent so they can confirm the parse matched their
                # intent.  When an explicit library_filter param also
                # supplied a (different) library, surface the override
                # explicitly: the colon prefix was *stripped* from the
                # name but the explicit param won as the library scope.
                interp: Dict[str, Any] = {
                    "parsedAs": "library:name",
                    "library": plan.effective_library,
                    "name": plan.name_query,
                }
                if library_filter and library_filter != plan.inline_prefix:
                    interp["note"] = (
                        f"explicit library={library_filter!r} overrode "
                        f"inline prefix {plan.inline_prefix!r}; searched "
                        f"{library_filter!r} for {plan.name_query!r}"
                    )
                response["interpretation"] = interp

            if plan.library_filter_matched_nothing:
                # Loud hint when the explicit filter excluded everything
                # — otherwise an empty result looks like "the symbol
                # doesn't exist" when the real cause is an unknown
                # library name.  (Inline colon prefixes that don't
                # match any library fall back to a global fuzzy search
                # inside ``split_library_qualifier`` and never reach
                # this branch.)
                sample = list(self.library_manager.libraries.keys())[:10]
                response["warning"] = (
                    f"No library nickname matches {plan.effective_library!r}. "
                    f"Loaded {len(self.library_manager.libraries)} libraries; "
                    f"sample: {sample}. "
                    f"Call list_symbol_libraries to see all names."
                )

            return response
        except Exception as e:
            import traceback as _tb

            logger.exception(f"Error searching symbols: {e}")
            return {
                "success": False,
                "message": f"Failed to search symbols: {type(e).__name__}: {e}",
                "errorDetails": _tb.format_exc(),
                "exceptionType": type(e).__name__,
            }

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
        except Exception as e:
            import traceback as _tb

            logger.exception(f"Error listing library symbols: {e}")
            return {
                "success": False,
                "message": f"Failed to list library symbols: {type(e).__name__}: {e}",
                "errorDetails": _tb.format_exc(),
                "exceptionType": type(e).__name__,
            }

    def get_symbol_info(self, params: Dict) -> Dict:
        """Get information about a specific symbol — properties + pin list."""
        try:
            symbol_spec = params.get("symbol")
            if not symbol_spec:
                return {"success": False, "message": "Missing symbol parameter"}

            self._ensure_manager_for(params)

            result = self.library_manager.find_symbol(symbol_spec)

            if not result:
                return {"success": False, "message": f"Symbol not found: {symbol_spec}"}

            info = asdict(result)
            # Inline pins so the caller can plan placement without a
            # round-trip via add_schematic_component → get_schematic_pin_locations.
            # Each pin's (x, y) is in the symbol's own coordinate frame;
            # after add_schematic_component(x=ax, y=ay, rotation=r), the
            # world-space pin position is the local pin (x, y) rotated
            # by ``r`` around (0, 0) and translated by (ax, ay).
            try:
                pins = self.library_manager.get_symbol_pins(result.library, result.name)
            except Exception as e:
                logger.debug(f"Could not extract pins for {result.full_ref}: {e}")
                pins = None
            if pins is not None:
                info["pins"] = pins
                info["pin_count"] = len(pins)
                # Local-coord bounding box of the pin endpoints — a quick
                # planning heuristic for collision avoidance before the
                # symbol is even on the schematic.
                if pins:
                    xs = [p.get("x", 0) for p in pins]
                    ys = [p.get("y", 0) for p in pins]
                    info["pin_bounding_box"] = {
                        "min_x": min(xs),
                        "min_y": min(ys),
                        "max_x": max(xs),
                        "max_y": max(ys),
                        "unit": "mm",
                    }

            return {"success": True, "symbol_info": info}

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
