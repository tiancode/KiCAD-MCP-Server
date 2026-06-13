"""Library discovery, sym-lib-table parsing, and disk cache.

Split out of the former monolithic commands/library_symbol.py.
"""

import atexit
import logging
import os
import pickle
import re
from pathlib import Path
from typing import Dict, List, Optional

from ._models import SymbolInfo

logger = logging.getLogger("kicad_interface")

# Cache constants live here alongside the disk-cache methods that use them
# (mirroring the original single-module layout). Tests monkeypatch
# commands.library_symbol._manager_loading._DISK_CACHE_PATH to redirect the
# cache file. Bump _DISK_CACHE_VERSION when SymbolInfo fields or the cache
# structure change so older pickles are rejected instead of producing
# stale/misshapen data.
_DISK_CACHE_VERSION = 1
_DISK_CACHE_PATH = Path.home() / ".kicad-mcp" / "cache" / "symbol_libraries.pickle"


class LoadingMixin:
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
