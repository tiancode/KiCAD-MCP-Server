"""Tests for list_shapes / delete_shape / edit_shape (IPC _ShapeMixin).

These close the gap where graphic shapes added by add_segment / add_circle /
... could not be enumerated, deleted, or edited through the MCP at all — the
only way out was hand-editing the .kicad_pcb (which the cross-backend state
machine then had to special-case).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from kicad_api.ipc_backend._board_core import IPCBoardAPI  # noqa: E402


class _Kiid:
    def __init__(self, value: str):
        self.value = value


def _stub_shape(uuid: str, type_name: str, layer: int) -> MagicMock:
    shape = MagicMock(name=f"shape_{uuid}")
    shape.__class__ = type(type_name, (), {})  # type: ignore[assignment]
    shape.id = _Kiid(uuid)
    shape.layer = layer
    return shape


def _api_with_shapes(shapes: List[Any]) -> IPCBoardAPI:
    api = IPCBoardAPI.__new__(IPCBoardAPI)
    api._kicad = MagicMock()
    api._notify = MagicMock()
    api._current_commit = None
    api._current_commit_description = None
    board = MagicMock(name="board")
    board.get_shapes.return_value = shapes
    board.get_item_bounding_box.side_effect = Exception("no bbox in stub")
    api._board = board
    return api


def test_list_shapes_reports_kind_and_clean_ids() -> None:
    seg = _stub_shape("aaa-111", "BoardSegment", 3)
    circ = _stub_shape("bbb-222", "BoardCircle", 3)
    api = _api_with_shapes([seg, circ])

    result = api.list_shapes()

    assert result["success"] is True
    assert result["shapeCount"] == 2
    kinds = {s["id"]: s["kind"] for s in result["shapes"]}
    assert kinds == {"aaa-111": "segment", "bbb-222": "circle"}


def test_delete_by_id_removes_only_that_shape() -> None:
    seg = _stub_shape("aaa-111", "BoardSegment", 3)
    circ = _stub_shape("bbb-222", "BoardCircle", 3)
    api = _api_with_shapes([seg, circ])

    result = api.delete_shapes(ids=["aaa-111"])

    assert result["success"] is True
    api._board.remove_items.assert_called_once_with([seg])


def test_delete_filter_multi_match_refused_without_all() -> None:
    shapes = [
        _stub_shape("aaa-111", "BoardSegment", 3),
        _stub_shape("bbb-222", "BoardCircle", 3),
    ]
    api = _api_with_shapes(shapes)

    with patch.object(IPCBoardAPI, "_layer_to_enum", return_value=3):
        result = api.delete_shapes(layer="F.SilkS")

    assert result["success"] is False
    assert api._board.remove_items.call_count == 0
    assert len(result["shapes"]) == 2


def test_delete_filter_all_removes_every_match() -> None:
    shapes = [
        _stub_shape("aaa-111", "BoardSegment", 3),
        _stub_shape("bbb-222", "BoardCircle", 3),
    ]
    api = _api_with_shapes(shapes)

    with patch.object(IPCBoardAPI, "_layer_to_enum", return_value=3):
        result = api.delete_shapes(layer="F.SilkS", delete_all=True)

    assert result["success"] is True
    assert len(result["deleted"]) == 2
    api._board.remove_items.assert_called_once()


def test_delete_respects_open_transaction() -> None:
    seg = _stub_shape("aaa-111", "BoardSegment", 3)
    api = _api_with_shapes([seg])
    api._current_commit = object()  # transaction open

    result = api.delete_shapes(ids=["aaa-111"])

    assert result["success"] is True
    # _apply_remove must NOT open its own commit while one is open
    api._board.begin_commit.assert_not_called()
    api._board.remove_items.assert_called_once_with([seg])


def test_edit_shape_width_and_layer(monkeypatch: Any) -> None:
    seg = _stub_shape("aaa-111", "BoardSegment", 3)
    api = _api_with_shapes([seg])

    fake_units = MagicMock()
    fake_units.from_mm.side_effect = lambda v: int(v * 1e6)
    fake_units.to_mm.side_effect = lambda v: v / 1e6
    monkeypatch.setitem(sys.modules, "kipy.util.units", fake_units)
    monkeypatch.setitem(sys.modules, "kipy.util", MagicMock(units=fake_units))
    monkeypatch.setitem(sys.modules, "kipy.geometry", MagicMock())
    monkeypatch.setitem(sys.modules, "kipy", MagicMock())

    with patch.object(IPCBoardAPI, "_layer_to_enum", return_value=31):
        result = api.edit_shape("aaa-111", new_layer="B.SilkS", width=0.3)

    assert result["success"] is True
    assert set(result["changed"]) == {"layer", "width"}
    assert seg.layer == 31
    api._board.update_items.assert_called_once()


def test_edit_shape_unknown_id() -> None:
    api = _api_with_shapes([])

    result = api.edit_shape("nope", width=0.3)

    assert result["success"] is False
    assert "list_shapes" in result["message"]
