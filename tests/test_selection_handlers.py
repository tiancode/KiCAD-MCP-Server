"""Tests for the selection / hit-test / interactive-move handlers.

These exercise python/handlers/selection.py via the KiCADInterface
dispatch trampoline (``iface._handle_<cmd>``), with a fake
``ipc_board_api`` standing in for kipy.  No real KiCAD install needed.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _make_iface(ipc_board_api=None, use_ipc=True):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = use_ipc
    iface.ipc_backend = MagicMock() if use_ipc else None
    iface.ipc_board_api = ipc_board_api
    iface.board = None
    iface.command_routes = {}
    iface._board_disk_signature = None
    iface._current_project_path = None
    iface._last_auto_save_status = None
    return iface


class _FakeAPI:
    """Records calls so tests can assert on what made it through."""

    def __init__(self):
        self.calls = []

    def get_selection(self):
        self.calls.append(("get_selection",))
        return [{"id": "kiid-1", "type": "Footprint", "reference": "R1"}]

    def clear_selection(self):
        self.calls.append(("clear_selection",))
        return True

    def add_to_selection(self, ids):
        self.calls.append(("add_to_selection", list(ids)))
        return {"success": True, "resolved": len(ids), "selection": []}

    def remove_from_selection(self, ids):
        self.calls.append(("remove_from_selection", list(ids)))
        return {"success": True, "resolved": len(ids), "selection": []}

    def hit_test(self, *, x, y, item_id, tolerance, unit):
        self.calls.append(("hit_test", x, y, item_id, tolerance, unit))
        return {"success": True, "hit": True, "items": [{"id": "kiid-hit"}]}

    def interactive_move(self, ids):
        self.calls.append(("interactive_move", list(ids)))
        return {"success": True, "resolved": len(ids)}

    # Used by _resolve_ids() when 'references' is passed.
    def _get_board(self):
        return _FakeBoard()


class _FakeBoard:
    def get_footprints(self):
        return [
            SimpleNamespace(
                id="kiid-r1",
                reference_field=SimpleNamespace(text=SimpleNamespace(value="R1")),
            ),
            SimpleNamespace(
                id="kiid-c1",
                reference_field=SimpleNamespace(text=SimpleNamespace(value="C1")),
            ),
            SimpleNamespace(
                id="kiid-u1",
                reference_field=SimpleNamespace(text=SimpleNamespace(value="U1")),
            ),
        ]


def test_get_selection_passes_through():
    api = _FakeAPI()
    iface = _make_iface(api)
    out = iface._handle_get_selection({})
    assert out["success"] is True
    assert out["count"] == 1
    assert out["items"][0]["reference"] == "R1"
    assert api.calls == [("get_selection",)]


def test_clear_selection_passes_through():
    api = _FakeAPI()
    iface = _make_iface(api)
    out = iface._handle_clear_selection({})
    assert out["success"] is True
    assert ("clear_selection",) in api.calls


def test_add_to_selection_accepts_ids():
    api = _FakeAPI()
    iface = _make_iface(api)
    out = iface._handle_add_to_selection({"ids": ["kiid-a", "kiid-b"]})
    assert out["success"] is True
    assert ("add_to_selection", ["kiid-a", "kiid-b"]) in api.calls


def test_add_to_selection_resolves_references():
    api = _FakeAPI()
    iface = _make_iface(api)
    out = iface._handle_add_to_selection({"references": ["R1", "U1"]})
    assert out["success"] is True
    # references resolved against the fake footprint list
    last_call = api.calls[-1]
    assert last_call[0] == "add_to_selection"
    assert set(last_call[1]) == {"kiid-r1", "kiid-u1"}


def test_add_to_selection_mixes_ids_and_references_dedupes():
    api = _FakeAPI()
    iface = _make_iface(api)
    out = iface._handle_add_to_selection(
        {"ids": ["kiid-r1", "kiid-other"], "references": ["R1", "C1"]}
    )
    assert out["success"] is True
    last_call = api.calls[-1]
    # kiid-r1 appears in both inputs but only once in the resolved list,
    # and order should be ids-first.
    assert last_call[1] == ["kiid-r1", "kiid-other", "kiid-c1"]


def test_add_to_selection_empty_input_fails_cleanly():
    api = _FakeAPI()
    iface = _make_iface(api)
    out = iface._handle_add_to_selection({})
    assert out["success"] is False
    assert "ids" in out["message"] or "references" in out["message"]
    # Backend was never called.
    assert not any(c[0] == "add_to_selection" for c in api.calls)


def test_hit_test_with_position_object():
    api = _FakeAPI()
    iface = _make_iface(api)
    out = iface._handle_hit_test({"position": {"x": 12, "y": 8, "unit": "mm"}})
    assert out["success"] is True
    # The forwarded call should carry x=12, y=8, unit=mm, no item_id.
    last_call = next(c for c in api.calls if c[0] == "hit_test")
    assert last_call[1] == 12.0 and last_call[2] == 8.0
    assert last_call[3] is None  # item_id
    assert last_call[5] == "mm"


def test_hit_test_with_flat_xy_and_tolerance():
    api = _FakeAPI()
    iface = _make_iface(api)
    out = iface._handle_hit_test({"x": 1, "y": 2, "tolerance": 0.5})
    assert out["success"] is True
    last_call = next(c for c in api.calls if c[0] == "hit_test")
    assert (last_call[1], last_call[2]) == (1.0, 2.0)
    assert last_call[4] == 0.5  # tolerance


def test_hit_test_narrows_by_reference():
    api = _FakeAPI()
    iface = _make_iface(api)
    iface._handle_hit_test({"position": {"x": 0, "y": 0}, "reference": "U1"})
    last_call = next(c for c in api.calls if c[0] == "hit_test")
    # reference U1 resolves to kiid-u1
    assert last_call[3] == "kiid-u1"


def test_interactive_move_resolves_and_forwards():
    api = _FakeAPI()
    iface = _make_iface(api)
    out = iface._handle_interactive_move({"references": ["C1"]})
    assert out["success"] is True
    last_call = next(c for c in api.calls if c[0] == "interactive_move")
    assert last_call[1] == ["kiid-c1"]


def test_handlers_fail_cleanly_without_ipc():
    iface = _make_iface(ipc_board_api=None, use_ipc=False)
    for cmd in (
        "get_selection",
        "clear_selection",
        "add_to_selection",
        "remove_from_selection",
        "hit_test",
        "interactive_move",
    ):
        out = getattr(iface, f"_handle_{cmd}")({})
        assert out["success"] is False
        assert "IPC" in out["message"]
