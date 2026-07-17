"""Library discovery, sym-lib-table parsing, and disk cache.

Split out of the former monolithic commands/library_symbol.py.
"""

import logging
import os
import pickle
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from utils.lib_tables import find_global_lib_table, parse_lib_table_entries, resolve_lib_uri

from ._models import SymbolInfo

logger = logging.getLogger("kicad_interface")

# Cache constants live here alongside the disk-cache methods that use them
# (mirroring the original single-module layout). Tests monkeypatch
# commands.library_symbol._manager_loading._DISK_CACHE_PATH to redirect the
# cache file. Bump _DISK_CACHE_VERSION when SymbolInfo fields or the cache
# structure change — OR when a parser fix changes which symbols/values a
# given .kicad_sym yields — so older pickles are rejected instead of
# producing stale/misshapen data.  v2: the block walker became string-aware
# (parens inside quoted strings no longer corrupt paren-depth), so v1 pickles
# written by the old parser could be missing whole libraries' worth of
# symbols (e.g. every MCU_ST_STM32H5 part); reject them and re-parse.
_DISK_CACHE_VERSION = 2
_DISK_CACHE_PATH = Path.home() / ".kicad-mcp" / "cache" / "symbol_libraries.pickle"

# Process-wide parsed-symbol store shared across ALL SymbolLibraryManager
# instances, keyed by the resolved .kicad_sym *absolute path* (not nickname —
# a project sym-lib-table may reuse a global nickname for a different file, so
# path is the robust key). Each entry is (symbols, source_mtime_ns).
#
# This is what makes the background symbol warm survive a manager rebuild:
# open_project -> use_project() constructs a fresh SymbolLibraryManager, whose
# __init__ hydrates its per-instance caches from this store, so the ~200 global
# libraries the warm already parsed on the default-scope manager are reused
# instead of re-parsed (~17 s -> <1 s). Project-specific libraries (new paths)
# still parse lazily. Entries are mtime-validated on hydration AND on every
# read (list_symbols -> _cached_if_fresh), so an external .kicad_sym edit still
# forces a re-parse. Writes go through _SHARED_PARSED_LOCK; the per-instance
# symbol_cache/_cache_mtimes dicts and the on-disk pickle are unchanged.
_SHARED_PARSED_LOCK = threading.Lock()
_SHARED_PARSED: Dict[str, Tuple[List[SymbolInfo], int]] = {}


def _reset_shared_symbol_cache() -> None:
    """Clear the process-wide shared parsed-symbol store.

    Used by the test suite (conftest) to keep per-test isolation, mirroring
    the existing ``_SYMBOL_MANAGER_CACHE.clear()`` discipline. Not used in
    production — the store is meant to live for the whole session.
    """
    with _SHARED_PARSED_LOCK:
        _SHARED_PARSED.clear()


class LoadingMixin:
    def _warm_cache(self) -> None:
        """Pre-parse all symbol libraries so the first search is instant.

        Called from ``__init__`` when KICAD_MCP_EAGER_SYMBOL_CACHE=1 (blocking
        eager warm) and from the default background warm thread
        (``start_background_symbol_warm`` in ``_core.py``); otherwise libraries
        are parsed lazily as ``list_symbols(nickname)`` is called.  The disk
        cache short-circuits most parses, so the price ranges from "near-zero
        (cache hit)" to "30-120 s (cold disk, no cache)".  See the module
        docstring for the cache file location.
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

    def _hydrate_from_shared_store(self) -> None:
        """Seed symbol_cache/_cache_mtimes from the process-wide shared store.

        Called at construction (after ``_load_disk_cache``) so a manager
        rebuilt for a new project scope — the open_project -> use_project()
        flow — instantly reuses libraries any other manager parsed this
        session (notably the background warm's default-scope manager),
        instead of re-parsing them.

        Only libraries this manager actually knows (present in
        ``self.libraries``) whose resolved path is in the shared store AND
        whose current on-disk mtime still matches the stored one are seeded;
        anything stale/absent parses lazily on first ``list_symbols`` (whose
        own mtime check still guards external edits). Existing per-instance
        entries (e.g. from the disk cache) are left untouched.
        """
        if not self.libraries:
            return
        with _SHARED_PARSED_LOCK:
            for nickname, library_path in self.libraries.items():
                if nickname in self.symbol_cache:
                    continue
                entry = _SHARED_PARSED.get(library_path)
                if entry is None:
                    continue
                symbols, stored_mtime = entry
                try:
                    current_mtime = os.stat(library_path).st_mtime_ns
                except OSError:
                    continue
                if current_mtime != stored_mtime:
                    continue
                self.symbol_cache.setdefault(nickname, symbols)
                self._cache_mtimes.setdefault(nickname, stored_mtime)

    def _publish_to_shared_store(
        self, library_path: str, symbols: List[SymbolInfo], mtime_ns: int
    ) -> None:
        """Record a freshly parsed library in the process-wide shared store.

        Keyed by resolved path so a later manager rebuild (new project scope)
        reuses this parse without re-reading the file. Thread-safe: the
        background warm and a racing search may both publish.
        """
        with _SHARED_PARSED_LOCK:
            _SHARED_PARSED[library_path] = (symbols, mtime_ns)

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

        Best-effort.  Called via atexit *and* immediately after the
        background warm completes (persist-early), so even a full manager
        rebuild that missed the in-memory shared store still starts hot off
        disk.  No-op when the cache hasn't been touched (saves an
        unnecessary pickle write on PCB-only sessions that never invoke
        search_symbols).

        Because the early call can overlap a concurrent search mutating the
        caches, the payload is snapshotted defensively and a
        ``RuntimeError`` ("dictionary changed size during iteration") is
        treated as a transient miss — atexit will retry.
        """
        if not self._cache_dirty:
            return
        try:
            _DISK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Shallow-copy under the risk of concurrent mutation; the copy
            # itself can race a live insert, so retry a couple of times.
            symbol_cache = mtimes = None
            for _ in range(3):
                try:
                    symbol_cache = dict(self.symbol_cache)
                    mtimes = dict(self._cache_mtimes)
                    break
                except RuntimeError:
                    continue
            if symbol_cache is None or mtimes is None:
                symbol_cache = dict(self.symbol_cache)
                mtimes = dict(self._cache_mtimes)
            payload = {
                "version": _DISK_CACHE_VERSION,
                "symbol_cache": symbol_cache,
                "mtimes": mtimes,
            }
            tmp = _DISK_CACHE_PATH.with_suffix(_DISK_CACHE_PATH.suffix + ".tmp")
            with tmp.open("wb") as fh:
                pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.replace(_DISK_CACHE_PATH)
            logger.info(
                "Saved symbol disk cache: %d libraries to %s",
                len(symbol_cache),
                _DISK_CACHE_PATH,
            )
        except (OSError, pickle.PicklingError, RuntimeError) as e:
            logger.warning("Could not persist symbol disk cache: %s", e)

    def _load_libraries(self) -> None:
        """Load libraries from sym-lib-table files"""
        # Track which sym-lib-table files were consulted so callers (the
        # signature check in SymbolLibraryCommands) can detect external
        # edits and rebuild the manager.  Includes the project table even
        # when it's missing, so creating one later triggers a rebuild.
        self._table_paths: List[Path] = []

        global_table = self._get_global_sym_lib_table()
        if global_table:
            self._table_paths.append(global_table)
        if global_table and global_table.exists():
            logger.info(f"Loading global sym-lib-table from: {global_table}")
            self._parse_sym_lib_table(global_table)
        else:
            logger.warning(f"Global sym-lib-table not found at: {global_table}")

        if self.project_path:
            project_table = self.project_path / "sym-lib-table"
            self._table_paths.append(project_table)
            if project_table.exists():
                logger.info(f"Loading project sym-lib-table from: {project_table}")
                self._parse_sym_lib_table(project_table)

        # Directory-scan fallback entries (vs sym-lib-table); list_symbol_libraries flags these.
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
        """Get path to global sym-lib-table file.

        Shares fp-lib-table's cross-platform lookup so a Flatpak/sandbox
        install finds both tables.
        """
        return find_global_lib_table("sym-lib-table")

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

            parse_lib_table_entries(
                content,
                self._resolve_uri,
                self._parse_sym_lib_table,
                self.libraries,
                unresolved_level=logging.DEBUG,
            )

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
        # Common KiCAD environment variables
        env_vars: Dict[str, Optional[str]] = {
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

        return resolve_lib_uri(uri, env_vars, self.project_path)

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
