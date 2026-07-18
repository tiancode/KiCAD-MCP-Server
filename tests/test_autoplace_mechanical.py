"""Handler tests for auto_place_components' mechanical-footprint exclusion.

E2E finding: auto_place relocated MountingHole footprints into the component
cluster and created courtyard overlaps. Netless mechanical footprints
(mounting holes H/MH, fiducials FID, netless test points TP) must stay fixed by
default and be reported in ``skipped_mechanical``; ``includeMechanical: true``
opts back in.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from handlers.autoplace import handle_auto_place_components  # noqa: E402

_NM = 1_000_000


def _fp(ref, nets=(), fpid="Resistor_SMD:R_0402", anchor_mm=(10, 10), box_mm=(9, 9, 11, 11)):
    fp = MagicMock()
    fp.GetReference.return_value = ref
    fp.GetValue.return_value = ref
    fp.GetFPIDAsString.return_value = fpid
    pads = []
    for net in nets:
        pad = MagicMock()
        pad.GetNetname.return_value = net
        pads.append(pad)
    fp.Pads.return_value = pads
    pos = MagicMock()
    pos.x, pos.y = int(anchor_mm[0] * _NM), int(anchor_mm[1] * _NM)
    fp.GetPosition.return_value = pos
    bb = MagicMock()
    bb.GetLeft.return_value = int(box_mm[0] * _NM)
    bb.GetTop.return_value = int(box_mm[1] * _NM)
    bb.GetRight.return_value = int(box_mm[2] * _NM)
    bb.GetBottom.return_value = int(box_mm[3] * _NM)
    fp.GetBoundingBox.return_value = bb
    fp.GetCourtyard.side_effect = AttributeError("no courtyard in stub")
    fp.IsLocked.return_value = False
    fp.SetPosition = MagicMock()
    return fp


def _iface(footprints, board_mm=(0, 0, 100, 100)):
    iface = MagicMock()
    iface.board.GetFootprints.return_value = footprints
    bbox = MagicMock()
    bbox.GetLeft.return_value = int(board_mm[0] * _NM)
    bbox.GetTop.return_value = int(board_mm[1] * _NM)
    bbox.GetWidth.return_value = int((board_mm[2] - board_mm[0]) * _NM)
    bbox.GetHeight.return_value = int((board_mm[3] - board_mm[1]) * _NM)
    iface.board.GetBoardEdgesBoundingBox.return_value = bbox
    return iface


@pytest.mark.unit
def test_mounting_holes_skipped_by_default_and_not_moved():
    # Two connected parts + four netless mounting holes at the corners.
    holes = [
        _fp("MH1", (), "MountingHole:MountingHole_3.2mm_M3", anchor_mm=(3, 3)),
        _fp("MH2", (), "MountingHole:MountingHole_3.2mm_M3", anchor_mm=(97, 3)),
        _fp("MH3", (), "MountingHole:MountingHole_3.2mm_M3", anchor_mm=(3, 97)),
        _fp("MH4", (), "MountingHole:MountingHole_3.2mm_M3", anchor_mm=(97, 97)),
    ]
    u1 = _fp("U1", ("N1",), anchor_mm=(50, 50))
    r1 = _fp("R1", ("N1",), anchor_mm=(60, 60))
    iface = _iface(holes + [u1, r1])

    out = handle_auto_place_components(iface, {})

    assert out["success"], out
    assert out["skipped_mechanical"] == ["MH1", "MH2", "MH3", "MH4"]
    # Mounting holes are never repositioned.
    for hole in holes:
        hole.SetPosition.assert_not_called()
    # Real components are still placed.
    assert out["moved"] == 2
    u1.SetPosition.assert_called_once()
    r1.SetPosition.assert_called_once()


@pytest.mark.unit
def test_include_mechanical_true_places_holes():
    holes = [
        _fp("MH1", (), "MountingHole:MountingHole_3.2mm_M3", anchor_mm=(3, 3)),
        _fp("MH2", (), "MountingHole:MountingHole_3.2mm_M3", anchor_mm=(97, 3)),
    ]
    u1 = _fp("U1", ("N1",), anchor_mm=(50, 50))
    r1 = _fp("R1", ("N1",), anchor_mm=(60, 60))
    iface = _iface(holes + [u1, r1])

    out = handle_auto_place_components(iface, {"includeMechanical": True})

    assert out["success"], out
    assert out["skipped_mechanical"] == []
    # Every footprint is now eligible to move.
    assert out["moved"] == 4
    for hole in holes:
        hole.SetPosition.assert_called_once()


@pytest.mark.unit
def test_netted_testpoint_is_not_mechanical():
    """A test point (TP prefix) that carries a net is electrical, not mechanical
    — it must be placed, not skipped."""
    tp = _fp("TP1", ("SIGNAL",), "TestPoint:TestPoint_Pad_D1.0mm", anchor_mm=(20, 20))
    u1 = _fp("U1", ("SIGNAL",), anchor_mm=(50, 50))
    iface = _iface([tp, u1])

    out = handle_auto_place_components(iface, {})

    assert out["success"], out
    assert out["skipped_mechanical"] == []
    tp.SetPosition.assert_called_once()


@pytest.mark.unit
def test_netless_fiducial_by_reference_prefix_is_skipped():
    """A netless FID/H footprint is mechanical by reference prefix even without
    a MountingHole footprint id."""
    fid = _fp("FID1", (), "Fiducial:Fiducial_1mm_Mask2mm", anchor_mm=(5, 5))
    u1 = _fp("U1", ("N1",), anchor_mm=(50, 50))
    r1 = _fp("R1", ("N1",), anchor_mm=(60, 60))
    iface = _iface([fid, u1, r1])

    out = handle_auto_place_components(iface, {})

    assert out["success"], out
    assert out["skipped_mechanical"] == ["FID1"]
    fid.SetPosition.assert_not_called()


@pytest.mark.unit
def test_dry_run_reports_skipped_without_moving():
    holes = [_fp("MH1", (), "MountingHole:MountingHole_3.2mm_M3", anchor_mm=(3, 3))]
    u1 = _fp("U1", ("N1",), anchor_mm=(50, 50))
    r1 = _fp("R1", ("N1",), anchor_mm=(60, 60))
    iface = _iface(holes + [u1, r1])

    out = handle_auto_place_components(iface, {"dryRun": True})

    assert out["success"], out
    assert out["dryRun"] is True
    assert out["skipped_mechanical"] == ["MH1"]
    assert out["moved"] == 0
    holes[0].SetPosition.assert_not_called()
