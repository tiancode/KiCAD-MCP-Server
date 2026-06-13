"""Project sym-lib-table discovery + env shared by ERC and netlist.

Split out of the former handlers/schematic_io.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Tuple

if TYPE_CHECKING:
    from pathlib import Path

    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.schematic_io")


def _project_dir_for(schematic_path: str) -> "Path":
    """Return the project root for a schematic: the nearest ancestor holding a
    sym-lib-table or a *.kicad_pro, else the schematic's own directory."""
    from pathlib import Path

    sch = Path(schematic_path)
    for ancestor in sch.parents:
        if (ancestor / "sym-lib-table").exists() or list(ancestor.glob("*.kicad_pro")):
            return ancestor
    return sch.parent


def _build_project_lib_config_home(project_dir: "Path") -> Optional[Tuple[str, List[str]]]:
    """Write a throwaway KiCad config home whose *global* sym-lib-table is the
    real global table plus the project-local table's libraries, so a kicad-cli
    schematic subprocess can resolve project-scoped symbol libraries.

    kicad-cli ``sch erc`` / ``sch export netlist`` only ever load the global
    sym-lib-table — they do not merge the project-local ``sym-lib-table`` the
    way the GUI does. A schematic that uses a project-registered custom library
    therefore gets a spurious "symbol library 'X' is not in the current
    configuration" warning (ERC) or an incomplete ``<libraries>`` block
    (netlist), even though the lib is registered next to the project and the
    symbols are embedded. Pointing ``KICAD_CONFIG_HOME`` at this merged copy for
    the subprocess fixes both without touching the user's real config.

    Returns ``(config_home_dir, merged_nicknames)`` or ``None`` when there is
    nothing to merge or anything goes wrong — the caller then runs exactly as
    before. Best-effort: never raises into the caller's path.
    """
    import shutil
    import tempfile

    config_home: Optional[str] = None
    try:
        from commands.library_symbol import get_symbol_library_manager

        project_table = project_dir / "sym-lib-table"
        if not project_table.exists():
            return None  # no project-local table → nothing to merge

        mgr = get_symbol_library_manager(project_path=project_dir)
        global_table = mgr._get_global_sym_lib_table()
        # Only proceed when we can replicate the real global table — otherwise a
        # minimal stand-in would *hide* kicad-cli's own global table (dropping
        # the standard "KiCad" lib) and create worse warnings than it fixes.
        if not global_table or not global_table.exists():
            return None
        global_text = global_table.read_text(encoding="utf-8")

        # Nicknames already in global — skip those from the project table so the
        # merged table never has a duplicate nickname (a hard error in KiCad).
        nick_re = re.compile(r'\(name\s+"?([^")\s]+)"?\)')
        global_nicks = set(nick_re.findall(global_text))

        # Reuse SymbolLibraryManager's parser regex + URI resolver (handles
        # ${KIPRJMOD} and the KICAD*_SYMBOL_DIR vars) so project libs land as
        # absolute paths in the merged (global-context) table.
        lib_re = re.compile(
            r'\(lib\s+\(name\s+"?([^")\s]+)"?\)\s*\(type\s+"?([^")\s]+)"?\)\s*\(uri\s+"?([^")\s]+)"?',
            re.IGNORECASE,
        )
        project_text = project_table.read_text(encoding="utf-8")
        added_lines: List[str] = []
        added_nicks: List[str] = []
        for nickname, lib_type, uri in lib_re.findall(project_text):
            if nickname in global_nicks:
                continue
            resolved = mgr._resolve_uri(uri) or uri
            added_lines.append(
                f'  (lib (name "{nickname}")(type "{lib_type}")' f'(uri "{resolved}")(options ""))'
            )
            added_nicks.append(nickname)

        if not added_lines:
            return None  # project table adds nothing new

        # Splice the new lib lines in before the final close-paren of the
        # global table, preserving the global table verbatim otherwise.
        stripped = global_text.rstrip()
        if not stripped.endswith(")"):
            return None  # unexpected table shape; don't risk a broken merge
        merged_text = stripped[:-1].rstrip() + "\n" + "\n".join(added_lines) + "\n)\n"

        # kicad-cli appends the KiCad version dir to KICAD_CONFIG_HOME (e.g.
        # "10.0"), so the merged table must live under that exact name. The
        # global table normally sits in such a versioned dir; if it doesn't
        # (unusual flat layout) skip rather than write somewhere kicad-cli
        # won't read — running ERC unchanged beats hiding the global table.
        version_dir = global_table.parent.name
        if not re.fullmatch(r"\d+\.\d+", version_dir):
            return None

        config_home = tempfile.mkdtemp(prefix="kicad-mcp-erc-cfg-")
        dest_dir = os.path.join(config_home, version_dir)
        # Copy the whole real config dir, not just the table: kicad_common.json
        # carries the user's custom environment-variable definitions, and other
        # entries in the *global* table may resolve through them. A bare temp
        # config would drop those vars and turn this fix into a new source of
        # "library not in configuration" warnings. We then overwrite only the
        # sym-lib-table with the merged copy; nothing in the real config is
        # touched (copytree reads).
        shutil.copytree(global_table.parent, dest_dir)
        with open(os.path.join(dest_dir, "sym-lib-table"), "w", encoding="utf-8") as f:
            f.write(merged_text)
        return config_home, added_nicks
    except Exception as e:  # best-effort — the caller must still run on failure
        logger.warning("Could not build merged sym-lib config: %s", e)
        if config_home and os.path.isdir(config_home):
            shutil.rmtree(config_home, ignore_errors=True)
        return None


@contextlib.contextmanager
def _merged_project_lib_env(project_dir: "Path") -> "Iterator[Tuple[Optional[dict], List[str]]]":
    """Context manager yielding ``(env, merged_nicknames)`` for a kicad-cli
    schematic subprocess that must resolve project-scoped symbol libraries.

    ``env`` is ``None`` (inherit the process environment unchanged) when there
    is no project-local table or nothing to merge; otherwise it is a copy of the
    environment with ``KICAD_CONFIG_HOME`` pointed at a merged config. The temp
    config home, if any, is always removed on exit.
    """
    merged = _build_project_lib_config_home(project_dir)
    if merged is None:
        yield None, []
        return
    config_home, nicknames = merged
    try:
        yield {**os.environ, "KICAD_CONFIG_HOME": config_home}, nicknames
    finally:
        import shutil

        shutil.rmtree(config_home, ignore_errors=True)
