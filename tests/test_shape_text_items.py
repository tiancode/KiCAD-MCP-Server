"""Board TEXT items in the shape management tools (papercut 4, GD32 E2E).

``add_board_text`` places a gr_text, but list_shapes / edit_shape /
delete_shape only handled line/arc/circle/rect/polygon — placed text could
not be listed, moved, or removed through the MCP at all.  These tests pin
the extension: kipy BoardText/BoardTextBox items surface as kind "text" /
"textbox" (with text content, position, size), are deletable by id or
filter (multi-match refusal preserved), and are editable (move / text /
size / layer), with inapplicable properties reported as ``unsupported``
instead of silently ignored.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from kicad_api.ipc_backend._board_core import IPCBoardAPI  # noqa: E402

NM = 1_000_000  # 1 mm in nanometers


class _Kiid:
    def __init__(self, value: str):
        self.value = value

    def __str__(self) -> str:  # mimic the proto field repr
        return f'value: "{self.value}"\n'


class _Vec:
    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y


class _TextAttrs:
    def __init__(self, size_nm: int = NM):
        self.size = _Vec(size_nm, size_nm)
        self.stroke_width = 150_000
        self.angle = 0.0


class _FakeVector2:
    """Stand-in for kipy.geometry.Vector2 (only what edit_shape uses)."""

    def __init__(self, x: int = 0, y: int = 0):
        self.x = x
        self.y = y

    @staticmethod
    def from_xy(x: int, y: int) -> "_FakeVector2":
        return _FakeVector2(x, y)


def _make_text(uuid: str, value: str, x_nm: int, y_nm: int, layer: int = 3) -> Any:
    """A BoardText-shaped stub — the class NAME drives kind mapping."""
    cls = type("BoardText", (), {})
    item = cls()
    item.id = _Kiid(uuid)
    item.value = value
    item.position = _Vec(x_nm, y_nm)
    item.layer = layer
    item.attributes = _TextAttrs()
    return item


def _make_textbox(uuid: str, value: str, layer: int = 3) -> Any:
    cls = type("BoardTextBox", (), {})
    item = cls()
    item.id = _Kiid(uuid)
    item.value = value
    item.top_left = _Vec(0, 0)
    item.bottom_right = _Vec(5 * NM, 2 * NM)
    item.layer = layer
    item.attributes = _TextAttrs()
    return item


def _stub_shape(uuid: str, type_name: str, layer: int) -> MagicMock:
    shape = MagicMock(name=f"shape_{uuid}")
    shape.__class__ = type(type_name, (), {})  # type: ignore[assignment]
    shape.id = _Kiid(uuid)
    shape.layer = layer
    return shape


def _api(shapes: List[Any], text_items: List[Any] | None = None) -> IPCBoardAPI:
    api = IPCBoardAPI.__new__(IPCBoardAPI)
    api._kicad = MagicMock()
    api._notify = MagicMock()
    api._current_commit = None
    api._current_commit_description = None
    board = MagicMock(name="board")
    board.get_shapes.return_value = shapes
    if text_items is None:
        board.get_text.side_effect = Exception("no get_text in stub")
    else:
        board.get_text.return_value = text_items
    board.get_item_bounding_box.side_effect = Exception("no bbox in stub")
    api._board = board
    return api


@pytest.fixture(autouse=True)
def _fake_kipy_units(monkeypatch: Any):
    """Deterministic unit conversion + Vector2, independent of a kipy install."""
    fake_units = MagicMock()
    fake_units.from_mm.side_effect = lambda v: int(v * NM)
    fake_units.to_mm.side_effect = lambda v: v / NM
    fake_geometry = MagicMock()
    fake_geometry.Vector2 = _FakeVector2
    monkeypatch.setitem(sys.modules, "kipy", MagicMock())
    monkeypatch.setitem(sys.modules, "kipy.util", MagicMock(units=fake_units))
    monkeypatch.setitem(sys.modules, "kipy.util.units", fake_units)
    monkeypatch.setitem(sys.modules, "kipy.geometry", fake_geometry)


# ---------------------------------------------------------------------------
# list_shapes
# ---------------------------------------------------------------------------
def test_list_includes_text_with_content_position_and_size() -> None:
    text = _make_text("txt-1", "REV A", 10 * NM, 20 * NM)
    api = _api([_stub_shape("seg-1", "BoardSegment", 3)], [text])

    result = api.list_shapes()

    assert result["success"] is True
    assert result["shapeCount"] == 2
    by_id = {s["id"]: s for s in result["shapes"]}
    assert by_id["seg-1"]["kind"] == "segment"
    entry = by_id["txt-1"]
    assert entry["kind"] == "text"
    assert entry["text"] == "REV A"
    assert entry["position"] == {"x": 10.0, "y": 20.0, "unit": "mm"}
    assert entry["size"] == {"width": 1.0, "height": 1.0, "unit": "mm"}


def test_list_kind_filter_separates_text_from_shapes() -> None:
    seg = _stub_shape("seg-1", "BoardSegment", 3)
    text = _make_text("txt-1", "REV A", 0, 0)
    api = _api([seg], [text])

    only_text = api.list_shapes(kind="text")
    assert [s["id"] for s in only_text["shapes"]] == ["txt-1"]

    only_seg = api.list_shapes(kind="segment")
    assert [s["id"] for s in only_seg["shapes"]] == ["seg-1"]


def test_list_reports_textbox_kind() -> None:
    api = _api([], [_make_textbox("tb-1", "note")])

    result = api.list_shapes()

    assert result["shapeCount"] == 1
    assert result["shapes"][0]["kind"] == "textbox"
    assert result["shapes"][0]["text"] == "note"


def test_list_survives_board_without_get_text() -> None:
    """Older kipy without Board.get_text: shapes still list, no crash."""
    api = _api([_stub_shape("seg-1", "BoardSegment", 3)], text_items=None)

    result = api.list_shapes()

    assert result["success"] is True
    assert [s["id"] for s in result["shapes"]] == ["seg-1"]


# ---------------------------------------------------------------------------
# delete_shape
# ---------------------------------------------------------------------------
def test_delete_text_by_id() -> None:
    seg = _stub_shape("seg-1", "BoardSegment", 3)
    text = _make_text("txt-1", "REV A", 0, 0)
    api = _api([seg], [text])

    result = api.delete_shapes(ids=["txt-1"])

    assert result["success"] is True
    api._board.remove_items.assert_called_once_with([text])


def test_delete_mixed_filter_multi_match_refused_without_all() -> None:
    """A layer filter matching one shape AND one text must keep the
    multi-match refusal semantics (candidate list, no deletion)."""
    seg = _stub_shape("seg-1", "BoardSegment", 3)
    text = _make_text("txt-1", "REV A", 0, 0, layer=3)
    api = _api([seg], [text])

    with patch.object(IPCBoardAPI, "_layer_to_enum", return_value=3):
        result = api.delete_shapes(layer="F.SilkS")

    assert result["success"] is False
    assert api._board.remove_items.call_count == 0
    kinds = {s["kind"] for s in result["shapes"]}
    assert kinds == {"segment", "text"}


def test_delete_mixed_filter_all_removes_shape_and_text() -> None:
    seg = _stub_shape("seg-1", "BoardSegment", 3)
    text = _make_text("txt-1", "REV A", 0, 0, layer=3)
    api = _api([seg], [text])

    with patch.object(IPCBoardAPI, "_layer_to_enum", return_value=3):
        result = api.delete_shapes(layer="F.SilkS", delete_all=True)

    assert result["success"] is True
    assert len(result["deleted"]) == 2
    api._board.remove_items.assert_called_once_with([seg, text])


def test_delete_kind_text_filter_only_touches_text() -> None:
    seg = _stub_shape("seg-1", "BoardSegment", 3)
    text = _make_text("txt-1", "REV A", 0, 0, layer=3)
    api = _api([seg], [text])

    result = api.delete_shapes(kind="text")

    assert result["success"] is True
    api._board.remove_items.assert_called_once_with([text])


# ---------------------------------------------------------------------------
# edit_shape
# ---------------------------------------------------------------------------
def test_edit_text_move_translates_position() -> None:
    text = _make_text("txt-1", "REV A", 10 * NM, 20 * NM)
    api = _api([], [text])

    result = api.edit_shape("txt-1", move={"dx": 1.5, "dy": -0.5})

    assert result["success"] is True
    assert result["changed"] == ["move"]
    assert (text.position.x, text.position.y) == (int(11.5 * NM), int(19.5 * NM))
    api._board.update_items.assert_called_once()


def test_edit_text_content_and_size() -> None:
    text = _make_text("txt-1", "REV A", 0, 0)
    api = _api([], [text])

    result = api.edit_shape("txt-1", text="REV B", size=2.0)

    assert result["success"] is True
    assert set(result["changed"]) == {"text", "size"}
    assert text.value == "REV B"
    assert (text.attributes.size.x, text.attributes.size.y) == (2 * NM, 2 * NM)
    assert result["shape"]["text"] == "REV B"


def test_edit_text_width_sets_text_stroke_width() -> None:
    text = _make_text("txt-1", "REV A", 0, 0)
    api = _api([], [text])

    result = api.edit_shape("txt-1", width=0.3)

    assert result["success"] is True
    assert result["changed"] == ["width"]
    assert text.attributes.stroke_width == int(0.3 * NM)


def test_edit_text_filled_is_structured_unsupported() -> None:
    """`filled` has no meaning for text — must come back as a structured
    `unsupported`, never a silent no-op."""
    text = _make_text("txt-1", "REV A", 0, 0)
    api = _api([], [text])

    result = api.edit_shape("txt-1", filled=True)

    assert result["success"] is False
    assert result["unsupported"] == ["filled"]
    api._board.update_items.assert_not_called()


def test_edit_text_mixed_supported_and_unsupported() -> None:
    text = _make_text("txt-1", "REV A", 0, 0)
    api = _api([], [text])

    result = api.edit_shape("txt-1", move={"dx": 1, "dy": 0}, filled=True)

    assert result["success"] is True
    assert result["changed"] == ["move"]
    assert result["unsupported"] == ["filled"]


def test_edit_shape_text_property_unsupported_on_segment(monkeypatch: Any) -> None:
    seg = _stub_shape("seg-1", "BoardSegment", 3)
    api = _api([seg], [])

    result = api.edit_shape("seg-1", text="nope", size=2.0)

    assert result["success"] is False
    assert set(result["unsupported"]) == {"text", "size"}
    api._board.update_items.assert_not_called()


def test_edit_textbox_moves_both_corners() -> None:
    box = _make_textbox("tb-1", "note")
    api = _api([], [box])

    result = api.edit_shape("tb-1", move={"dx": 2, "dy": 3})

    assert result["success"] is True
    assert (box.top_left.x, box.top_left.y) == (2 * NM, 3 * NM)
    assert (box.bottom_right.x, box.bottom_right.y) == (7 * NM, 5 * NM)


# ---------------------------------------------------------------------------
# Handler layer forwards text/size
# ---------------------------------------------------------------------------
def test_handle_edit_shape_forwards_text_and_size(monkeypatch: Any) -> None:
    from kicad_interface import KiCADInterface
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr(KiCADProcessManager, "is_pcb_editor_running", lambda: True)
    monkeypatch.setattr(KiCADInterface, "_ipc_has_open_board_document", lambda self: True)

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    iface.ipc_backend = MagicMock()
    iface.ipc_board_api = MagicMock()
    iface.ipc_board_api.edit_shape.return_value = {"success": True}
    iface.board = None
    iface.command_routes = {}
    iface._board_disk_signature = None
    iface._current_project_path = None
    iface._last_auto_save_status = None

    out = iface._handle_edit_shape({"id": "txt-1", "text": "REV B", "size": 1.2})

    assert out["success"] is True
    iface.ipc_board_api.edit_shape.assert_called_once_with(
        shape_id="txt-1",
        new_layer=None,
        width=None,
        filled=None,
        move=None,
        text="REV B",
        size=1.2,
    )
