"""D2 regression: query_copper pagination must be backend-consistent.

The SWIG ``query_traces`` paginates traces (100/page default) and returns
``total`` / ``count`` / ``offset`` / ``limit`` / ``truncated`` metadata; the IPC
fast-path dumped ALL traces, ignored ``limit``/``offset``, and emitted none of
that metadata — so a caller written against the SWIG paging contract broke on
IPC.  These tests pin the IPC handler to the IDENTICAL pagination shape:

  * honours ``limit`` / ``offset`` (incl. the 100/page default and the
    ``limit<=0`` "uncapped" case);
  * emits the same metadata keys/values as the shared ``paginate`` util;
  * ``traceCount`` is the FULL total (not the page length);
  * vias stay unpaginated — exactly as the SWIG path returns them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from handlers.ipc_fastpath._routing import handle_query_traces  # noqa: E402
from utils.pagination import paginate  # noqa: E402


def _track(i: int, net: str = "/GND") -> Dict[str, Any]:
    return {
        "id": f"t{i}",
        "net": net,
        "netCode": 1,
        "layer": "F.Cu",
        "width": 0.25,
        "start": {"x": float(i), "y": 0.0},
        "end": {"x": float(i) + 1.0, "y": 0.0},
    }


def _via(i: int, net: str = "/GND") -> Dict[str, Any]:
    return {
        "id": f"v{i}",
        "position": {"x": float(i), "y": 1.0},
        "diameter": 0.8,
        "drill": 0.4,
        "net": net,
        "netCode": 1,
    }


class _FakeBoardAPI:
    def __init__(self, tracks: List[Dict[str, Any]], vias: List[Dict[str, Any]] | None = None):
        self._tracks = tracks
        self._vias = vias or []

    def get_tracks(self):
        return self._tracks

    def get_vias(self):
        return self._vias


def _iface(n_tracks: int, n_vias: int = 0):
    tracks = [_track(i) for i in range(n_tracks)]
    vias = [_via(i) for i in range(n_vias)]
    return SimpleNamespace(
        ipc_board_api=_FakeBoardAPI(tracks, vias),
        _normalize_ipc_layer_name=lambda layer: layer or "F.Cu",
    )


_PAGE_KEYS = {"total", "count", "offset", "limit", "truncated"}


@pytest.mark.unit
class TestIpcQueryTracesPagination:
    def test_default_limit_caps_at_100(self):
        result = handle_query_traces(_iface(150), {})
        assert result["success"] is True
        assert _PAGE_KEYS <= set(result)  # all metadata present
        assert result["total"] == 150
        assert result["traceCount"] == 150  # full total, not page length
        assert len(result["traces"]) == 100
        assert result["count"] == 100
        assert result["limit"] == 100
        assert result["offset"] == 0
        assert result["truncated"] is True

    def test_explicit_limit_honoured(self):
        result = handle_query_traces(_iface(408), {"limit": 5})
        assert len(result["traces"]) == 5
        assert result["total"] == 408
        assert result["count"] == 5
        assert result["limit"] == 5
        assert result["truncated"] is True
        # The E2E repro: IPC returned all 408 ignoring limit=5.
        assert len(result["traces"]) != 408

    def test_offset_slices(self):
        result = handle_query_traces(_iface(20), {"limit": 5, "offset": 5})
        ids = [t["uuid"] for t in result["traces"]]
        assert ids == [f"t{i}" for i in range(5, 10)]
        assert result["offset"] == 5
        assert result["truncated"] is True

    def test_limit_zero_is_uncapped(self):
        result = handle_query_traces(_iface(150), {"limit": 0})
        assert len(result["traces"]) == 150
        assert result["limit"] is None
        assert result["truncated"] is False
        assert result["total"] == 150

    def test_last_page_not_truncated(self):
        result = handle_query_traces(_iface(12), {"limit": 5, "offset": 10})
        assert len(result["traces"]) == 2
        assert result["truncated"] is False

    def test_metadata_matches_shared_paginate_util(self):
        # Identical shape to what the SWIG path merges via **page.
        params = {"limit": 7, "offset": 3}
        result = handle_query_traces(_iface(50), params)
        _, expected = paginate(list(range(50)), params)
        for key in _PAGE_KEYS:
            assert result[key] == expected[key], key
        assert result["traceCount"] == expected["total"]

    def test_vias_not_paginated(self):
        # SWIG paginates only traces; vias are returned in full — match that.
        result = handle_query_traces(_iface(0, n_vias=250), {"includeVias": True, "limit": 5})
        assert result["viaCount"] == 250
        assert len(result["vias"]) == 250
