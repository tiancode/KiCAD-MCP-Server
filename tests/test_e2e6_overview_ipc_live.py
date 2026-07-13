"""E2E round-6 P7: get_pcb_overview must read LIVE IPC state, not stale disk.

On IPC, move_component + route_trace succeeded live and
get_component_properties reflected them immediately, but get_pcb_overview kept
returning the stale on-disk SWIG counts until save_project — because the
overview called the SWIG command objects (routing_commands.query_traces …)
directly, bypassing the IPC fast path.  It now routes each slice through the
IPC fast-path handlers when a board document is open over IPC, and falls back
to the SWIG command objects otherwise (preserving the F5 pagination contract).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from handlers.overview import _pcb_reads_should_use_ipc, handle_get_pcb_overview  # noqa: E402


# ---------------------------------------------------------------------------
# _pcb_reads_should_use_ipc gate
# ---------------------------------------------------------------------------
def test_gate_false_for_plain_swig_iface():
    """The F5 SimpleNamespace iface (no use_ipc attr) must take the SWIG path."""
    iface = SimpleNamespace(routing_commands=None, component_commands=None, board_commands=None)
    assert _pcb_reads_should_use_ipc(iface) is False


def test_gate_false_when_no_board_document_open():
    iface = SimpleNamespace(
        use_ipc=True,
        ipc_board_api=object(),
        _ipc_has_open_board_document=lambda: False,
    )
    assert _pcb_reads_should_use_ipc(iface) is False


def test_gate_true_when_board_open_over_ipc():
    iface = SimpleNamespace(
        use_ipc=True,
        ipc_board_api=object(),
        _ipc_has_open_board_document=lambda: True,
    )
    assert _pcb_reads_should_use_ipc(iface) is True


# ---------------------------------------------------------------------------
# Overview reads through the IPC fast path when the board is open over IPC
# ---------------------------------------------------------------------------
def test_overview_uses_ipc_fastpath_when_board_open(monkeypatch):
    """With a board open over IPC, the overview must call the IPC fast-path
    handlers (live state) rather than the SWIG command objects.

    handle_get_pcb_overview imports the fast-path handlers from the
    ``handlers.ipc_fastpath`` package at call time, so we patch them there."""
    import handlers.ipc_fastpath as fp

    called = {"ipc": [], "swig": []}

    def _swig_boom(*a, **k):
        called["swig"].append(True)
        raise AssertionError("SWIG command object must not be used when IPC is live")

    rc = SimpleNamespace(query_traces=_swig_boom, query_zones=_swig_boom, get_nets_list=_swig_boom)
    cc = SimpleNamespace(get_component_list=_swig_boom)
    bc = SimpleNamespace(get_board_info=_swig_boom)

    iface = SimpleNamespace(
        use_ipc=True,
        ipc_board_api=object(),
        _ipc_has_open_board_document=lambda: True,
        routing_commands=rc,
        component_commands=cc,
        board_commands=bc,
    )

    def _fp(name, count_key, n):
        def _h(_iface, _params):
            called["ipc"].append(name)
            return {"success": True, count_key: [{"i": i} for i in range(n)]}

        return _h

    def _tracks(_iface, _params):
        called["ipc"].append("tracks")
        return {"success": True, "traces": [1] * 7, "viaCount": 3, "vias": [1, 1, 1]}

    monkeypatch.setattr(fp, "handle_get_component_list", _fp("components", "components", 42))
    monkeypatch.setattr(fp, "handle_query_traces", _tracks)
    monkeypatch.setattr(fp, "handle_query_zones", _fp("zones", "zones", 2))
    monkeypatch.setattr(fp, "handle_get_nets_list", _fp("nets", "nets", 9))
    monkeypatch.setattr(fp, "handle_get_board_info", lambda i, p: {"success": True, "layers": ["F.Cu"]})

    out = handle_get_pcb_overview(iface, {})

    assert called["swig"] == []  # SWIG path never touched
    assert set(called["ipc"]) == {"components", "tracks", "zones", "nets"}
    summary = out["summary"]
    assert summary["component_count"] == 42
    assert summary["track_count"] == 7
    assert summary["via_count"] == 3
    assert summary["zone_count"] == 2
    assert summary["net_count"] == 9
    assert out["success"] is True


def test_overview_uses_swig_when_no_ipc(monkeypatch):
    """Without an IPC board document, the overview keeps reading the SWIG
    command objects (the F5 path) — the IPC handlers must not be called."""
    import handlers.ipc_fastpath as fp

    def _ipc_boom(*a, **k):
        raise AssertionError("IPC handler must not run without a board over IPC")

    monkeypatch.setattr(fp, "handle_query_traces", _ipc_boom)

    calls = {}

    def _qt(params):
        calls["qt"] = params
        return {"success": True, "traces": [1, 1], "viaCount": 0}

    rc = SimpleNamespace(
        query_traces=_qt,
        query_zones=lambda p: {"success": True, "zones": [1]},
        get_nets_list=lambda p: {"success": True, "nets": [1, 1, 1]},
    )
    cc = SimpleNamespace(get_component_list=lambda p: {"success": True, "components": [1] * 5})
    bc = SimpleNamespace(get_board_info=lambda p: {"success": True, "layers": ["F.Cu"]})

    iface = SimpleNamespace(routing_commands=rc, component_commands=cc, board_commands=bc)

    out = handle_get_pcb_overview(iface, {})

    assert calls["qt"].get("includeVias") is True
    assert out["summary"]["track_count"] == 2
    assert out["summary"]["component_count"] == 5
