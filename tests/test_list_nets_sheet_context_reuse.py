"""Regression test for the list_schematic_nets parse-reuse optimization.

get_connections_for_net used to re-load the sexp and rebuild the O(wires^2)
wire-adjacency graph for *every* net, so listing nets on a large schematic was
O(nets * wires^2) and the schematic overview (which bundles it) timed out.

It now accepts a shared ``sheet_contexts`` cache so each sheet is parsed and
indexed only once across the whole net loop. These tests pin that behaviour:

- with a shared cache, each sheet's context is built once regardless of how
  many nets are queried;
- without a cache (single-net callers), behaviour is unchanged — the context
  is built once per call.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.wire_connectivity import _queries  # noqa: E402
from commands.wire_connectivity import get_connections_for_net  # noqa: E402


def _patch_counters(monkeypatch):
    """Replace the heavy per-sheet work with counters; isolate the cache logic."""
    builds: list = []

    def fake_build(schematic, schematic_path, sexp=None):
        builds.append(schematic_path)
        return {"all_wires": [], "instances": []}  # truthy, non-None context

    monkeypatch.setattr(_queries, "_build_sheet_context", fake_build)
    monkeypatch.setattr(_queries, "_discover_sub_sheets", lambda path: [])
    monkeypatch.setattr(_queries, "_process_single_sheet", lambda *a, **k: [])
    return builds


def test_shared_cache_builds_each_sheet_once_across_nets(monkeypatch):
    builds = _patch_counters(monkeypatch)
    cache: dict = {}

    for net in ("NET1", "NET2", "NET3"):
        get_connections_for_net(object(), "/top.kicad_sch", net, sheet_contexts=cache)

    # Three nets, one sheet → built exactly once (the old code built it 3x).
    assert builds == ["/top.kicad_sch"]


def test_no_cache_builds_once_per_call_unchanged(monkeypatch):
    builds = _patch_counters(monkeypatch)

    get_connections_for_net(object(), "/top.kicad_sch", "NET1")
    get_connections_for_net(object(), "/top.kicad_sch", "NET2")

    # Each standalone call builds its own context — same as before the change.
    assert builds == ["/top.kicad_sch", "/top.kicad_sch"]


def test_unreadable_sheet_context_is_skipped(monkeypatch):
    """A sheet whose context can't be built (None) must not crash the loop."""
    monkeypatch.setattr(_queries, "_build_sheet_context", lambda *a, **k: None)
    monkeypatch.setattr(_queries, "_discover_sub_sheets", lambda path: [])

    called = []
    monkeypatch.setattr(_queries, "_process_single_sheet", lambda *a, **k: called.append(1) or [])

    result = get_connections_for_net(object(), "/bad.kicad_sch", "NET1", sheet_contexts={})

    assert result == []
    # None context → the sheet is skipped, _process_single_sheet never runs.
    assert called == []
