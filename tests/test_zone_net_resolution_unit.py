"""Unit tests for copper-pour net resolution (finding F3).

``copper_pour(action=add)`` with ``net:"GND"`` on a board whose real net is
the sheet-prefixed ``/GND`` used to silently create a net-code-0 floating
zone — a large electrically-dead plane with zero warning.  These tests pin:

  * the pure ``resolve_net_name`` resolution order and candidate reporting;
  * SWIG ``add_copper_pour`` resolving / refusing / honouring the no-net
    escape hatch, and reporting ``resolvedNet``;
  * ``edit_copper_pour(newNet=...)`` and the ``_find_zones`` net selector
    (delete/edit) understanding the ``/`` prefix;
  * the IPC fast-path handler doing the same resolution before ``add_zone``.

Runs against the stubbed ``pcbnew`` (see tests/conftest.py) — the resolution
logic is pure over net-name lists, so no real KiCAD is required.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.routing._zones import ZoneMixin, resolve_net_name  # noqa: E402

# ---------------------------------------------------------------------------
# resolve_net_name — pure resolution order
# ---------------------------------------------------------------------------

BOARD_NETS = ["", "/GND", "/+3V3", "/+5V", "/USART1_TX", "unconnected-(U1A-PA0-Pad23)"]


def test_exact_match_wins() -> None:
    assert resolve_net_name("/GND", BOARD_NETS) == ("/GND", [])


def test_slash_prefix_match() -> None:
    # The canonical F3 case: bare "GND" → sheet-root "/GND".
    assert resolve_net_name("GND", BOARD_NETS) == ("/GND", [])


def test_case_insensitive_of_slash_prefix() -> None:
    assert resolve_net_name("gnd", BOARD_NETS) == ("/GND", [])


def test_case_insensitive_exact() -> None:
    assert resolve_net_name("/gnd", BOARD_NETS) == ("/GND", [])


def test_last_path_segment_unique() -> None:
    # Deep hierarchical net: only its last segment equals the request.
    nets = ["/Power/GND", "/+3V3"]
    assert resolve_net_name("GND", nets) == ("/Power/GND", [])


def test_slash_request_matches_bare_board_net() -> None:
    # Reverse direction: request carries the slash, board net doesn't.
    assert resolve_net_name("/GND", ["GND", "+3V3"]) == ("GND", [])


def test_ambiguous_last_segment_refuses_with_candidates() -> None:
    nets = ["/PowerA/GND", "/PowerB/GND"]
    resolved, candidates = resolve_net_name("GND", nets)
    assert resolved is None
    assert set(candidates) == {"/PowerA/GND", "/PowerB/GND"}


def test_no_match_lists_substring_candidates() -> None:
    resolved, candidates = resolve_net_name("USART", BOARD_NETS)
    assert resolved is None
    assert "/USART1_TX" in candidates


def test_no_match_no_substring_lists_real_nets_capped() -> None:
    resolved, candidates = resolve_net_name("ZZZ", BOARD_NETS)
    assert resolved is None
    # Real named nets surfaced; empty + unconnected-* filtered out.
    assert "/GND" in candidates
    assert "" not in candidates
    assert all(not c.startswith("unconnected-") for c in candidates)


def test_empty_request_returns_none() -> None:
    assert resolve_net_name("", BOARD_NETS) == (None, [])


def test_candidates_capped() -> None:
    nets = [f"/NET{i}" for i in range(40)]
    _, candidates = resolve_net_name("nope", nets, cap=5)
    assert len(candidates) == 5


# ---------------------------------------------------------------------------
# SWIG add_copper_pour — resolve / refuse / escape hatch
# ---------------------------------------------------------------------------


class _AddHost(ZoneMixin):
    """Minimal ZoneMixin host with a fixed net list and a stub board."""

    def __init__(self, net_names: List[str]) -> None:
        self._net_names = net_names
        self.board = MagicMock(name="board")
        self.board.GetLayerID.side_effect = lambda n: {"F.Cu": 0, "B.Cu": 31}.get(n, -1)
        nm = MagicMock(name="NetsByName")
        nm.has_key.side_effect = lambda n: n in net_names
        self.board.GetNetInfo.return_value.NetsByName.return_value = nm
        self._nets_by_name = nm

    # Bypass the pcbnew GetNetItem enumeration — resolution logic is what we test.
    def _board_net_names(self) -> List[str]:
        return list(self._net_names)


def _square(side: float) -> list:
    return [
        {"x": 0, "y": 0},
        {"x": side, "y": 0},
        {"x": side, "y": side},
        {"x": 0, "y": side},
    ]


def test_add_resolves_bare_to_slash_and_reports_resolved_net() -> None:
    host = _AddHost(["", "/GND", "/+3V3"])
    out = host.add_copper_pour({"layer": "B.Cu", "net": "GND", "outline": _square(50)})

    assert out["success"] is True
    assert out["resolvedNet"] == "/GND"
    assert out["pour"]["net"] == "/GND"
    assert out["pour"]["requestedNet"] == "GND"
    assert out["pour"]["resolvedNet"] == "/GND"
    assert "warning" in out
    # The zone was attached to the *resolved* net.
    host._nets_by_name.has_key.assert_any_call("/GND")


def test_add_exact_net_has_no_resolved_field() -> None:
    host = _AddHost(["", "/GND"])
    out = host.add_copper_pour({"layer": "B.Cu", "net": "/GND", "outline": _square(50)})
    assert out["success"] is True
    assert "resolvedNet" not in out
    assert out["pour"]["net"] == "/GND"
    assert "requestedNet" not in out["pour"]


def test_add_unknown_net_refuses_with_candidates_and_no_zone() -> None:
    host = _AddHost(["", "/GND", "/+3V3", "/+5V"])
    out = host.add_copper_pour({"layer": "B.Cu", "net": "VBUS", "outline": _square(50)})

    assert out["success"] is False
    assert out["requestedNet"] == "VBUS"
    assert "candidates" in out
    assert "net-0" in out["message"]
    # Nothing was added to the board.
    host.board.Add.assert_not_called()


def test_add_allow_unconnected_creates_no_net_zone() -> None:
    host = _AddHost(["", "/GND"])
    out = host.add_copper_pour({"layer": "B.Cu", "allowUnconnected": True, "outline": _square(50)})
    assert out["success"] is True
    assert out["pour"]["unconnected"] is True
    assert out["pour"]["net"] == ""
    host.board.Add.assert_called_once()


def test_add_empty_net_string_is_escape_hatch() -> None:
    host = _AddHost(["", "/GND"])
    out = host.add_copper_pour({"layer": "B.Cu", "net": "", "outline": _square(50)})
    assert out["success"] is True
    assert out["pour"]["unconnected"] is True


def test_add_missing_net_without_flag_refuses() -> None:
    host = _AddHost(["", "/GND"])
    out = host.add_copper_pour({"layer": "B.Cu", "outline": _square(50)})
    assert out["success"] is False
    assert "net" in out["message"].lower()
    host.board.Add.assert_not_called()


# ---------------------------------------------------------------------------
# _find_zones net selector + edit newNet (delete/edit consistency)
# ---------------------------------------------------------------------------


def _stub_zone(uuid: str, net: str, layer_id: int) -> MagicMock:
    zone = MagicMock(name=f"zone_{uuid}")
    zone.m_Uuid.AsString.return_value = uuid
    zone.GetNetname.return_value = net
    zone.GetLayer.return_value = layer_id
    zone.IsFilled.return_value = True
    return zone


class _ZoneHost(ZoneMixin):
    def __init__(self, zones: List[MagicMock], net_names: List[str]) -> None:
        self._net_names = net_names
        self.board = MagicMock(name="board")
        self.board.Zones.return_value = zones
        self.board.GetLayerID.side_effect = lambda n: {"F.Cu": 0, "B.Cu": 31}.get(n, -1)
        self.board.GetLayerName.side_effect = lambda lid: {0: "F.Cu", 31: "B.Cu"}.get(lid, "?")
        nm = MagicMock(name="NetsByName")
        nm.has_key.side_effect = lambda n: n in net_names
        self.board.GetNetInfo.return_value.NetsByName.return_value = nm

    def _board_net_names(self) -> List[str]:
        return list(self._net_names)


def test_delete_net_selector_understands_slash_prefix() -> None:
    z = _stub_zone("u1", "/GND", 31)
    host = _ZoneHost([z], ["", "/GND"])
    # Caller passes bare "GND"; the zone is on "/GND".
    out = host.delete_copper_pour({"net": "GND"})
    assert out["success"] is True
    host.board.Remove.assert_called_once_with(z)


def test_edit_new_net_resolves_and_reports() -> None:
    z = _stub_zone("u1", "/GND", 31)
    host = _ZoneHost([z], ["", "/GND", "/+3V3"])
    out = host.edit_copper_pour({"uuid": "u1", "newNet": "+3V3"})
    assert out["success"] is True
    assert "net" in out["changed"]
    assert out["resolvedNet"] == "/+3V3"


def test_edit_new_net_unknown_refuses_with_candidates() -> None:
    z = _stub_zone("u1", "/GND", 31)
    host = _ZoneHost([z], ["", "/GND"])
    out = host.edit_copper_pour({"uuid": "u1", "newNet": "NOPE"})
    assert out["success"] is False
    assert out["requestedNet"] == "NOPE"
    assert "candidates" in out


# ---------------------------------------------------------------------------
# IPC fast-path add_copper_pour resolution
# ---------------------------------------------------------------------------


def _ipc_iface(net_names: List[str]):
    from kicad_interface import KiCADInterface

    obj = KiCADInterface.__new__(KiCADInterface)
    obj.use_ipc = True
    board_api = MagicMock()
    board_api.add_zone = MagicMock(return_value=True)
    board_api.get_nets = MagicMock(
        return_value=[{"name": n, "code": i} for i, n in enumerate(net_names)]
    )
    obj.ipc_board_api = board_api
    return obj


def test_ipc_add_resolves_bare_to_slash() -> None:
    from handlers.ipc_fastpath import handle_add_copper_pour

    iface = _ipc_iface(["/GND", "/+3V3"])
    out = handle_add_copper_pour(iface, {"layer": "B.Cu", "net": "GND", "outline": _square(50)})

    assert out["success"] is True
    assert out["resolvedNet"] == "/GND"
    call = iface.ipc_board_api.add_zone.call_args
    assert call.kwargs["net_name"] == "/GND"
    assert out["pour"]["resolvedNet"] == "/GND"


def test_ipc_add_unknown_net_refuses_without_calling_add_zone() -> None:
    from handlers.ipc_fastpath import handle_add_copper_pour

    iface = _ipc_iface(["/GND", "/+3V3"])
    out = handle_add_copper_pour(iface, {"layer": "B.Cu", "net": "VBUS", "outline": _square(50)})

    assert out["success"] is False
    assert "candidates" in out
    iface.ipc_board_api.add_zone.assert_not_called()


def test_ipc_add_allow_unconnected_passes_none_net() -> None:
    from handlers.ipc_fastpath import handle_add_copper_pour

    iface = _ipc_iface(["/GND"])
    out = handle_add_copper_pour(
        iface, {"layer": "B.Cu", "allowUnconnected": True, "outline": _square(50)}
    )
    assert out["success"] is True
    call = iface.ipc_board_api.add_zone.call_args
    assert call.kwargs["net_name"] is None
    assert out["pour"]["unconnected"] is True


def test_ipc_add_passes_through_when_nets_not_enumerable() -> None:
    """Bare mock get_nets isn't iterable → resolution is skipped and the net
    is passed through (keeps the existing IPC handler tests green)."""
    from handlers.ipc_fastpath import handle_add_copper_pour
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    iface.ipc_board_api = MagicMock()
    iface.ipc_board_api.add_zone = MagicMock(return_value=True)
    # get_nets returns a bare MagicMock (not iterable) — enumeration fails.

    out = handle_add_copper_pour(iface, {"layer": "B.Cu", "net": "GND", "outline": _square(50)})
    assert out["success"] is True
    call = iface.ipc_board_api.add_zone.call_args
    assert call.kwargs["net_name"] == "GND"
