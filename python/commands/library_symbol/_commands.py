"""SymbolLibraryCommands: MCP-facing symbol library command handlers.

Split out of the former monolithic commands/library_symbol.py.
"""

import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional

from utils.responses import failed

from ._core import SymbolLibraryManager, get_symbol_library_manager
from ._models import SymbolInfo  # noqa: F401

logger = logging.getLogger("kicad_interface")


class SymbolLibraryCommands:
    """Command handlers for symbol library operations"""

    def __init__(self, library_manager: Optional[SymbolLibraryManager] = None):
        """Initialize with optional library manager"""
        self.library_manager = library_manager or get_symbol_library_manager()

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
            return failed("Failed to refresh symbol libraries", e)

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

            from utils.pagination import paginate

            symbol_dicts, page = paginate([asdict(s) for s in symbols], params)
            return {
                "success": True,
                "library": library,
                "symbols": symbol_dicts,
                **page,
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
            return failed("Failed to get symbol info", e)


if __name__ == "__main__":
    # Test the symbol library manager
    logging.basicConfig(level=logging.INFO)

    manager = SymbolLibraryManager()

    print(f"\nFound {len(manager.libraries)} symbol libraries:")
    for name in list(manager.libraries.keys())[:10]:
        print(f"  - {name}")
    if len(manager.libraries) > 10:
        print(f"  ... and {len(manager.libraries) - 10} more")

    if manager.libraries:
        print("\n\nSearching for 'ESP32':")
        results = manager.search_symbols("ESP32", limit=5)
        for symbol in results:
            print(f"  - {symbol.full_ref}: {symbol.description or symbol.value}")
            if symbol.lcsc_id:
                print(f"      LCSC: {symbol.lcsc_id}")
