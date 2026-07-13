"""E2E round-6 P9: query_copper must report canonical layer names on IPC.

On the IPC backend kipy 10 hands tracks back with the raw BoardLayer enum int
(3 = F.Cu, 34 = B.Cu); ``get_tracks`` stringified it to "3" / "34", so a
``query_copper(layer="B.Cu")`` filter matched nothing even though B.Cu tracks
existed.  The fix normalizes at the source (``get_tracks``) and hardens the
interface-level ``_normalize_ipc_layer_name`` so name filters work on both
backends.  (Zones already normalized via ``_normalize_zone_layer`` — pinned in
test_ipc_zone_query_format.py.)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


# ---------------------------------------------------------------------------
# _normalize_ipc_layer_name: numeric enum + numeric string + BL_ name
# ---------------------------------------------------------------------------
def test_normalize_layer_handles_numeric_string():
    """The exact leak: get_tracks emitted str(track.layer) -> '3' / '34'."""
    from kicad_interface import KiCADInterface

    assert KiCADInterface._normalize_ipc_layer_name("3") == "F.Cu"
    assert KiCADInterface._normalize_ipc_layer_name("34") == "B.Cu"


def test_normalize_layer_handles_int_and_bl_name():
    from kicad_interface import KiCADInterface

    assert KiCADInterface._normalize_ipc_layer_name(3) == "F.Cu"
    assert KiCADInterface._normalize_ipc_layer_name(34) == "B.Cu"
    assert KiCADInterface._normalize_ipc_layer_name("BL_F_Cu") == "F.Cu"
    # An already-canonical name round-trips unchanged.
    assert KiCADInterface._normalize_ipc_layer_name("B.Cu") == "B.Cu"


# ---------------------------------------------------------------------------
# IPC query_traces: layer name filter works when the API yields numeric layers
# ---------------------------------------------------------------------------
class _NumericLayerBoardAPI:
    """Mimics kipy 10: get_tracks() layers already normalized by the source fix
    won't reach here, so this simulates the RAW state to exercise the handler's
    own normalization defense — layers arrive as numeric strings '3' / '34'."""

    def get_tracks(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "t-fcu",
                "start": {"x": 0, "y": 0},
                "end": {"x": 1, "y": 0},
                "width": 0.25,
                "layer": "3",  # F.Cu as a raw enum int stringified
                "net": "/GND",
                "netCode": 1,
            },
            {
                "id": "t-bcu",
                "start": {"x": 0, "y": 0},
                "end": {"x": 1, "y": 0},
                "width": 0.25,
                "layer": "34",  # B.Cu
                "net": "/GND",
                "netCode": 1,
            },
        ]

    def get_vias(self) -> List[Dict[str, Any]]:
        return []

    def get_nets(self) -> List[Dict[str, Any]]:
        return [{"name": "/GND", "code": 1}]


def _iface():
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    iface.ipc_board_api = _NumericLayerBoardAPI()
    return iface


def test_query_traces_reports_canonical_layer_names():
    from handlers.ipc_fastpath._routing import handle_query_traces

    out = handle_query_traces(_iface(), {})
    layers = {t["layer"] for t in out["traces"]}
    assert layers == {"F.Cu", "B.Cu"}  # never '3' / '34'


def test_query_traces_bcu_filter_matches_bcu_track():
    """The headline bug: filtering by B.Cu silently returned 0."""
    from handlers.ipc_fastpath._routing import handle_query_traces

    out = handle_query_traces(_iface(), {"layer": "B.Cu"})
    assert out["traceCount"] == 1
    assert out["traces"][0]["uuid"] == "t-bcu"
    assert out["traces"][0]["layer"] == "B.Cu"


def test_query_traces_fcu_filter_matches_fcu_track():
    from handlers.ipc_fastpath._routing import handle_query_traces

    out = handle_query_traces(_iface(), {"layer": "F.Cu"})
    assert out["traceCount"] == 1
    assert out["traces"][0]["uuid"] == "t-fcu"


# ---------------------------------------------------------------------------
# Source-level fix: get_tracks() normalizes the layer itself
# ---------------------------------------------------------------------------
def test_get_tracks_normalizes_layer_at_source(monkeypatch):
    """get_tracks must emit 'F.Cu'/'B.Cu', not the raw enum int, so EVERY
    consumer (query_traces AND ipc_get_tracks) gets canonical names."""
    from unittest.mock import MagicMock

    from kicad_api.ipc_backend._board_tracks import _TrackMixin

    class _Host(_TrackMixin):
        def __init__(self, board):
            self._board = board

        def _get_board(self):
            return self._board

    def _track(layer_int, net_name):
        t = MagicMock()
        t.start.x = 0
        t.start.y = 0
        t.end.x = 1000000
        t.end.y = 0
        t.width = 250000
        t.layer = layer_int  # raw enum int, as kipy 10 returns
        net = MagicMock()
        net.name = net_name
        t.net = net
        t.id = "id-" + net_name
        return t

    board = MagicMock()
    board.get_tracks.return_value = [_track(3, "GND"), _track(34, "VCC")]
    host = _Host(board)

    tracks = host.get_tracks()
    assert {t["layer"] for t in tracks} == {"F.Cu", "B.Cu"}
