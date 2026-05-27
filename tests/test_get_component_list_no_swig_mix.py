"""Regression tests for ``get_component_list`` / ``get_component_properties``.

Bug history: the IPC handlers patched missing ``boundingBox`` (and
``courtyard``) values from ``iface.board`` — the SWIG in-memory copy that
holds the pre-IPC-mutation positions.  A component that just moved via
``move_component`` (IPC) came back with a fresh ``position`` and a stale
``boundingBox``: same record, two coordinate frames.  These tests lock
in "every field comes from one source — leave it null if IPC can't
compute it".

Also covers the layer-name normalization regression where kipy's
``str(BoardLayer.BL_F_Cu)`` returned ``"3"`` on some versions, leaving
the user with ``layer: "3"`` instead of ``"F.Cu"``.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


@pytest.fixture(autouse=True)
def _open_board(monkeypatch):
    """IPC gates assume a board is loaded; bypass them for these focused tests."""
    from kicad_interface import KiCADInterface

    monkeypatch.setattr(
        KiCADInterface, "_ipc_has_open_board_document", lambda self: True
    )


def _make_iface():
    """Bare KiCADInterface with a fake ipc_board_api and a SWIG board that
    holds INTENTIONALLY-DIFFERENT data so any cross-backend mix shows up
    in the assertions."""
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    iface.ipc_board_api = MagicMock()
    iface.ipc_backend = MagicMock()
    iface.command_routes = {}
    iface._ipc_writes_pending = False
    iface._swig_writes_landed = False
    iface._ipc_change_callback_registered = False
    # SWIG side: an object that, if the handler accidentally reaches into it,
    # would return STALE bbox values — so the test fails loudly instead of
    # silently passing on a coincidentally-matching board.
    iface.board = object()
    iface.component_commands = MagicMock()
    iface.component_commands.get_component_list = MagicMock(
        return_value={
            "success": True,
            "components": [
                {
                    "reference": "R1",
                    "boundingBox": {  # ← pre-move SWIG coords
                        "min_x": 105.4,
                        "min_y": 20.0,
                        "max_x": 115.2,
                        "max_y": 28.0,
                        "unit": "mm",
                    },
                }
            ],
        }
    )
    iface.component_commands.get_component_properties = MagicMock(
        return_value={
            "success": True,
            "component": {
                "reference": "R1",
                "boundingBox": {
                    "min_x": 105.4,
                    "min_y": 20.0,
                    "max_x": 115.2,
                    "max_y": 28.0,
                    "unit": "mm",
                },
                "courtyard": {"min_x": 105, "max_x": 116},
            },
        }
    )
    return iface


# ---------------------------------------------------------------------------
# get_component_list: never patches boundingBox from SWIG
# ---------------------------------------------------------------------------
def test_get_component_list_leaves_missing_bbox_null_when_ipc_cant_compute():
    """The user's failure: position is IPC's fresh value (38, 25) but
    boundingBox was patched from SWIG (105.4–115.2). Handler must NOT
    mix sources — leave boundingBox null instead."""
    from handlers.ipc_fastpath import handle_get_component_list

    iface = _make_iface()
    iface.ipc_board_api.list_components = MagicMock(
        return_value=[
            {
                "reference": "R1",
                "position": {"x": 38, "y": 25, "unit": "mm"},
                "rotation": 0,
                "layer": "F.Cu",
                "boundingBox": None,  # ← IPC couldn't compute
            }
        ]
    )

    out = handle_get_component_list(iface, {})

    assert out["success"] is True
    [comp] = out["components"]
    assert comp["position"] == {"x": 38, "y": 25, "unit": "mm"}
    # The fix: do NOT patch boundingBox from SWIG.
    assert comp["boundingBox"] is None
    iface.component_commands.get_component_list.assert_not_called()


def test_get_component_list_keeps_ipc_bbox_when_available():
    """When IPC computed a fresh boundingBox, it must come through unchanged
    (same coordinate frame as position)."""
    from handlers.ipc_fastpath import handle_get_component_list

    iface = _make_iface()
    fresh_bbox = {
        "min_x": 35.0,
        "min_y": 22.0,
        "max_x": 41.0,
        "max_y": 28.0,
        "unit": "mm",
    }
    iface.ipc_board_api.list_components = MagicMock(
        return_value=[
            {
                "reference": "R1",
                "position": {"x": 38, "y": 25, "unit": "mm"},
                "rotation": 0,
                "layer": "F.Cu",
                "boundingBox": fresh_bbox,
            }
        ]
    )

    out = handle_get_component_list(iface, {})

    [comp] = out["components"]
    assert comp["boundingBox"] == fresh_bbox
    iface.component_commands.get_component_list.assert_not_called()


# ---------------------------------------------------------------------------
# get_component_properties: same contract
# ---------------------------------------------------------------------------
def test_get_component_properties_leaves_missing_bbox_null():
    from handlers.ipc_fastpath import handle_get_component_properties

    iface = _make_iface()
    iface.ipc_board_api.list_components = MagicMock(
        return_value=[
            {
                "reference": "R1",
                "position": {"x": 38, "y": 25, "unit": "mm"},
                "boundingBox": None,
                "courtyard": None,
            }
        ]
    )

    out = handle_get_component_properties(iface, {"reference": "R1"})

    assert out["success"] is True
    assert out["component"]["boundingBox"] is None
    assert out["component"].get("courtyard") is None
    iface.component_commands.get_component_properties.assert_not_called()


# ---------------------------------------------------------------------------
# IPCBoardAPI.list_components normalises BoardLayer to a layer name string
# ---------------------------------------------------------------------------
def _stub_to_mm(monkeypatch):
    """Provide kipy.util.units.to_mm without requiring a real kipy install."""
    units = MagicMock()
    units.to_mm = lambda v: v / 1_000_000 if isinstance(v, int) else float(v)
    monkeypatch.setitem(sys.modules, "kipy", MagicMock())
    monkeypatch.setitem(sys.modules, "kipy.util", MagicMock())
    monkeypatch.setitem(sys.modules, "kipy.util.units", units)


def _fake_footprint(layer_value):
    """Build a kipy-shaped footprint stand-in with the given layer enum."""
    return SimpleNamespace(
        reference_field=SimpleNamespace(text=SimpleNamespace(value="R1")),
        value_field=SimpleNamespace(text=SimpleNamespace(value="10k")),
        definition=SimpleNamespace(library_link="lib:R_0402"),
        position=SimpleNamespace(x=38_000_000, y=25_000_000),
        orientation=SimpleNamespace(degrees=0),
        layer=layer_value,
        id="id-0",
        pads=[],
    )


class _LayerEnumWithName:
    """Enum-style object exposing .name (kipy on most versions)."""

    def __init__(self, name: str, value: int):
        self.name = name
        self._value = value

    def __str__(self) -> str:
        # On the version that triggered the user's bug, this is the int value.
        return str(self._value)


def test_layer_normalised_to_human_name_when_enum_str_returns_int(monkeypatch):
    """kipy on the user's KiCad returns ``str(layer) == '3'`` instead of
    ``'BL_F_Cu'``.  The handler must use ``.name`` so the user gets
    ``layer: 'F.Cu'`` rather than ``layer: '3'``."""
    _stub_to_mm(monkeypatch)
    from kicad_api.ipc_backend import IPCBoardAPI

    fp = _fake_footprint(_LayerEnumWithName("BL_F_Cu", 3))
    board = MagicMock()
    board.get_footprints = MagicMock(return_value=[fp])
    board.get_item_bounding_box = MagicMock(return_value=None)

    api = IPCBoardAPI(None, lambda *_a: None)
    api._board = board

    [comp] = api.list_components()

    assert comp["layer"] == "F.Cu"


def test_layer_string_stripped_when_str_is_enum_name(monkeypatch):
    """kipy on other versions returns ``str(layer) == 'BL_F_Cu'`` directly
    (no ``.name``).  The fallback strips ``BL_`` and replaces ``_`` with
    ``.`` so the output is still ``F.Cu``."""
    _stub_to_mm(monkeypatch)
    from kicad_api.ipc_backend import IPCBoardAPI

    # No .name attribute — only str() exists.
    class _LayerEnumPlain:
        def __str__(self):
            return "BL_F_Cu"

    fp = _fake_footprint(_LayerEnumPlain())
    board = MagicMock()
    board.get_footprints = MagicMock(return_value=[fp])
    board.get_item_bounding_box = MagicMock(return_value=None)

    api = IPCBoardAPI(None, lambda *_a: None)
    api._board = board

    [comp] = api.list_components()

    assert comp["layer"] == "F.Cu"
