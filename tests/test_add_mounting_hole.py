"""
Regression tests for BoardOutlineCommands.add_mounting_hole.

Covers two prior bugs:

1. Empty FPID
   The footprint was created with no library:name id, producing
   `(footprint "" ...)` in the .kicad_pcb. KiCad's GUI Move tool refuses to
   select footprints with no library link, so users couldn't drag the
   resulting MHs in the editor.

2. NPTH pad on copper layers
   The pad was emitted with the default LSET (`*.Cu` + `*.Mask`) even when
   `plated:false`. With `padDiameter > diameter` that produces phantom
   copper annular rings on every Cu layer, which trigger DRC clearance
   errors against neighbouring nets.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

import pcbnew  # noqa: E402  — pcbnew is stubbed by conftest


@pytest.fixture
def fresh_pcbnew_mock(monkeypatch):
    """
    The conftest pcbnew is a long-lived MagicMock. Reset its call history
    before each test so we can make precise assertions about what
    add_mounting_hole calls on the pcbnew API.
    """
    pcbnew.reset_mock()
    # PAD_ATTRIB constants must compare unequal so the conditional in the
    # implementation picks the right branch.
    pcbnew.PAD_ATTRIB_NPTH = "NPTH"
    pcbnew.PAD_ATTRIB_PTH = "PTH"
    pcbnew.PAD_SHAPE_CIRCLE = "circle"
    pcbnew.F_Mask = "F.Mask"
    pcbnew.B_Mask = "B.Mask"
    # Constants used by the F.Courtyard / F.Fab shape generation.
    pcbnew.SHAPE_T_CIRCLE = "circle_shape"
    pcbnew.F_CrtYd = "F.CrtYd"
    pcbnew.F_Fab = "F.Fab"
    return pcbnew


@pytest.fixture
def cmds(fresh_pcbnew_mock):
    from commands.board.outline import BoardOutlineCommands

    board = MagicMock(name="board")
    board.GetFootprints.return_value = []  # no existing MHs
    return BoardOutlineCommands(board=board)


def _captured_module(pcbnew_mock):
    """Return the FOOTPRINT mock instance created by the call under test."""
    return pcbnew_mock.FOOTPRINT.return_value


def _captured_pad(pcbnew_mock):
    """Return the PAD mock instance created by the call under test."""
    return pcbnew_mock.PAD.return_value


# ---------------------------------------------------------------------------
# Bug #1: empty FPID
# ---------------------------------------------------------------------------


class TestFootprintLibIdSet:
    def test_default_fpid_uses_diameter(self, cmds, fresh_pcbnew_mock, monkeypatch):
        # Pin the stock-lib probe to "not found" so this covers the legacy
        # synthetic-name fallback deterministically (independent of whether the
        # test host has the KiCAD footprint library installed). The real
        # stock-lib resolution path is covered in
        # tests/test_mounting_hole_footprint_resolution.py.
        import commands.board.outline as _outline

        monkeypatch.setattr(_outline, "_list_mountinghole_footprints", lambda: set())

        result = cmds.add_mounting_hole(
            {
                "position": {"x": 117, "y": 84.5, "unit": "mm"},
                "diameter": 3.2,
                "padDiameter": 3.5,
            }
        )

        assert result["success"] is True

        # LIB_ID was constructed with a non-empty library and footprint name
        fresh_pcbnew_mock.LIB_ID.assert_called_once_with("MountingHole", "MountingHole_3.2mm")
        # And the FOOTPRINT had its FPID set
        _captured_module(fresh_pcbnew_mock).SetFPID.assert_called_once_with(
            fresh_pcbnew_mock.LIB_ID.return_value
        )
        # The response surfaces the lib id used
        assert result["mountingHole"]["footprintLibId"] == "MountingHole:MountingHole_3.2mm"

    def test_default_fpid_strips_trailing_zeros(self, cmds, fresh_pcbnew_mock):
        cmds.add_mounting_hole(
            {
                "position": {"x": 0, "y": 0, "unit": "mm"},
                "diameter": 3.0,  # would become "3.0mm" with %f, "3" with %g
            }
        )

        # %g formatting: 3.0 → "3"
        fresh_pcbnew_mock.LIB_ID.assert_called_once_with("MountingHole", "MountingHole_3mm")

    def test_explicit_fpid_override(self, cmds, fresh_pcbnew_mock):
        cmds.add_mounting_hole(
            {
                "position": {"x": 50, "y": 50, "unit": "mm"},
                "diameter": 3.2,
                "footprintLibId": "MountingHole:MountingHole_3.2mm_M3",
            }
        )

        fresh_pcbnew_mock.LIB_ID.assert_called_once_with("MountingHole", "MountingHole_3.2mm_M3")

    def test_explicit_fpid_without_colon_falls_back_to_mountinghole_lib(
        self, cmds, fresh_pcbnew_mock
    ):
        cmds.add_mounting_hole(
            {
                "position": {"x": 0, "y": 0, "unit": "mm"},
                "diameter": 2.5,
                "footprintLibId": "MyCustomHole",
            }
        )

        fresh_pcbnew_mock.LIB_ID.assert_called_once_with("MountingHole", "MyCustomHole")


# ---------------------------------------------------------------------------
# Bug #2: NPTH pad layers
# ---------------------------------------------------------------------------


class TestNpthPadLayers:
    def test_npth_pad_layers_are_mask_only(self, cmds, fresh_pcbnew_mock):
        cmds.add_mounting_hole(
            {
                "position": {"x": 117, "y": 84.5, "unit": "mm"},
                "diameter": 3.2,
                "padDiameter": 3.5,
                "plated": False,
            }
        )

        pad = _captured_pad(fresh_pcbnew_mock)

        # The pad must have been set to NPTH attr
        pad.SetAttribute.assert_called_once_with("NPTH")

        # SetLayerSet was called exactly once with an LSET that has
        # F_Mask and B_Mask added — and nothing on Cu layers.
        pad.SetLayerSet.assert_called_once()
        lset_arg = pad.SetLayerSet.call_args.args[0]

        added_layers = [c.args[0] for c in lset_arg.AddLayer.call_args_list]
        assert "F.Mask" in added_layers
        assert "B.Mask" in added_layers
        assert all(
            "Cu" not in str(layer) for layer in added_layers
        ), f"NPTH pad must not include any Cu layers, got: {added_layers}"

    def test_npth_is_default(self, cmds, fresh_pcbnew_mock):
        # Omit `plated` entirely; default must be NPTH.
        cmds.add_mounting_hole(
            {
                "position": {"x": 0, "y": 0, "unit": "mm"},
                "diameter": 3.2,
            }
        )

        pad = _captured_pad(fresh_pcbnew_mock)
        pad.SetAttribute.assert_called_once_with("NPTH")
        pad.SetLayerSet.assert_called_once()

    def test_pth_keeps_default_layers(self, cmds, fresh_pcbnew_mock):
        cmds.add_mounting_hole(
            {
                "position": {"x": 0, "y": 0, "unit": "mm"},
                "diameter": 3.2,
                "padDiameter": 3.5,
                "plated": True,
            }
        )

        pad = _captured_pad(fresh_pcbnew_mock)
        pad.SetAttribute.assert_called_once_with("PTH")

        # For PTH, the default LSET (*.Cu + *.Mask) is correct, so we must
        # NOT override it via SetLayerSet.
        pad.SetLayerSet.assert_not_called()


# ---------------------------------------------------------------------------
# B9(b): NPTH pad geometry follows the KiCad-library convention
#   (bare hole → pad size == drill == hole; no annular ring, no oversized mask).
# ---------------------------------------------------------------------------


class TestNpthGeometryConvention:
    def test_npth_default_pad_size_equals_hole(self, cmds, fresh_pcbnew_mock):
        result = cmds.add_mounting_hole(
            {"position": {"x": 0, "y": 0, "unit": "mm"}, "diameter": 3.2}
        )
        # Bare NPTH hole: pad diameter defaults to the hole diameter (no ring).
        assert result["mountingHole"]["padDiameter"] == 3.2
        # Both SetSize and SetDrillSize therefore build VECTOR2I(3.2mm, 3.2mm).
        vc = [c.args for c in fresh_pcbnew_mock.VECTOR2I.call_args_list]
        assert (3_200_000, 3_200_000) in vc, vc

    def test_plated_default_keeps_annular_ring(self, cmds, fresh_pcbnew_mock):
        result = cmds.add_mounting_hole(
            {"position": {"x": 0, "y": 0, "unit": "mm"}, "diameter": 3.2, "plated": True}
        )
        # Plated hole keeps a ~1 mm annular copper ring so there is copper.
        assert result["mountingHole"]["padDiameter"] == 4.2

    def test_explicit_pad_diameter_is_respected(self, cmds, fresh_pcbnew_mock):
        result = cmds.add_mounting_hole(
            {"position": {"x": 0, "y": 0, "unit": "mm"}, "diameter": 3.2, "padDiameter": 3.5}
        )
        assert result["mountingHole"]["padDiameter"] == 3.5


# ---------------------------------------------------------------------------
# B9(a): the generated footprint carries an F.Courtyard circle (and an F.Fab
# hole outline) — without a courtyard the part feeds check_courtyard_overlaps'
# text-inflated bbox fallback and KiCad DRC's missing-courtyard checks.
# ---------------------------------------------------------------------------


class TestCourtyardAndFab:
    def test_courtyard_circle_added_with_margin(self, cmds, fresh_pcbnew_mock):
        result = cmds.add_mounting_hole(
            {"position": {"x": 5, "y": 5, "unit": "mm"}, "diameter": 3.2}
        )
        assert result["success"] is True
        assert result["mountingHole"]["courtyard"] is True

        pcb = fresh_pcbnew_mock
        assert pcb.PCB_SHAPE.called, "a PCB_SHAPE must be created for the courtyard"
        shape = pcb.PCB_SHAPE.return_value
        set_layers = [c.args[0] for c in shape.SetLayer.call_args_list]
        assert "F.CrtYd" in set_layers

        # Courtyard radius = hole radius (1.6 mm) + 0.25 mm margin = 1.85 mm,
        # built as VECTOR2I(1_850_000, 0).
        radius_calls = [c.args for c in pcb.VECTOR2I.call_args_list]
        assert (1_850_000, 0) in radius_calls, radius_calls

    def test_courtyard_radius_tracks_annular_pad(self, cmds, fresh_pcbnew_mock):
        # With a 3.5 mm pad the courtyard must enclose the pad, not just the
        # hole: radius = 3.5/2 + 0.25 = 2.0 mm.
        cmds.add_mounting_hole(
            {"position": {"x": 0, "y": 0, "unit": "mm"}, "diameter": 3.2, "padDiameter": 3.5}
        )
        radius_calls = [c.args for c in fresh_pcbnew_mock.VECTOR2I.call_args_list]
        assert (2_000_000, 0) in radius_calls, radius_calls

    def test_fab_circle_added_at_hole_radius(self, cmds, fresh_pcbnew_mock):
        cmds.add_mounting_hole({"position": {"x": 0, "y": 0, "unit": "mm"}, "diameter": 3.2})
        pcb = fresh_pcbnew_mock
        shape = pcb.PCB_SHAPE.return_value
        set_layers = [c.args[0] for c in shape.SetLayer.call_args_list]
        assert "F.Fab" in set_layers
        # Fab outline is drawn at the hole radius (1.6 mm).
        radius_calls = [c.args for c in pcb.VECTOR2I.call_args_list]
        assert (1_600_000, 0) in radius_calls, radius_calls
