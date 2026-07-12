"""F5 regression: overview summary counts must be FULL totals, not page caps.

``get_pcb_overview`` (and ``get_schematic_overview``) call paginated query
handlers that spread ``utils.pagination`` metadata (``total`` = full count,
``count`` = this page).  The summary's ``_count`` helper read ``count``, so
``track_count`` reported the 100-item page cap while the board had 226
segments.  These tests pin that summary counts come from ``total`` and that a
``via_count`` is surfaced.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from handlers.overview import _count, _via_count, handle_get_pcb_overview  # noqa: E402


def _paginated(items_key, page_len, total, **extra):
    """A query-handler-shaped result: a page slice + pagination metadata."""
    result = {
        "success": True,
        items_key: [{"i": n} for n in range(page_len)],
        "total": total,
        "count": page_len,  # page length — the value that used to leak out
        "offset": 0,
        "limit": page_len,
        "truncated": page_len < total,
    }
    result.update(extra)
    return result


# ---------------------------------------------------------------------------
# _count helper
# ---------------------------------------------------------------------------
def test_count_prefers_total_over_page_count():
    result = _paginated("traces", page_len=100, total=226)
    assert _count(result, "traces") == 226  # not 100


def test_count_falls_back_to_explicit_count_when_no_total():
    result = {"success": True, "count": 42}
    assert _count(result, "zones") == 42


def test_count_falls_back_to_array_length_when_no_total_or_count():
    result = {"success": True, "zones": [1, 2, 3]}
    assert _count(result, "zones") == 3


def test_count_zero_on_failed_slice():
    assert _count({"success": False, "total": 999}, "traces") == 0


def test_via_count_reads_full_viacount_field():
    result = {"success": True, "viaCount": 36, "vias": [{"x": 1}]}
    assert _via_count(result) == 36


def test_via_count_falls_back_to_list_len():
    assert _via_count({"success": True, "vias": [1, 2]}) == 2


def test_via_count_zero_on_failure():
    assert _via_count({"success": False}) == 0


# ---------------------------------------------------------------------------
# handle_get_pcb_overview end to end
# ---------------------------------------------------------------------------
def _make_iface():
    tracks_result = _paginated("traces", page_len=100, total=226, traceCount=226, viaCount=36)
    calls = {}

    def query_traces(params):
        calls["query_traces"] = dict(params)
        return tracks_result

    rc = SimpleNamespace(
        query_traces=query_traces,
        query_zones=lambda p: {"success": True, "zoneCount": 2, "zones": [{"z": 1}, {"z": 2}]},
        get_nets_list=lambda p: _paginated("nets", page_len=100, total=120),
    )
    cc = SimpleNamespace(get_component_list=lambda p: _paginated("components", 100, 359))
    bc = SimpleNamespace(get_board_info=lambda p: {"success": True, "layers": ["F.Cu", "B.Cu"]})

    iface = SimpleNamespace(routing_commands=rc, component_commands=cc, board_commands=bc)
    return iface, calls


def test_pcb_overview_summary_uses_full_totals():
    iface, calls = _make_iface()

    out = handle_get_pcb_overview(iface, {})

    summary = out["summary"]
    assert summary["component_count"] == 359
    assert summary["track_count"] == 226  # was 100 (the page cap) before F5
    assert summary["via_count"] == 36
    assert summary["zone_count"] == 2
    assert summary["net_count"] == 120
    assert summary["failed_slices"] == []
    # The tracks slice was requested with includeVias so via_count is available.
    assert calls["query_traces"].get("includeVias") is True


def test_pcb_overview_marks_failed_slice():
    iface, _ = _make_iface()
    iface.routing_commands.query_zones = lambda p: {"success": False, "message": "boom"}

    out = handle_get_pcb_overview(iface, {})

    assert out["success"] is False
    assert "zones" in out["summary"]["failed_slices"]
    # A failed slice counts as 0, the rest still report real totals.
    assert out["summary"]["zone_count"] == 0
    assert out["summary"]["track_count"] == 226
