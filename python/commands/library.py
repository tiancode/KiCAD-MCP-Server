"""
Library management for KiCAD footprints

Handles parsing fp-lib-table files, discovering footprints,
and providing search functionality for component placement.
"""

import logging
import os
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Tuple, TypeVar

from utils.lib_tables import find_global_lib_table, parse_lib_table_entries, resolve_lib_uri
from utils.responses import failed

logger = logging.getLogger("kicad_interface")


class LibraryManager:
    """
    Manages KiCAD footprint libraries

    Parses fp-lib-table files (both global and project-specific),
    indexes available footprints, and provides search functionality.
    """

    def __init__(self, project_path: Optional[Path] = None):
        """
        Initialize library manager

        Args:
            project_path: Optional path to project directory for project-specific libraries
        """
        self.project_path = project_path
        self.libraries: Dict[str, str] = {}  # nickname -> path mapping
        self.footprint_cache: Dict[str, List[str]] = {}  # library -> [footprint names]
        # .pretty dir mtime_ns each footprint_cache entry reflects, so a footprint
        # added/removed mid-session is re-scanned even on a shared cached manager.
        self._footprint_cache_mtimes: Dict[str, int] = {}
        # Memoized results of the (filesystem-probing, log-emitting) dir finders.
        # Keyed by "footprint"/"3rdparty"; resolved at most once per instance so a
        # single _resolve_uri call doesn't probe + warn 4-5 times over.
        self._dir_cache: Dict[str, Optional[str]] = {}
        # fp-lib-tables actually parsed (global + any "Table" redirects + project),
        # recorded so table_signature() can detect on-disk edits for the cache in
        # get_library_manager().
        self._table_paths: List[Path] = []
        self._load_libraries()

    def table_signature(self) -> Dict[str, int]:
        """Return {path: mtime_ns} for every fp-lib-table consulted at load.

        Missing files map to -1 so a previously-absent project table appearing
        later also counts as a change. ``get_library_manager`` compares
        signatures to decide whether a cached manager is still fresh, letting a
        user edit the table from KiCad's GUI (or by hand) without restarting.
        """
        sig: Dict[str, int] = {}
        for path in self._table_paths:
            try:
                sig[str(path)] = path.stat().st_mtime_ns
            except OSError:
                sig[str(path)] = -1
        return sig

    def _load_libraries(self) -> None:
        """Load libraries from fp-lib-table files"""
        # Load global libraries. Track the table path even when absent so that
        # creating one later changes table_signature() and invalidates a cached
        # manager (see _load_cached_manager).
        global_table = self._get_global_fp_lib_table()
        if global_table:
            self._table_paths.append(global_table)
        if global_table and global_table.exists():
            logger.info(f"Loading global fp-lib-table from: {global_table}")
            self._parse_fp_lib_table(global_table)
        else:
            logger.warning(f"Global fp-lib-table not found at: {global_table}")

        # Load project-specific libraries if project path provided. Track the
        # (deterministic) project table path unconditionally so creating it
        # mid-session is detected even though it was absent at load.
        if self.project_path:
            project_table = self.project_path / "fp-lib-table"
            self._table_paths.append(project_table)
            if project_table.exists():
                logger.info(f"Loading project fp-lib-table from: {project_table}")
                self._parse_fp_lib_table(project_table)

        logger.info(f"Loaded {len(self.libraries)} footprint libraries")

    def _get_global_fp_lib_table(self) -> Optional[Path]:
        """Get path to global fp-lib-table file."""
        return find_global_lib_table("fp-lib-table")

    def _parse_fp_lib_table(self, table_path: Path) -> None:
        """
        Parse fp-lib-table file

        Format is S-expression (Lisp-like):
        (fp_lib_table
          (lib (name "Library_Name")(type KiCad)(uri "${KICAD9_FOOTPRINT_DIR}/Library.pretty")(options "")(descr "Description"))
        )
        """
        try:
            with open(table_path, "r") as f:
                content = f.read()
            # Capture followed "Table" redirect children too; _load_libraries
            # already tracked the global/project tables, so guard against dupes.
            if table_path not in self._table_paths:
                self._table_paths.append(table_path)

            parse_lib_table_entries(
                content,
                self._resolve_uri,
                self._parse_fp_lib_table,
                self.libraries,
                unresolved_level=logging.WARNING,
            )

        except (OSError, ValueError) as e:
            logger.exception(f"Error parsing fp-lib-table at {table_path}: {e}")

    def _resolve_uri(self, uri: str) -> Optional[str]:
        """
        Resolve environment variables and paths in library URI

        Handles:
        - ${KICAD9_FOOTPRINT_DIR} -> /usr/share/kicad/footprints
        - ${KICAD8_FOOTPRINT_DIR} -> /usr/share/kicad/footprints
        - ${KIPRJMOD} -> project directory
        - Relative paths
        - Absolute paths
        """
        # Common KiCAD environment variables
        env_vars: Dict[str, Optional[str]] = {
            "KICAD10_FOOTPRINT_DIR": self._find_kicad_footprint_dir(),
            "KICAD9_FOOTPRINT_DIR": self._find_kicad_footprint_dir(),
            "KICAD8_FOOTPRINT_DIR": self._find_kicad_footprint_dir(),
            "KICAD_FOOTPRINT_DIR": self._find_kicad_footprint_dir(),
            "KISYSMOD": self._find_kicad_footprint_dir(),
            "KICAD10_3RD_PARTY": self._find_kicad_3rdparty_dir(),
            "KICAD9_3RD_PARTY": self._find_kicad_3rdparty_dir(),
            "KICAD8_3RD_PARTY": self._find_kicad_3rdparty_dir(),
            "KICAD_3RD_PARTY": self._find_kicad_3rdparty_dir(),
        }

        return resolve_lib_uri(uri, env_vars, self.project_path)

    def _find_kicad_footprint_dir(self) -> Optional[str]:
        """Memoized wrapper around :meth:`_locate_kicad_footprint_dir`."""
        # setdefault so we stay robust if __init__ was bypassed (e.g. tests that
        # build via LibraryManager.__new__).
        cache = self.__dict__.setdefault("_dir_cache", {})
        if "footprint" not in cache:
            cache["footprint"] = self._locate_kicad_footprint_dir()
        return cache["footprint"]

    def _find_kicad_3rdparty_dir(self) -> Optional[str]:
        """Memoized wrapper around :meth:`_locate_kicad_3rdparty_dir`."""
        cache = self.__dict__.setdefault("_dir_cache", {})
        if "3rdparty" not in cache:
            cache["3rdparty"] = self._locate_kicad_3rdparty_dir()
        return cache["3rdparty"]

    def _locate_kicad_footprint_dir(self) -> Optional[str]:
        """Find the KiCAD stock footprint directory (first existing search root).

        Delegates to the ONE shared cross-platform resolver
        (utils.platform_helper.kicad_footprint_search_roots) that also backs
        list_footprint_libraries — so the two can no longer disagree about which
        roots to consider (the C6 divergence). Env-var overrides are already
        ordered first by the resolver, so the first existing root wins.
        """
        from utils.platform_helper import PlatformHelper

        for path in PlatformHelper.kicad_footprint_search_roots():
            if os.path.isdir(path):
                return path

        return None

    def _locate_kicad_3rdparty_dir(self) -> Optional[str]:
        """
        Find KiCAD 3rd party libraries directory.

        Resolution order:
        1. Shell environment variable KICAD9_3RD_PARTY
        2. User settings in kicad_common.json
        3. Platform-specific defaults based on detected KiCad version
        """
        import json

        # 1. Check shell environment variable first
        for var in ("KICAD10_3RD_PARTY", "KICAD9_3RD_PARTY", "KICAD8_3RD_PARTY", "KICAD_3RD_PARTY"):
            if var in os.environ:
                path = os.environ[var]
                if os.path.isdir(path):
                    return path

        # 2. Check kicad_common.json for user-defined variables
        kicad_common_paths = [
            Path.home()
            / "Library"
            / "Preferences"
            / "kicad"
            / "9.0"
            / "kicad_common.json",  # macOS
            Path.home() / ".config" / "kicad" / "9.0" / "kicad_common.json",  # Linux
            Path.home() / "AppData" / "Roaming" / "kicad" / "9.0" / "kicad_common.json",  # Windows
        ]

        for config_path in kicad_common_paths:
            if config_path.exists():
                try:
                    with open(config_path, "r") as f:
                        config = json.load(f)
                    env_vars = config.get("environment", {}).get("vars", {})
                    if env_vars and "KICAD9_3RD_PARTY" in env_vars:
                        path = env_vars["KICAD9_3RD_PARTY"]
                        if os.path.isdir(path):
                            return path
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass

                # Derive version from config path location
                version = config_path.parent.name  # e.g., "9.0"
                break
        else:
            version = "9.0"  # Default

        # 3. Use platform-specific defaults
        possible_paths = [
            # macOS - Documents/KiCad/{version}/3rdparty
            Path.home() / "Documents" / "KiCad" / version / "3rdparty",
            # Linux - ~/.local/share/kicad/{version}/3rdparty
            Path.home() / ".local" / "share" / "kicad" / version / "3rdparty",
            # Windows - Documents/KiCad/{version}/3rdparty
            Path.home() / "Documents" / "KiCad" / version / "3rdparty",
        ]

        for candidate in possible_paths:
            if candidate.exists():
                logger.info(f"Found KiCad 3rd party directory: {candidate}")
                return str(candidate)

        # Absence is the normal case (no Plugin & Content Manager add-on libs
        # installed); the standard footprint/symbol libs don't need it. Keep this
        # at debug so it doesn't spam the log as a false alarm.
        logger.debug("Could not find KiCad 3rd party directory")
        return None

    def list_libraries(self) -> List[str]:
        """Get list of available library nicknames"""
        return list(self.libraries.keys())

    def list_footprints(self, library_nickname: str) -> List[str]:
        """
        List all footprints in a library

        Args:
            library_nickname: Library name (e.g., "Resistor_SMD")

        Returns:
            List of footprint names (without .kicad_mod extension)
        """
        library_path = self.libraries.get(library_nickname)
        if not library_path:
            logger.warning(f"Library not found: {library_nickname}")
            return []

        lib_dir = Path(library_path)
        # Validate the cache against the .pretty directory mtime — adding or
        # removing a .kicad_mod bumps the directory mtime — so a stale entry on a
        # shared cached manager is re-scanned instead of served forever.
        try:
            dir_mtime = lib_dir.stat().st_mtime_ns
        except OSError:
            dir_mtime = -1
        if (
            library_nickname in self.footprint_cache
            and self._footprint_cache_mtimes.get(library_nickname) == dir_mtime
        ):
            return self.footprint_cache[library_nickname]

        try:
            footprints = []

            # List all .kicad_mod files
            for fp_file in lib_dir.glob("*.kicad_mod"):
                # Remove .kicad_mod extension
                footprint_name = fp_file.stem
                footprints.append(footprint_name)

            # Cache the results + the dir mtime they reflect
            self.footprint_cache[library_nickname] = footprints
            self._footprint_cache_mtimes[library_nickname] = dir_mtime
            logger.debug(f"Found {len(footprints)} footprints in {library_nickname}")

            return footprints

        except (OSError, ValueError) as e:
            logger.exception(f"Error listing footprints in {library_nickname}: {e}")
            return []

    def find_footprint(self, footprint_spec: str) -> Optional[Tuple[str, str]]:
        """
        Find a footprint by specification

        Supports multiple formats:
        - "Library:Footprint" (e.g., "Resistor_SMD:R_0603_1608Metric")
        - "Footprint" (searches all libraries)

        Args:
            footprint_spec: Footprint specification

        Returns:
            Tuple of (library_path, footprint_name) or None if not found
        """
        # Parse specification
        if ":" in footprint_spec:
            # Format: Library:Footprint
            library_nickname, footprint_name = footprint_spec.split(":", 1)
            library_path = self.libraries.get(library_nickname)

            if not library_path:
                logger.warning(f"Library not found: {library_nickname}")
                return None

            # Check if footprint exists
            fp_file = Path(library_path) / f"{footprint_name}.kicad_mod"
            if fp_file.exists():
                return (library_path, footprint_name)
            else:
                logger.warning(f"Footprint not found: {footprint_spec}")
                return None
        else:
            # Format: Footprint (search all libraries)
            footprint_name = footprint_spec

            # Search in all libraries
            for library_nickname, library_path in self.libraries.items():
                fp_file = Path(library_path) / f"{footprint_name}.kicad_mod"
                if fp_file.exists():
                    logger.info(f"Found footprint {footprint_name} in library {library_nickname}")
                    return (library_path, footprint_name)

            logger.warning(f"Footprint not found in any library: {footprint_name}")
            return None

    def search_footprints(self, pattern: str, limit: int = 20) -> List[Dict[str, str]]:
        """
        Search for footprints matching a pattern, ranked so the exact /
        most-relevant matches come first.

        Args:
            pattern: Search pattern (supports ``*`` wildcards; other
                regex metacharacters in the input are escaped so a
                pattern like ``LED_D5.0mm`` matches the literal dot).
                Case-insensitive.
            limit: Maximum number of results to return after ranking.

        Returns:
            List of dicts with 'library', 'footprint', and 'full_name'
            keys, ordered:
              1. Exact case-insensitive match (whole footprint name).
              2. Prefix match (footprint starts with the pattern stem).
              3. Substring match.
              ``Lib:Name`` style patterns rank the matching library
              higher when both library and name align.
            Within each band, shorter names come first — the user
            reported that ``LED_D5.0mm`` was buried under
            ``LED_D5.0mm-3``, ``LED_D5.0mm-3_Horizontal`` etc. precisely
            because the previous implementation returned matches in
            dict-iteration order and cut at ``limit`` before reaching
            the exact entry.
        """
        if not pattern:
            return []

        # Split "Lib:Name" prefix when given; the library scope becomes
        # a soft hint (still searches all, but boosts hits in the named
        # library).  Wildcards inside the library scope work too.
        lib_scope: Optional[str] = None
        name_pattern = pattern
        if ":" in pattern:
            lib_scope, name_pattern = pattern.split(":", 1)
            lib_scope = lib_scope.strip().lower() or None
            name_pattern = name_pattern.strip() or pattern

        # Escape regex metachars in the pattern, then re-enable ``*``
        # as the user-facing wildcard.  Previously ``.`` was treated as
        # "any char" so ``LED_D5.0mm`` also matched ``LED_D5X0mm``.
        name_lower = name_pattern.lower()
        escaped = re.escape(name_lower).replace(r"\*", ".*")
        try:
            regex = re.compile(escaped)
        except re.error:
            # Defensive: malformed pattern → no matches rather than crash.
            return []

        # Stem for prefix/exact comparison: the same input with wildcards
        # stripped so e.g. ``LED_D5.0mm*`` still recognises ``LED_D5.0mm``
        # as an exact match.
        stem = name_lower.replace("*", "")

        scored: List[Tuple[Tuple[int, int, int, str], Dict[str, str]]] = []
        for library_nickname in self.libraries.keys():
            lib_lower = library_nickname.lower()
            for footprint in self.list_footprints(library_nickname):
                fp_lower = footprint.lower()
                if not regex.search(fp_lower):
                    continue
                # Score band: exact (3) > prefix (2) > substring (1).
                # Higher score sorts first via negation in the key tuple.
                if fp_lower == stem:
                    band = 3
                elif fp_lower.startswith(stem):
                    band = 2
                else:
                    band = 1
                # Library-scope boost: when the caller used Lib:Name, hits
                # in the matching library outrank hits in other libraries
                # within the same band.
                lib_boost = 1 if lib_scope is None or lib_scope in lib_lower else 0
                # Sort key: lower tuple sorts first; we negate the bands
                # so larger-is-better becomes smaller-is-first.
                key = (-band, -lib_boost, len(footprint), fp_lower)
                scored.append(
                    (
                        key,
                        {
                            "library": library_nickname,
                            "footprint": footprint,
                            "full_name": f"{library_nickname}:{footprint}",
                        },
                    )
                )

        scored.sort(key=lambda pair: pair[0])
        return [entry for _, entry in scored[:limit]]

    def get_footprint_info(
        self, library_nickname: str, footprint_name: str
    ) -> Optional[Dict[str, str]]:
        """
        Get information about a specific footprint

        Args:
            library_nickname: Library name
            footprint_name: Footprint name

        Returns:
            Dict with footprint information or None if not found
        """
        library_path = self.libraries.get(library_nickname)
        if not library_path:
            return None

        fp_file = Path(library_path) / f"{footprint_name}.kicad_mod"
        if not fp_file.exists():
            return None

        return {
            "library": library_nickname,
            "footprint": footprint_name,
            "full_name": f"{library_nickname}:{footprint_name}",
            "path": str(fp_file),
            "library_path": library_path,
        }


class _MtimeCachedManager(Protocol):
    """Duck-typed surface shared by LibraryManager / SymbolLibraryManager.

    ``_load_cached_manager`` only needs these two members: ``libraries`` (to
    skip caching an empty load) and ``table_signature`` (to detect on-disk
    edits).
    """

    libraries: Dict[str, str]

    def table_signature(self) -> Dict[str, int]: ...


M = TypeVar("M", bound=_MtimeCachedManager)


def _load_cached_manager(
    cache: Dict[Optional[str], Tuple[M, Dict[str, int]]],
    ctor: Callable[[Optional[Path]], M],
    project_path: Optional[Path],
) -> M:
    """Return a process-wide cached manager for ``project_path``, rebuilding it
    when one of the lib-tables it parsed has changed on disk.

    Building a manager fully re-parses the global lib-table (~155 libs); hot
    paths (e.g. IPC footprint load) used to construct one per call. The cached
    instance is reused unless its ``table_signature`` moved. A manager that
    loaded **zero** libraries is not cached, so a not-yet-present lib-table is
    retried on the next call instead of being frozen as permanently empty.
    """
    key = str(project_path) if project_path is not None else None
    entry = cache.get(key)
    if entry is not None:
        manager, signature = entry
        if manager.table_signature() == signature:
            return manager
    manager = ctor(project_path)
    if manager.libraries:
        cache[key] = (manager, manager.table_signature())
    return manager


_MANAGER_CACHE: Dict[Optional[str], Tuple[LibraryManager, Dict[str, int]]] = {}


def get_library_manager(project_path: Optional[Path] = None) -> LibraryManager:
    """Return a cached :class:`LibraryManager` for the given project scope
    (``project_path=None`` for the global-only manager, the common case)."""
    return _load_cached_manager(_MANAGER_CACHE, LibraryManager, project_path)


class LibraryCommands:
    """Command handlers for library operations"""

    def __init__(self, library_manager: Optional[LibraryManager] = None):
        """Initialize with optional library manager"""
        self.library_manager = library_manager or get_library_manager()

    def list_libraries(self, params: Dict) -> Dict:
        """List all available footprint libraries"""
        try:
            libraries = self.library_manager.list_libraries()
            return {"success": True, "libraries": libraries, "count": len(libraries)}
        except (OSError, ValueError) as e:
            logger.exception(f"Error listing libraries: {e}")
            return failed("Failed to list libraries", e)

    def search_footprints(self, params: Dict) -> Dict:
        """Search for footprints by pattern"""
        try:
            # Support both 'pattern' and 'search_term' parameter names
            pattern = params.get("pattern") or params.get("search_term", "*")
            limit = params.get("limit", 20)
            library_filter = params.get("library")

            results = self.library_manager.search_footprints(
                pattern, limit * 10 if library_filter else limit
            )

            # Filter by library if specified
            if library_filter:
                results = [
                    r for r in results if r.get("library", "").lower() == library_filter.lower()
                ]
                results = results[:limit]

            return {
                "success": True,
                "footprints": results,
                "count": len(results),
                "pattern": pattern,
            }
        except (OSError, ValueError) as e:
            logger.exception(f"Error searching footprints: {e}")
            return failed("Failed to search footprints", e)

    def list_library_footprints(self, params: Dict) -> Dict:
        """List all footprints in a specific library"""
        try:
            library = params.get("library") or params.get("library_name")
            if not library:
                return {"success": False, "message": "Missing library parameter"}

            footprints = self.library_manager.list_footprints(library)

            from utils.pagination import paginate

            footprints, page = paginate(footprints, params)
            return {
                "success": True,
                "library": library,
                "footprints": footprints,
                **page,
            }
        except (OSError, ValueError) as e:
            logger.exception(f"Error listing library footprints: {e}")
            return failed("Failed to list library footprints", e)

    def get_footprint_info(self, params: Dict) -> Dict:
        """Get information about a specific footprint"""
        try:
            footprint_spec = params.get("footprint_name")
            if not footprint_spec:
                return {"success": False, "message": "Missing footprint parameter"}

            # Try to find the footprint
            result = self.library_manager.find_footprint(footprint_spec)

            if not result:
                # Not found — return a clean failure rather than falling through
                # to reference unbound locals (library_path / footprint_name),
                # which raised a NameError the outer handler didn't catch.
                return {
                    "success": False,
                    "message": "Footprint not found",
                    "errorDetails": (
                        f"Could not resolve footprint '{footprint_spec}' in any "
                        "registered library. If it lives in a project library, "
                        "open the project first so its fp-lib-table is loaded."
                    ),
                }

            library_path, footprint_name = result
            # Extract library nickname from path
            library_nickname = None
            for nick, path in self.library_manager.libraries.items():
                if path == library_path:
                    library_nickname = nick
                    break

            # Minimal info — always returned even if the parser fails
            info: Dict = {
                "library": library_nickname,
                "name": footprint_name,
                "full_name": f"{library_nickname}:{footprint_name}",
                "library_path": library_path,
            }

            # Attempt to enrich with parsed .kicad_mod data
            try:
                from pathlib import Path as _Path

                from parsers.kicad_mod_parser import parse_kicad_mod

                mod_file = str(_Path(library_path) / f"{footprint_name}.kicad_mod")
                parsed = parse_kicad_mod(mod_file)
                if parsed:
                    # Merge parser output into info; keep our resolved library context
                    info.update(parsed)
                    info["name"] = footprint_name  # entry name wins over in-file name
                    info["library"] = library_nickname
                    info["full_name"] = f"{library_nickname}:{footprint_name}"
                    info["library_path"] = library_path
                else:
                    logger.warning(
                        f"get_footprint_info: parser returned nothing for {mod_file}, using minimal info"
                    )
            except Exception as parse_err:
                logger.warning(
                    f"get_footprint_info: parser error ({parse_err}), using minimal info"
                )

            return {"success": True, "info": info}

        except (OSError, ValueError) as e:
            logger.exception(f"Error getting footprint info: {e}")
            return failed("Failed to get footprint info", e)
