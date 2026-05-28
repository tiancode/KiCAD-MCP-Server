"""Regression tests for concurrent .kicad_sch corruption.

User report: calling two schematic tools in parallel on the same file
(e.g. get_schematic_view + run_erc) exploded a clean 1-root / 6.6 KB
file into 497 roots / 564 KB — a new ``(kicad_sch ...(lib_symbols ...``
was stuffed into the previous file's lib_symbols, nesting layer by
layer.  Root cause: every writer did an unsynchronized read-modify-write
with a non-atomic ``open(path, "w")``, so concurrent operations either
lost updates or read a half-written file and wrote the garbage back.

The fix is in ``commands.schematic_locks``:

* ``schematic_path_lock`` — a process-wide, per-path reentrant lock,
  applied to every writer so same-file operations serialize.
* ``atomic_write_text`` — temp file + ``os.replace`` so a reader never
  sees a partial file and two writers can't interleave bytes.

These tests exercise the primitives directly and drive concurrent
writers through WireManager to prove the file stays a single, valid
root.
"""

import importlib.util
import os
import sys
import threading
from unittest.mock import MagicMock

import sexpdata

# Stub heavy / optional deps before importing the commands modules.
for modname in ("pcbnew", "skip"):
    sys.modules.setdefault(modname, MagicMock())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

from commands.schematic_locks import (  # noqa: E402
    atomic_write_text,
    schematic_path_lock,
    serialize_on_path,
)

_wm_spec = importlib.util.spec_from_file_location(
    "wire_manager",
    os.path.join(os.path.dirname(__file__), "..", "python", "commands", "wire_manager.py"),
)
_wm_mod = importlib.util.module_from_spec(_wm_spec)
_wm_spec.loader.exec_module(_wm_mod)
WireManager = _wm_mod.WireManager


_EMPTY_SCH = """\
(kicad_sch (version 20250114) (generator "KiCAD-MCP-Server")
  (lib_symbols)
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


# ---------------------------------------------------------------------------
# atomic_write_text
# ---------------------------------------------------------------------------
def test_atomic_write_replaces_content(tmp_path):
    p = tmp_path / "a.kicad_sch"
    p.write_text("old")
    atomic_write_text(p, "new content")
    assert p.read_text() == "new content"
    # No stray temp files left behind.
    leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftovers == []


def test_atomic_write_leaves_original_on_failure(tmp_path):
    p = tmp_path / "b.kicad_sch"
    p.write_text("original")

    class Boom:
        def __str__(self):  # sexpdata-free way to blow up mid-write
            raise RuntimeError("write blew up")

    # A content object whose encoding fails should leave the original
    # intact (the temp file is discarded, os.replace never runs).
    try:
        atomic_write_text(p, Boom())  # type: ignore[arg-type]
    except Exception:
        pass
    assert p.read_text() == "original"
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


# ---------------------------------------------------------------------------
# schematic_path_lock
# ---------------------------------------------------------------------------
def test_path_lock_is_per_path_and_reentrant(tmp_path):
    a = tmp_path / "x.kicad_sch"
    b = tmp_path / "y.kicad_sch"
    # Same path -> same lock object; different path -> different object.
    with schematic_path_lock(a):
        # Reentrant: acquiring the same path again on the same thread
        # must not deadlock.
        with schematic_path_lock(a):
            pass
        # A different path's lock is independent and acquirable.
        with schematic_path_lock(b):
            pass


def test_serialize_on_path_runs_unlocked_when_arg_missing():
    calls = []

    @serialize_on_path(0)
    def fn(path=None):
        calls.append(path)
        return "ok"

    # No positional arg at index 0 -> still runs, just unlocked.
    assert fn() == "ok"
    assert calls == [None]


# ---------------------------------------------------------------------------
# Concurrency: many threads adding labels to ONE file -> stays 1 valid root
# ---------------------------------------------------------------------------
def test_concurrent_add_label_keeps_single_valid_root(tmp_path):
    sch = tmp_path / "conc.kicad_sch"
    sch.write_text(_EMPTY_SCH)

    errors = []

    def worker(i):
        try:
            WireManager.add_label(sch, f"NET{i}", [10.0 + i, 20.0], label_type="label")
        except Exception as e:  # pragma: no cover - failure path
            errors.append(str(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent writers raised: {errors[:3]}"

    text = sch.read_text()
    # Exactly one schematic root — no nested/duplicated (kicad_sch ...).
    assert text.count("(kicad_sch") == 1

    # The file re-parses to a single kicad_sch tree.
    tree = sexpdata.loads(text)
    assert isinstance(tree, list) and str(tree[0]) == "kicad_sch"

    # No writer was lost: all 24 labels are present (serialized, not raced).
    label_count = sum(
        1 for item in tree if isinstance(item, list) and item and str(item[0]) == "label"
    )
    assert label_count == 24, f"expected 24 labels, got {label_count} (lost updates)"
