"""Unit tests for the background symbol-cache warm (F1).

The first cold ``search_symbols`` parses every ``.kicad_sym`` and can block
~60-70 s.  ``start_background_symbol_warm`` moves that cost onto a daemon
thread started *after* the ``{"type": "ready"}`` handshake, so startup, the
``_warmup`` command, and queued commands are never delayed.

Covers:
  - env gating: KICAD_MCP_BG_SYMBOL_WARM off / eager-flag set => no thread
  - default-on: a thread is started and runs the warm through the shared
    manager, writing nothing to stdout
  - warm marks the cache so a subsequent search is a pure cache hit
  - the thread kickoff sits after the ready print in main() (ordering)
  - per-library locking: concurrent warm + search parse each library once

pcbnew is stubbed globally by tests/conftest.py; no real KiCad needed.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

import commands.library_symbol._core as lib_core  # noqa: E402
from commands.library_symbol import (  # noqa: E402
    SymbolLibraryManager,
    start_background_symbol_warm,
)

_TINY_LIB = (
    '(kicad_symbol_lib (version 20241209) (generator "test") '
    '(symbol "{name}" '
    '(property "Reference" "{ref}") '
    '(property "Value" "{name}") '
    '(property "Description" "{desc}")))\n'
)


def _write_lib(path: Path, name: str, ref: str, desc: str) -> Path:
    path.write_text(_TINY_LIB.format(name=name, ref=ref, desc=desc), encoding="utf-8")
    return path


def _bare_manager(libraries) -> SymbolLibraryManager:
    mgr = SymbolLibraryManager.__new__(SymbolLibraryManager)
    mgr.project_path = None
    mgr.libraries = dict(libraries)
    mgr.symbol_cache = {}
    mgr._cache_mtimes = {}
    mgr._cache_dirty = False
    return mgr


# ---------------------------------------------------------------------------
# (a) env gating — no thread started
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackgroundWarmEnvGating:
    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "False", " 0 "])
    def test_disabled_by_env_flag_returns_no_thread(self, monkeypatch, value):
        monkeypatch.delenv("KICAD_MCP_EAGER_SYMBOL_CACHE", raising=False)
        monkeypatch.setenv("KICAD_MCP_BG_SYMBOL_WARM", value)
        assert start_background_symbol_warm() is None

    def test_eager_flag_skips_background_thread(self, monkeypatch):
        """Blocking eager warm and the bg warm must not both run."""
        monkeypatch.setenv("KICAD_MCP_EAGER_SYMBOL_CACHE", "1")
        monkeypatch.delenv("KICAD_MCP_BG_SYMBOL_WARM", raising=False)
        assert start_background_symbol_warm() is None

    @pytest.mark.parametrize("value", ["", "1", "true", "yes", "on"])
    def test_enabled_by_default_and_other_values(self, monkeypatch, value):
        monkeypatch.delenv("KICAD_MCP_EAGER_SYMBOL_CACHE", raising=False)
        if value == "":
            monkeypatch.delenv("KICAD_MCP_BG_SYMBOL_WARM", raising=False)
        else:
            monkeypatch.setenv("KICAD_MCP_BG_SYMBOL_WARM", value)
        assert lib_core._bg_symbol_warm_enabled() is True


# ---------------------------------------------------------------------------
# (b) default-on: a thread runs the warm, writes nothing to stdout
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackgroundWarmRuns:
    def test_thread_started_and_warms_shared_manager(self, monkeypatch, capsys):
        monkeypatch.delenv("KICAD_MCP_EAGER_SYMBOL_CACHE", raising=False)
        monkeypatch.delenv("KICAD_MCP_BG_SYMBOL_WARM", raising=False)

        ran = threading.Event()

        class _FakeManager:
            symbol_cache = {"LibA": [], "LibB": []}

            def _warm_cache(self):
                ran.set()

        monkeypatch.setattr(lib_core, "get_symbol_library_manager", lambda: _FakeManager())

        thread = start_background_symbol_warm()
        assert isinstance(thread, threading.Thread)
        assert thread.daemon is True
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert ran.is_set(), "the bg thread must call _warm_cache on the shared manager"

        # The warm must never touch stdout — that channel carries the JSON
        # protocol; diagnostics go to the logger only.
        assert capsys.readouterr().out == ""

    def test_warm_failure_does_not_raise(self, monkeypatch):
        """A broken lib-table must not take the process down."""
        monkeypatch.delenv("KICAD_MCP_EAGER_SYMBOL_CACHE", raising=False)
        monkeypatch.delenv("KICAD_MCP_BG_SYMBOL_WARM", raising=False)

        done = threading.Event()

        class _Boom:
            symbol_cache: dict = {}

            def _warm_cache(self):
                try:
                    raise RuntimeError("broken sym-lib-table")
                finally:
                    done.set()

        monkeypatch.setattr(lib_core, "get_symbol_library_manager", lambda: _Boom())

        thread = start_background_symbol_warm()
        assert thread is not None
        thread.join(timeout=5)
        assert done.is_set()
        assert not thread.is_alive()  # swallowed, thread exits cleanly


# ---------------------------------------------------------------------------
# warm marks cache state so a subsequent search is a cache hit
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWarmMarksCache:
    def test_warm_populates_cache_and_marks_dirty(self, tmp_path):
        lib_a = _write_lib(tmp_path / "LibA.kicad_sym", "R", "R", "Resistor")
        lib_b = _write_lib(tmp_path / "LibB.kicad_sym", "LED", "D", "Light emitting diode")
        mgr = _bare_manager({"LibA": str(lib_a), "LibB": str(lib_b)})

        parse_calls = {"count": 0}
        orig = SymbolLibraryManager._parse_kicad_sym_file

        def _spy(self, path, name):
            parse_calls["count"] += 1
            return orig(self, path, name)

        mgr._parse_kicad_sym_file = _spy.__get__(mgr, SymbolLibraryManager)

        mgr._warm_cache()
        assert parse_calls["count"] == 2, "warm parses each library once"
        assert set(mgr.symbol_cache) == {"LibA", "LibB"}
        assert mgr._cache_dirty is True, "warm must mark the cache dirty so it persists"

        # A subsequent search must be served entirely from cache (no re-parse).
        parse_calls["count"] = 0
        results = mgr.search_symbols("LED")
        assert [s.full_ref for s in results] == ["LibB:LED"]
        assert parse_calls["count"] == 0, "post-warm search must be a pure cache hit"


# ---------------------------------------------------------------------------
# (c) ready-line ordering: the kickoff sits after the ready print in main()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReadyHandshakeOrdering:
    def test_bg_warm_kickoff_is_after_ready_print(self):
        import inspect

        import kicad_interface

        src = inspect.getsource(kicad_interface.main)
        ready_idx = src.index('{"type": "ready"}')
        warm_idx = src.index("start_background_symbol_warm")
        stdin_idx = src.index("for line in sys.stdin")

        assert ready_idx < warm_idx, "warm must start only after the ready handshake"
        assert warm_idx < stdin_idx, "warm must start before the stdin loop begins"


# ---------------------------------------------------------------------------
# per-library locking: concurrent warm + search parse each library once
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConcurrentWarmAndSearch:
    def test_each_library_parsed_at_most_once(self, tmp_path):
        libs = {}
        for i in range(6):
            f = _write_lib(tmp_path / f"Lib{i}.kicad_sym", f"S{i}", "U", f"desc {i}")
            libs[f"Lib{i}"] = str(f)
        mgr = _bare_manager(libs)

        parse_counts: dict = {}
        lock = threading.Lock()
        orig = SymbolLibraryManager._parse_kicad_sym_file

        def _spy(self, path, name):
            with lock:
                parse_counts[name] = parse_counts.get(name, 0) + 1
            time.sleep(0.005)  # widen the race window
            return orig(self, path, name)

        mgr._parse_kicad_sym_file = _spy.__get__(mgr, SymbolLibraryManager)

        errors = []

        def _warm():
            try:
                mgr._warm_cache()
            except Exception as e:  # pragma: no cover
                errors.append(e)

        def _search():
            try:
                for nick in list(mgr.libraries):
                    mgr.list_symbols(nick)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=_warm)] + [
            threading.Thread(target=_search) for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"no thread may raise: {errors}"
        assert set(parse_counts) == set(mgr.libraries)
        assert all(
            c == 1 for c in parse_counts.values()
        ), f"each library must parse exactly once under concurrency: {parse_counts}"
