"""B8: SWIG and IPC component listings must agree on the component set.

The E2E found the SWIG ``get_component_list`` returning 19 components while
the IPC path returned 23 for the same board — the difference was the four
mounting holes (MH1-4).  Mounting holes are real footprints with real
references; both backends must list them, and both must tag them with
``is_mounting_hole`` so a consumer can filter intentionally instead of the
backends silently disagreeing.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


# ---------------------------------------------------------------------------
# SWIG side
# ---------------------------------------------------------------------------


def _bbox(left, top, right, bottom):
    bb = MagicMock()
    bb.GetLeft.return_value = left
    bb.GetTop.return_value = top
    bb.GetRight.return_value = right
    bb.GetBottom.return_value = bottom
    return bb


def _swig_module(ref, fpid):
    pos = MagicMock()
    pos.x = 5_000_000
    pos.y = 5_000_000
    m = MagicMock()
    m.GetPosition.return_value = pos
    m.GetReference.return_value = ref
    m.GetValue.return_value = ""
    m.GetFPIDAsString.return_value = fpid
    ori = MagicMock()
    ori.AsDegrees.return_value = 0
    m.GetOrientation.return_value = ori
    m.GetLayer.return_value = 0
    m.GetBoundingBox.return_value = _bbox(0, 0, 3_000_000, 3_000_000)
    return m


def _swig_list(modules):
    from commands.component import ComponentCommands

    board = MagicMock()
    board.GetLayerName.return_value = "F.Cu"
    board.GetFootprints.return_value = list(modules)
    cmd = ComponentCommands(board=board)
    out = cmd.get_component_list({})
    assert out["success"], out
    return out["components"]


# ---------------------------------------------------------------------------
# IPC side
# ---------------------------------------------------------------------------


def _stub_to_mm(monkeypatch):
    units = MagicMock()
    units.to_mm = lambda v: v / 1_000_000 if isinstance(v, int) else float(v)
    monkeypatch.setitem(sys.modules, "kipy", MagicMock())
    monkeypatch.setitem(sys.modules, "kipy.util", MagicMock())
    monkeypatch.setitem(sys.modules, "kipy.util.units", units)


def _ipc_fp(ref, library_link):
    return SimpleNamespace(
        reference_field=SimpleNamespace(text=SimpleNamespace(value=ref)),
        value_field=SimpleNamespace(text=SimpleNamespace(value="")),
        definition=SimpleNamespace(library_link=library_link),
        position=SimpleNamespace(x=5_000_000, y=5_000_000),
        orientation=SimpleNamespace(degrees=0),
        layer=SimpleNamespace(name="BL_F_Cu"),
        id="id-" + ref,
        pads=[],
    )


def _ipc_list(monkeypatch, fps):
    _stub_to_mm(monkeypatch)
    from kicad_api.ipc_backend import IPCBoardAPI

    board = MagicMock()
    board.get_footprints.return_value = list(fps)
    board.get_item_bounding_box.return_value = None
    api = IPCBoardAPI(None, lambda *_a: None)
    api._board = board
    return api.list_components()


# The same logical board, expressed for each backend: two normal parts and one
# mounting hole (MCP-generated FPID + MH reference).
_PARTS = [
    ("R1", "Device:R_0402"),
    ("C1", "Device:C_0402"),
    ("MH1", "MountingHole:MountingHole_3.2mm"),
]


def test_swig_and_ipc_list_the_same_component_set(monkeypatch):
    swig = _swig_list([_swig_module(ref, fpid) for ref, fpid in _PARTS])
    ipc = _ipc_list(monkeypatch, [_ipc_fp(ref, fpid) for ref, fpid in _PARTS])

    swig_refs = {c["reference"] for c in swig}
    ipc_refs = {c["reference"] for c in ipc}
    assert swig_refs == ipc_refs == {"R1", "C1", "MH1"}
    # The mounting hole is present on both — never filtered out.
    assert "MH1" in swig_refs


def test_both_backends_tag_mounting_holes_identically(monkeypatch):
    swig = {c["reference"]: c for c in _swig_list([_swig_module(r, f) for r, f in _PARTS])}
    ipc = {c["reference"]: c for c in _ipc_list(monkeypatch, [_ipc_fp(r, f) for r, f in _PARTS])}

    for ref in ("R1", "C1", "MH1"):
        expected = ref == "MH1"
        assert swig[ref]["is_mounting_hole"] is expected, (ref, swig[ref])
        assert ipc[ref]["is_mounting_hole"] is expected, (ref, ipc[ref])


def test_mounting_hole_detected_by_mh_reference_even_without_library_hint(monkeypatch):
    # An MH-referenced footprint whose FPID doesn't say "MountingHole" is still
    # classified as a hole (both backends use the shared classifier).
    parts = [("MH2", "Connector:TestPoint")]
    swig = _swig_list([_swig_module(r, f) for r, f in parts])
    ipc = _ipc_list(monkeypatch, [_ipc_fp(r, f) for r, f in parts])
    assert swig[0]["is_mounting_hole"] is True
    assert ipc[0]["is_mounting_hole"] is True
