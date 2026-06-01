"""
Test configuration for python/tests.

Sets up sys.modules stubs for heavy KiCAD modules (pcbnew, skip) before any
test module can trigger their import, preventing crashes on systems where the
real KiCAD environment is not fully initialised for testing.
"""

import importlib
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make the python/ package tree importable (commands.*, utils.*) regardless of
# whether a given test module has added it to sys.path yet — the autouse
# cache-reset fixture below imports commands.library.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

# Keep the test suite out of the real ~/.kicad-mcp/logs/kicad_interface.log:
# avoids polluting it and avoids concurrent pytest processes racing on
# RotatingFileHandler rollover. Set before kicad_interface is ever imported so
# its module-level logging setup sees the override (empty = stderr only).
os.environ.setdefault("KICAD_MCP_LOG_DIR", "")

# ---------------------------------------------------------------------------
# pcbnew stub — kicad_interface.py accesses pcbnew.__file__ and
# pcbnew.GetBuildVersion() at module level.  Use MagicMock so that any
# attribute access (pcbnew.BOARD, pcbnew.PCB_TRACK, …) returns a mock
# rather than raising AttributeError.
# ---------------------------------------------------------------------------
_pcbnew = MagicMock(name="pcbnew")
_pcbnew.__file__ = "/fake/pcbnew.cpython-39-x86_64-linux-gnu.so"
_pcbnew.__name__ = "pcbnew"
_pcbnew.__spec__ = None
_pcbnew.GetBuildVersion.return_value = "9.0.0-stub"
sys.modules["pcbnew"] = _pcbnew

# ---------------------------------------------------------------------------
# Stub: skip  (kicad-skip — use real module if available, stub otherwise)
# ---------------------------------------------------------------------------
try:
    import skip as _skip_test  # noqa: F401 — try importing real skip
except ImportError:
    skip_mod = types.ModuleType("skip")

    class _FakeSchematic:
        """Minimal stand-in for skip.Schematic used in PinLocator cache."""

        def __init__(self, path: str):
            self.path = path
            self.symbol = []

    skip_mod.Schematic = _FakeSchematic  # type: ignore[attr-defined]
    sys.modules["skip"] = skip_mod


@pytest.fixture(autouse=True)
def _reset_library_manager_caches():
    """Clear the process-wide LibraryManager / SymbolLibraryManager caches around
    every test so a cached (env-dependent) manager from one test cannot leak into
    another. No-op if those modules aren't importable in this environment."""
    try:
        from commands.library import _MANAGER_CACHE
        from commands.library_symbol import _SYMBOL_MANAGER_CACHE
    except Exception:
        yield
        return
    _MANAGER_CACHE.clear()
    _SYMBOL_MANAGER_CACHE.clear()
    try:
        yield
    finally:
        _MANAGER_CACHE.clear()
        _SYMBOL_MANAGER_CACHE.clear()


@pytest.fixture
def real_kipy():
    """Guarantee the REAL kipy package for a test, then restore prior state.

    Several IPC tests stub ``sys.modules["kipy"]`` with a MagicMock /
    non-package and don't restore it, which makes ``kipy.proto...``
    un-importable and silently breaks any later test that needs the real
    library (e.g. the kipy-10 ``get_open_documents`` / ``add_zone`` paths).
    This fixture swaps the real module in for the test's duration, then
    restores exactly what was there so it doesn't perturb others.  Skips
    when kipy isn't installed at all.
    """
    saved = {k: v for k, v in sys.modules.items() if k == "kipy" or k.startswith("kipy.")}

    def _purge():
        for k in [k for k in list(sys.modules) if k == "kipy" or k.startswith("kipy.")]:
            del sys.modules[k]

    _purge()
    try:
        importlib.import_module("kipy")
        importlib.import_module("kipy.proto.board.board_types_pb2")
        importlib.import_module("kipy.proto.common.types")
    except Exception:
        _purge()
        sys.modules.update(saved)
        pytest.skip("real kipy not installed")
    try:
        yield
    finally:
        _purge()
        sys.modules.update(saved)
