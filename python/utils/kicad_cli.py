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

import os
import platform
import shutil
from typing import Dict, List, Optional


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


def c_locale_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Return an environment dict with the C locale forced (``LC_ALL``/``LANG``).

    kicad-cli emits ERC/DRC violation *descriptions* in the user's UI locale, so
    on a non-English desktop the messages come back translated (e.g. Chinese) —
    which breaks any downstream tooling/agent that pattern-matches on the English
    text.  Running the subprocess under the C locale pins that text to stable
    English without disturbing anything else about the environment.

    ``base`` defaults to a copy of the current process environment; pass a
    pre-built env (e.g. one carrying a custom ``KICAD_CONFIG_HOME``) to layer the
    locale override on top of it.  Every other variable is preserved verbatim.
    """
    env = dict(os.environ if base is None else base)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env
