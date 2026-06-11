"""Tests for IPC query_zones output formatting.

Regression context: ``str(zone.id)`` stringified the KIID proto message into
``value: "f7557a52-..."\\n`` instead of the bare uuid, and ``zone.layers``
yielded protobuf enum ints that surfaced as ``"3"`` / ``"34"`` instead of
``F.Cu`` / ``B.Cu`` — neither round-tripped into uuid/layer-keyed tools.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from handlers.ipc_fastpath._zones import (  # noqa: E402
    _normalize_zone_layer,
    _zone_uuid_str,
)


class _ProtoKiid:
    """Mimic the kipy KIID proto: has .value, str() prints the field repr."""

    def __init__(self, value: str):
        self.value = value

    def __str__(self) -> str:
        return f'value: "{self.value}"\n'


def test_uuid_prefers_proto_value_field() -> None:
    zone = MagicMock()
    zone.id = _ProtoKiid("f7557a52-42ec-4806-8cbf-e6394158cc89")
    assert _zone_uuid_str(zone) == "f7557a52-42ec-4806-8cbf-e6394158cc89"


def test_uuid_cleans_repr_when_no_value_attr() -> None:
    class _ReprOnly:
        def __str__(self) -> str:
            return 'value: "abc-123"\n'

    zone = MagicMock()
    zone.id = _ReprOnly()
    assert _zone_uuid_str(zone) == "abc-123"


def test_uuid_empty_when_zone_has_no_id() -> None:
    zone = MagicMock(spec=[])  # no .id attribute
    assert _zone_uuid_str(zone) == ""


def test_layer_resolves_proto_enum_int(monkeypatch) -> None:
    import handlers.ipc_fastpath._zones as zones_mod

    board_layer = MagicMock()
    board_layer.Name.side_effect = {3: "BL_F_Cu", 34: "BL_B_Cu"}.get
    proto_mod = MagicMock(BoardLayer=board_layer)
    monkeypatch.setitem(sys.modules, "kipy.proto.board.board_types_pb2", proto_mod)
    monkeypatch.setitem(sys.modules, "kipy.proto.board", MagicMock())
    monkeypatch.setitem(sys.modules, "kipy.proto", MagicMock())
    monkeypatch.setitem(sys.modules, "kipy", MagicMock())

    assert _normalize_zone_layer(3) == "F.Cu"
    assert _normalize_zone_layer(34) == "B.Cu"


def test_layer_uses_enum_name_attribute() -> None:
    enum_like = MagicMock()
    enum_like.name = "BL_Edge_Cuts"
    assert _normalize_zone_layer(enum_like) == "Edge.Cuts"


def test_layer_none_is_empty() -> None:
    assert _normalize_zone_layer(None) == ""
