"""Shared helpers for KiCAD fp-lib-table / sym-lib-table discovery + parsing.

Both ``LibraryManager`` (footprints, ``commands/library.py``) and
``SymbolLibraryManager`` (symbols, ``commands/library_symbol/``) locate the same
global lib-table across platforms, parse the same S-expression ``(lib …)``
format, and resolve the same ``${KICADx_*_DIR}`` style URIs.  These helpers hold
the identical core so the two managers can't drift; behaviour that genuinely
differs between them (file encoding, table-path tracking, unresolved-URI log
level, which env vars map to which directory) stays in the callers.
"""

import logging
import os
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("kicad_interface")

# Pattern: (lib (name "NAME")(type TYPE)(uri "URI")...)
_LIB_TABLE_ENTRY_RE = re.compile(
    r'\(lib\s+\(name\s+"?([^")\s]+)"?\)\s*\(type\s+"?([^")\s]+)"?\)\s*\(uri\s+"?([^")\s]+)"?',
    re.IGNORECASE,
)


def find_global_lib_table(table_name: str) -> Optional[Path]:
    """Return the first existing global ``table_name`` file.

    Probes the known KiCAD config locations — Linux native + Flatpak (Flathub
    sandboxes the config under ``.var/app``), Windows, and macOS native +
    sandboxed (App Store / Mac App) — trying versioned (10.0/9.0/8.0) then
    unversioned directories.  ``table_name`` is ``"fp-lib-table"`` or
    ``"sym-lib-table"``.
    """
    # Linux native + Flatpak (Flathub sandboxes the config under .var/app)
    linux_bases = [
        Path.home() / ".config" / "kicad",
        Path.home() / ".var" / "app" / "org.kicad.KiCad" / "config" / "kicad",
    ]
    # Windows
    windows_bases = [Path.home() / "AppData" / "Roaming" / "kicad"]
    # macOS native + sandboxed (App Store / Mac App)
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
            kicad_config_paths.append(base / version / table_name)
        kicad_config_paths.append(base / table_name)

    for path in kicad_config_paths:
        if path.exists():
            return path

    return None


def parse_lib_table_entries(
    content: str,
    resolve_uri: Callable[[str], Optional[str]],
    follow_table: Callable[[Path], None],
    libraries: Dict[str, str],
    *,
    unresolved_level: int = logging.WARNING,
) -> None:
    """Parse ``(lib …)`` entries from a fp/sym-lib-table into ``libraries``.

    ``resolve_uri`` expands a raw URI to an absolute path (or ``None``);
    ``follow_table`` is invoked to recurse into a ``(type Table)`` redirect;
    a library whose URI can't be resolved is logged at ``unresolved_level``
    (footprints warn, symbols debug — matching each manager's original
    behaviour).
    """
    for match in _LIB_TABLE_ENTRY_RE.finditer(content):
        nickname = match.group(1)
        lib_type = match.group(2)
        uri = match.group(3)

        if lib_type.lower() == "table":
            table_uri = uri
            if os.path.isabs(table_uri) and os.path.isfile(table_uri):
                logger.info(f"  Following Table reference: {nickname} -> {table_uri}")
                follow_table(Path(table_uri))
            else:
                logger.warning(f"  Could not resolve Table URI: {table_uri}")
            continue

        # Resolve environment variables in URI
        resolved_uri = resolve_uri(uri)

        if resolved_uri:
            libraries[nickname] = resolved_uri
            logger.debug(f"  Found library: {nickname} -> {resolved_uri}")
        else:
            logger.log(unresolved_level, f"  Could not resolve URI for library {nickname}: {uri}")


def resolve_lib_uri(
    uri: str,
    env_vars: Dict[str, Optional[str]],
    project_path: Optional[Path],
) -> Optional[str]:
    """Resolve environment variables and paths in a library URI.

    Substitutes ``${VAR}`` / ``$VAR`` for each entry in ``env_vars`` (values of
    ``None`` are skipped), adds ``KIPRJMOD`` from ``project_path`` when set
    (appended last, as the original managers did), expands ``~``, and returns
    the absolute path if it exists — else ``None``.
    """
    resolved = uri

    # Project directory (appended last so it can't shadow the dir vars above)
    if project_path is not None:
        env_vars = {**env_vars, "KIPRJMOD": str(project_path)}

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
