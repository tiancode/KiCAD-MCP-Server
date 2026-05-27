"""Tests for the board metadata handlers (origins + title block).

Exercises python/handlers/board_meta.py through the KiCADInterface
dispatch trampoline with a fake ipc_board_api.  No real KiCAD required.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _make_iface(api=None, use_ipc=True):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = use_ipc
    iface.ipc_backend = MagicMock() if use_ipc else None
    iface.ipc_board_api = api
    iface.board = None
    iface.command_routes = {}
    iface._board_disk_signature = None
    iface._current_project_path = None
    iface._last_auto_save_status = None
    return iface


class _RecordingAPI:
    def __init__(self):
        self.calls = []

    def get_origin(self, *, origin_type, unit):
        self.calls.append(("get_origin", origin_type, unit))
        return {"success": True, "type": origin_type, "x": 1.0, "y": 2.0, "unit": unit}

    def set_origin(self, *, origin_type, x, y, unit):
        self.calls.append(("set_origin", origin_type, x, y, unit))
        return {"success": True, "type": origin_type, "x": x, "y": y, "unit": unit}

    def get_title_block_info(self):
        self.calls.append(("get_title_block_info",))
        return {
            "success": True,
            "title": "T",
            "date": "D",
            "revision": "A",
            "company": "C",
            "comments": {"1": "old1", "2": "", "3": "", "4": "", "5": "old5",
                         "6": "", "7": "", "8": "", "9": ""},
        }

    def set_title_block_info(self, *, title, date, revision, company, comments):
        self.calls.append(
            ("set_title_block_info", title, date, revision, company, comments)
        )
        return {"success": True}


# ---------------------------------------------------------------- get_origin
def test_get_origin_defaults_to_drill_mm():
    api = _RecordingAPI()
    iface = _make_iface(api)
    out = iface._handle_get_origin({})
    assert out["success"] is True
    assert api.calls == [("get_origin", "drill", "mm")]


def test_get_origin_passes_explicit_type_and_unit():
    api = _RecordingAPI()
    iface = _make_iface(api)
    iface._handle_get_origin({"type": "grid", "unit": "inch"})
    assert api.calls[-1] == ("get_origin", "grid", "inch")


# ---------------------------------------------------------------- set_origin
def test_set_origin_with_position_object():
    api = _RecordingAPI()
    iface = _make_iface(api)
    out = iface._handle_set_origin(
        {"type": "drill", "position": {"x": 5, "y": 7, "unit": "mm"}}
    )
    assert out["success"] is True
    assert api.calls[-1] == ("set_origin", "drill", 5.0, 7.0, "mm")


def test_set_origin_with_flat_xy():
    api = _RecordingAPI()
    iface = _make_iface(api)
    iface._handle_set_origin({"type": "grid", "x": 1, "y": 2, "unit": "inch"})
    assert api.calls[-1] == ("set_origin", "grid", 1.0, 2.0, "inch")


def test_set_origin_rejects_missing_type():
    api = _RecordingAPI()
    iface = _make_iface(api)
    out = iface._handle_set_origin({"position": {"x": 0, "y": 0}})
    assert out["success"] is False
    assert "type" in out["message"].lower()
    # Backend not called.
    assert not any(c[0] == "set_origin" for c in api.calls)


def test_set_origin_rejects_missing_position():
    """Calling set_origin without coords used to silently move the origin
    to (0, 0). Now it must reject."""
    api = _RecordingAPI()
    iface = _make_iface(api)
    out = iface._handle_set_origin({"type": "drill"})
    assert out["success"] is False
    assert "coord" in out["message"].lower() or "position" in out["message"].lower()
    assert not any(c[0] == "set_origin" for c in api.calls)


def test_set_origin_rejects_partial_flat_xy():
    """Only `x` (no `y`) is a signal of error, not a request to default y=0."""
    api = _RecordingAPI()
    iface = _make_iface(api)
    out = iface._handle_set_origin({"type": "drill", "x": 5})
    assert out["success"] is False
    assert not any(c[0] == "set_origin" for c in api.calls)


def test_set_origin_rejects_position_dict_missing_xy():
    """Empty `{position: {}}` is a missing-coords mistake, not a (0,0) request."""
    api = _RecordingAPI()
    iface = _make_iface(api)
    out = iface._handle_set_origin({"type": "drill", "position": {}})
    assert out["success"] is False
    assert not any(c[0] == "set_origin" for c in api.calls)


def test_set_origin_accepts_explicit_zero():
    """(0, 0) is a perfectly valid origin — must NOT be confused with 'missing'."""
    api = _RecordingAPI()
    iface = _make_iface(api)
    out = iface._handle_set_origin(
        {"type": "drill", "position": {"x": 0, "y": 0}}
    )
    assert out["success"] is True
    assert api.calls[-1] == ("set_origin", "drill", 0.0, 0.0, "mm")


# ----------------------------------------------------- get_title_block_info
def test_get_title_block_info_passes_through():
    api = _RecordingAPI()
    iface = _make_iface(api)
    out = iface._handle_get_title_block_info({})
    assert out["success"] is True
    assert out["title"] == "T"
    assert out["comments"]["1"] == "old1"


# ----------------------------------------------------- set_title_block_info
def test_set_title_block_info_partial_update_omits_none():
    api = _RecordingAPI()
    iface = _make_iface(api)
    out = iface._handle_set_title_block_info({"title": "New title"})
    assert out["success"] is True
    cmd = api.calls[-1]
    assert cmd[0] == "set_title_block_info"
    # title set, others None (handler does NOT fetch current — the *backend*
    # merges with current state; handler just forwards the partial update).
    assert cmd[1] == "New title"
    assert cmd[2] is None  # date
    assert cmd[3] is None  # revision
    assert cmd[4] is None  # company
    assert cmd[5] is None  # comments (empty dict → handler passes None)


def test_set_title_block_info_comments_dict():
    api = _RecordingAPI()
    iface = _make_iface(api)
    iface._handle_set_title_block_info(
        {"comments": {"1": "hello", "5": "world"}}
    )
    cmd = api.calls[-1]
    assert cmd[5] == {1: "hello", 5: "world"}


def test_set_title_block_info_comments_list_is_positional():
    api = _RecordingAPI()
    iface = _make_iface(api)
    iface._handle_set_title_block_info({"comments": ["a", "b", "c"]})
    cmd = api.calls[-1]
    # index 0 → slot 1, index 1 → slot 2, ...
    assert cmd[5] == {1: "a", 2: "b", 3: "c"}


def test_set_title_block_info_drops_non_integer_dict_keys():
    api = _RecordingAPI()
    iface = _make_iface(api)
    iface._handle_set_title_block_info({"comments": {"1": "ok", "bad": "drop"}})
    cmd = api.calls[-1]
    assert cmd[5] == {1: "ok"}


def test_set_title_block_info_null_comment_means_no_change_not_clear():
    """Null in a comment slot used to clear (collapse to empty string).
    That contradicted the 'explicit empty string clears' contract — JSON
    encoders that emit null for unset fields would silently wipe slots.
    Now null is treated as 'no change'."""
    api = _RecordingAPI()
    iface = _make_iface(api)
    iface._handle_set_title_block_info(
        {"comments": {"1": "kept", "2": None, "5": ""}}
    )
    cmd = api.calls[-1]
    # Slot 1 set to "kept"; slot 2 skipped (None = no change); slot 5
    # explicitly cleared (empty string still means clear).
    assert cmd[5] == {1: "kept", 5: ""}


def test_set_title_block_info_null_in_list_form_skips_slot():
    """Positional list form: `null` at index n leaves slot n+1 alone."""
    api = _RecordingAPI()
    iface = _make_iface(api)
    iface._handle_set_title_block_info({"comments": ["a", None, "c"]})
    cmd = api.calls[-1]
    # Slot 1='a', slot 2 skipped (None), slot 3='c'.
    assert cmd[5] == {1: "a", 3: "c"}


def test_set_title_block_info_explicit_empty_string_passes_through():
    """Clearing a field must be possible — empty string is *not* treated as None."""
    api = _RecordingAPI()
    iface = _make_iface(api)
    iface._handle_set_title_block_info({"title": ""})
    cmd = api.calls[-1]
    assert cmd[1] == ""  # title forwarded as empty string, not dropped


def test_handlers_fail_cleanly_without_ipc():
    iface = _make_iface(api=None, use_ipc=False)
    # ensure_ipc was added so handlers can auto-launch KiCAD; stub it out
    # here because the point of this test is the "no IPC available" message
    # path, not the recovery path. Without the stub the test machine's live
    # KiCAD (or a stale socket) can satisfy ensure_ipc and the handler then
    # returns an IPC-layer error instead of our gating message.
    iface.ensure_ipc = lambda **kw: (False, "ipc disabled in test")
    for cmd in (
        "get_origin",
        "set_origin",
        "get_title_block_info",
        "set_title_block_info",
    ):
        out = getattr(iface, f"_handle_{cmd}")({})
        assert out["success"] is False
        assert "IPC" in out["message"]
