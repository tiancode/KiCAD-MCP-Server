"""Single source of truth for locating the ``kicad-cli`` executable.

Historically this discovery logic was copy-pasted into three places
(``DesignRuleCommands._find_kicad_cli``, ``CommonMixin._find_kicad_cli`` and
``KiCADInterface._find_kicad_cli_static``) and the hardcoded install-path
lists had already drifted between them — only one knew about KiCad 10, and
none knew about the Homebrew prefix on Apple Silicon.  Those methods now all
delegate here so a new KiCad version or install location is added once.

PATH is always tried first via ``shutil.which`` (``kicad-cli.exe`` on
Windows); the platform-specific lists are only a fallback for installs that
don't put the binary on PATH — most notably macOS, where ``kicad-cli`` lives
inside ``KiCad.app/Contents/MacOS`` and is never on PATH by default.
"""

import atexit
import json
import logging
import os
import platform
import re
import shutil
import tempfile
import threading
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("kicad_interface")


def _candidate_paths() -> List[str]:
    """Platform-specific fallback locations, newest KiCad version first."""
    system = platform.system()

    if system == "Windows":
        roots = [r"C:\Program Files\KiCad", r"C:\Program Files (x86)\KiCad"]
        paths = [
            os.path.join(root, version, "bin", "kicad-cli.exe")
            for root in roots
            for version in ("10.0", "9.0", "8.0")
        ]
        # Bare bin/ (some installers drop the version directory).
        paths += [os.path.join(root, "bin", "kicad-cli.exe") for root in roots]
        return paths

    if system == "Darwin":  # macOS
        return [
            "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
            os.path.expanduser("~/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
            "/opt/homebrew/bin/kicad-cli",  # Apple Silicon Homebrew
            "/usr/local/bin/kicad-cli",  # Intel Homebrew
        ]

    # Linux and anything else Unix-like.
    return [
        "/usr/bin/kicad-cli",
        "/usr/local/bin/kicad-cli",
        "/opt/kicad/bin/kicad-cli",
    ]


def find_kicad_cli() -> Optional[str]:
    """Return the path to ``kicad-cli``, or ``None`` if it can't be found.

    Resolution order: system PATH, then the platform fallback locations.
    """
    cli_name = "kicad-cli.exe" if platform.system() == "Windows" else "kicad-cli"
    on_path = shutil.which(cli_name)
    if on_path:
        return on_path

    for path in _candidate_paths():
        if os.path.exists(path):
            return path

    return None


# ---------------------------------------------------------------------------
# Locale normalization for kicad-cli ERC / DRC.
#
# kicad-cli emits violation ``description`` text in the UI language taken from
# the KiCad config (``<KICAD_CONFIG_HOME>/<ver>/kicad_common.json`` →
# ``system.language``) — NOT from ``LC_ALL`` / ``LANG``.  A user whose KiCad UI
# is set to e.g. 简体中文 therefore gets localized ERC/DRC strings that the
# server's heuristics (which match English substrings) silently miss, and that
# an English-speaking agent can't act on.  ``LC_ALL=C`` alone does not fix this
# (verified against kicad-cli 10.0.4): the config language wins.
#
# The fix builds a derived KICAD_CONFIG_HOME that is a copy of the user's real
# config with ``system.language`` overridden so kicad-cli speaks English, and
# points the subprocess at it.  ``LC_ALL=C`` / ``LANG=C`` are kept too — they
# are harmless and normalize any string that is NOT config-driven.  The user's
# real config is only ever READ (copied), never modified.
#
# We write ``"English"`` (not ``"Default"``): ``"Default"`` means "follow the OS
# locale", so under a Chinese ``LANG`` it still yields Chinese even with the
# copied config — verified.  ``"English"`` is locale-independent.
# ---------------------------------------------------------------------------

# The KiCad UI-language value that forces English output regardless of the host
# OS locale.  See the note above for why this is not ``"Default"``.
_ENGLISH_LANGUAGE = "English"

_VERSION_DIR_RE = re.compile(r"\d+\.\d+")

# Cache the derived English config home for the no-existing-config case, keyed
# by the source ``kicad_common.json`` (path, mtime) so an external edit to the
# user's config rebuilds it.  Guarded by a lock — ERC/DRC are serialized in the
# dispatcher, but the background symbol-warm thread shares this process.
_en_config_lock = threading.Lock()
_en_config_cache: Dict[Tuple[str, float], str] = {}


def _config_home_candidates() -> List[str]:
    """Directories that may contain a ``<version>/kicad_common.json`` tree.

    Mirrors the library layer's cross-install probing (native / Flatpak /
    macOS sandbox / Windows) so the language override works wherever the real
    config lives.  ``KICAD_CONFIG_HOME`` (if the user set it) is tried first.
    """
    homes: List[str] = []
    env_home = os.environ.get("KICAD_CONFIG_HOME")
    if env_home:
        homes.append(env_home)
    home = os.path.expanduser("~")
    homes += [
        os.path.join(home, ".config", "kicad"),
        os.path.join(home, ".var", "app", "org.kicad.KiCad", "config", "kicad"),
        os.path.join(home, "Library", "Preferences", "kicad"),
        os.path.join(
            home,
            "Library",
            "Containers",
            "org.kicad.KiCad",
            "Data",
            "Library",
            "Preferences",
            "kicad",
        ),
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        homes.append(os.path.join(appdata, "kicad"))
    return homes


def _discover_real_config() -> Optional[Tuple[str, str]]:
    """Locate the user's real KiCad config: ``(config_home, version_dir)``.

    ``config_home`` is the directory that CONTAINS the versioned subdir (e.g.
    ``~/.config/kicad``); ``version_dir`` is that subdir's name (e.g. ``10.0``).
    Picks the newest version that actually carries a ``kicad_common.json``.
    Returns ``None`` when nothing is found (fresh machine) — the caller then
    relies on ``LC_ALL=C`` and kicad-cli's own English default.

    Using the real config's version-dir name (rather than kicad-cli's) is safe:
    kicad-cli appends its OWN version, so if the two ever differ kicad-cli reads
    no config and defaults to English anyway — never a wrong-language leak.
    """
    for home in _config_home_candidates():
        try:
            if not os.path.isdir(home):
                continue
            versions = [
                d
                for d in os.listdir(home)
                if _VERSION_DIR_RE.fullmatch(d)
                and os.path.exists(os.path.join(home, d, "kicad_common.json"))
            ]
        except OSError:
            continue
        if versions:
            versions.sort(key=lambda v: tuple(int(p) for p in v.split(".")), reverse=True)
            return home, versions[0]
    return None


def _write_english_language(common_json_path: str) -> None:
    """Set ``system.language`` = English in a kicad_common.json, preserving the
    rest of the file.  Creates a minimal file if it is missing/unreadable."""
    data: Dict = {}
    try:
        with open(common_json_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}
    system = data.get("system")
    if not isinstance(system, dict):
        system = {}
        data["system"] = system
    system["language"] = _ENGLISH_LANGUAGE
    with open(common_json_path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def force_english_config(config_home: str) -> None:
    """Override ``system.language`` to English in every versioned
    ``kicad_common.json`` under a config home WE OWN (a temp dir).

    Used when an existing merged-config home (e.g. the ERC sym-lib-table merge)
    is already being passed to kicad-cli: we override the language in place
    rather than build a second config.  Never call this on the user's real
    config directory.
    """
    try:
        entries = os.listdir(config_home)
    except OSError:
        return
    touched = False
    for name in entries:
        sub = os.path.join(config_home, name)
        if _VERSION_DIR_RE.fullmatch(name) and os.path.isdir(sub):
            _write_english_language(os.path.join(sub, "kicad_common.json"))
            touched = True
    if not touched:
        # No versioned dir present — create one so kicad-cli finds a config.
        discovered = _discover_real_config()
        version = discovered[1] if discovered else None
        if version:
            os.makedirs(os.path.join(config_home, version), exist_ok=True)
            _write_english_language(os.path.join(config_home, version, "kicad_common.json"))


def _english_config_home() -> Optional[str]:
    """Return (building/caching once) a KICAD_CONFIG_HOME that is a copy of the
    user's real config with the UI language forced to English.

    Returns ``None`` when no real config can be located — the caller then just
    relies on ``LC_ALL=C``.  Cache-invalidated on the source config's mtime.
    """
    discovered = _discover_real_config()
    if discovered is None:
        return None
    real_home, version = discovered
    src_dir = os.path.join(real_home, version)
    src_common = os.path.join(src_dir, "kicad_common.json")
    try:
        mtime = os.path.getmtime(src_common)
    except OSError:
        mtime = 0.0
    key = (src_common, mtime)

    with _en_config_lock:
        cached = _en_config_cache.get(key)
        if cached and os.path.isdir(cached):
            return cached
        try:
            dest_home = tempfile.mkdtemp(prefix="kicad-mcp-en-cfg-")
            dest_dir = os.path.join(dest_home, version)
            # Copy the whole real config dir (not just kicad_common.json): it
            # carries the user's env-var defs + the global lib tables that
            # kicad-cli resolves against; a bare stand-in would drop those.
            shutil.copytree(src_dir, dest_dir)
            _write_english_language(os.path.join(dest_dir, "kicad_common.json"))
        except OSError as e:
            logger.warning("Could not build English kicad-cli config: %s", e)
            return None
        _en_config_cache[key] = dest_home
        atexit.register(shutil.rmtree, dest_home, ignore_errors=True)
        return dest_home


def c_locale_env(
    base_env: Optional[Dict[str, str]] = None,
    owned_config_home: Optional[str] = None,
) -> Dict[str, str]:
    """Environment for a kicad-cli ERC/DRC subprocess that yields English text.

    * Sets ``LC_ALL=C`` / ``LANG=C`` (harmless; normalizes non-config strings).
    * Points ``KICAD_CONFIG_HOME`` at a config whose ``system.language`` is
      English so kicad-cli's violation ``description`` text is English.

    ``base_env`` is the starting environment (defaults to ``os.environ``).

    ``owned_config_home`` is an existing, WE-own-it config-home directory (e.g.
    the temp home the ERC sym-lib-table merge already built by copying the real
    config) whose language should be overridden in place; when given it is used
    as-is (with the language forced English) instead of building a second copy.
    Pass ``None`` (the common case, and always for DRC) to build/reuse the
    cached derived English config.  Never pass the user's real config dir here.
    """
    env: Dict[str, str] = dict(base_env if base_env is not None else os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"

    if owned_config_home:
        force_english_config(owned_config_home)
        env["KICAD_CONFIG_HOME"] = owned_config_home
        return env

    english_home = _english_config_home()
    if english_home:
        env["KICAD_CONFIG_HOME"] = english_home
    return env
