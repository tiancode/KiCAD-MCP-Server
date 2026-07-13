"""
Regression tests for sync_schematic_to_board's footprint-add path.

Before the fix, _handle_sync_schematic_to_board only mutated nets and pad
assignments — it iterated board.GetFootprints() and never added new ones.
A schematic symbol whose Reference was not yet on the PCB was therefore
silently dropped on the floor: no footprint added, no rats nest reaching
the missing component.

These tests cover _add_missing_footprints_from_schematic and its kicad-cli
helper _extract_components_from_schematic.
"""

import sys
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_existing_fp(reference: str) -> MagicMock:
    fp = MagicMock(name=f"existing_fp_{reference}")
    fp.GetReference.return_value = reference
    return fp


def _interface() -> Any:
    from kicad_interface import KiCADInterface

    return KiCADInterface()


# ---------------------------------------------------------------------------
# _add_missing_footprints_from_schematic
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAddMissingFootprintsFromSchematic:
    """The fix path: walk netlist, add footprints for refs not yet on the board."""

    def _patch_extract(self, components: List[dict]) -> Any:
        return patch.object(
            _interface().__class__,
            "_extract_components_from_schematic",
            return_value=components,
        )

    def test_adds_footprint_for_missing_reference(self, tmp_path: Any) -> None:
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)\n")

        board = MagicMock(name="board")
        board.GetFootprints.return_value = []  # nothing on the board yet

        loaded_module = MagicMock(name="loaded_R0603")
        with (
            patch.object(
                _interface().__class__,
                "_extract_components_from_schematic",
                return_value=[
                    {
                        "reference": "R99",
                        "value": "10k",
                        "footprint": "Resistor_SMD:R_0603_1608Metric",
                    }
                ],
            ),
            patch("kicad_interface.pcbnew") as mock_pcbnew,
            patch("commands.library.LibraryManager") as mock_lm_cls,
        ):
            mock_pcbnew.FootprintLoad.return_value = loaded_module
            lm = MagicMock()
            lm.libraries = {"Resistor_SMD": "/fake/Resistor_SMD.pretty"}
            mock_lm_cls.return_value = lm

            iface = _interface()
            added, skipped = iface._add_missing_footprints_from_schematic(board, str(sch))

        assert len(added) == 1
        assert added[0]["reference"] == "R99"
        assert added[0]["footprint"] == "Resistor_SMD:R_0603_1608Metric"
        assert skipped == []
        # Footprint was added to the board.
        board.Add.assert_called_once_with(loaded_module)
        loaded_module.SetReference.assert_called_with("R99")
        loaded_module.SetValue.assert_called_with("10k")

    def test_skips_reference_already_on_board(self, tmp_path: Any) -> None:
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)\n")

        board = MagicMock(name="board")
        board.GetFootprints.return_value = [_make_existing_fp("R1")]

        with (
            patch.object(
                _interface().__class__,
                "_extract_components_from_schematic",
                return_value=[
                    {
                        "reference": "R1",
                        "value": "10k",
                        "footprint": "Resistor_SMD:R_0603_1608Metric",
                    }
                ],
            ),
            patch("kicad_interface.pcbnew"),
            patch("commands.library.LibraryManager") as mock_lm_cls,
        ):
            lm = MagicMock()
            lm.libraries = {"Resistor_SMD": "/fake/Resistor_SMD.pretty"}
            mock_lm_cls.return_value = lm

            iface = _interface()
            added, skipped = iface._add_missing_footprints_from_schematic(board, str(sch))

        assert added == []
        assert skipped == []
        board.Add.assert_not_called()

    def test_skips_power_symbols(self, tmp_path: Any) -> None:
        """References starting with # (e.g. #PWR, #FLG) have no PCB footprint."""
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)\n")

        board = MagicMock(name="board")
        board.GetFootprints.return_value = []

        with (
            patch.object(
                _interface().__class__,
                "_extract_components_from_schematic",
                return_value=[
                    {"reference": "#PWR0001", "value": "GND", "footprint": ""},
                    {"reference": "#FLG0001", "value": "PWR_FLAG", "footprint": ""},
                ],
            ),
            patch("kicad_interface.pcbnew"),
            patch("commands.library.LibraryManager") as mock_lm_cls,
        ):
            mock_lm_cls.return_value = MagicMock(libraries={})

            iface = _interface()
            added, skipped = iface._add_missing_footprints_from_schematic(board, str(sch))

        assert added == []
        # Power refs are excluded entirely — they don't show up in the skipped
        # diagnostic list either, since "no PCB footprint" is the right answer.
        assert skipped == []
        board.Add.assert_not_called()

    def test_records_skip_reason_for_missing_footprint_property(self, tmp_path: Any) -> None:
        """A schematic symbol with no Footprint property is reported as skipped."""
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)\n")

        board = MagicMock(name="board")
        board.GetFootprints.return_value = []

        with (
            patch.object(
                _interface().__class__,
                "_extract_components_from_schematic",
                return_value=[{"reference": "R1", "value": "10k", "footprint": ""}],
            ),
            patch("kicad_interface.pcbnew"),
            patch("commands.library.LibraryManager") as mock_lm_cls,
        ):
            mock_lm_cls.return_value = MagicMock(libraries={})

            iface = _interface()
            added, skipped = iface._add_missing_footprints_from_schematic(board, str(sch))

        assert added == []
        assert len(skipped) == 1
        assert skipped[0]["reference"] == "R1"
        assert "no Library:Name" in skipped[0]["reason"]

    def test_records_skip_reason_for_unknown_library(self, tmp_path: Any) -> None:
        """If the footprint's library nickname isn't in fp-lib-table, skip with reason."""
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)\n")

        board = MagicMock(name="board")
        board.GetFootprints.return_value = []

        with (
            patch.object(
                _interface().__class__,
                "_extract_components_from_schematic",
                return_value=[
                    {
                        "reference": "U1",
                        "value": "MyChip",
                        "footprint": "MyVendor:MyChip_QFN24",
                    }
                ],
            ),
            patch("kicad_interface.pcbnew"),
            patch("commands.library.LibraryManager") as mock_lm_cls,
        ):
            mock_lm_cls.return_value = MagicMock(libraries={})  # MyVendor not present

            iface = _interface()
            added, skipped = iface._add_missing_footprints_from_schematic(board, str(sch))

        assert added == []
        assert len(skipped) == 1
        assert skipped[0]["reference"] == "U1"
        assert "MyVendor" in skipped[0]["reason"]

    def test_no_op_when_kicad_cli_returns_empty(self, tmp_path: Any) -> None:
        """If the netlist extractor returns nothing, the helper is a no-op."""
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)\n")

        board = MagicMock(name="board")
        board.GetFootprints.return_value = []

        with patch.object(
            _interface().__class__,
            "_extract_components_from_schematic",
            return_value=[],
        ):
            iface = _interface()
            added, skipped = iface._add_missing_footprints_from_schematic(board, str(sch))

        assert added == []
        assert skipped == []
        board.Add.assert_not_called()


# ---------------------------------------------------------------------------
# _extract_components_from_schematic
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExtractComponentsFromSchematic:
    """The kicad-cli helper that produces (reference, value, footprint) records."""

    def test_parses_kicad_xml_netlist(self, tmp_path: Any) -> None:
        netlist_xml = """<?xml version="1.0" encoding="UTF-8"?>
<export version="E">
  <design />
  <components>
    <comp ref="R1">
      <value>10k</value>
      <footprint>Resistor_SMD:R_0603_1608Metric</footprint>
    </comp>
    <comp ref="C1">
      <value>0.1uF</value>
      <footprint>Capacitor_SMD:C_0603_1608Metric</footprint>
    </comp>
    <comp ref="U1">
      <value>MyChip</value>
      <footprint />
    </comp>
  </components>
  <nets />
</export>
"""
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)\n")

        def fake_run(cmd: Any, **kwargs: Any) -> Any:
            output_idx = cmd.index("--output") + 1
            Path(cmd[output_idx]).write_text(netlist_xml)
            return MagicMock(returncode=0, stderr="", stdout="")

        with (
            patch.object(
                _interface().__class__, "_find_kicad_cli_static", return_value="/fake/kicad-cli"
            ),
            patch("subprocess.run", side_effect=fake_run),
        ):
            iface = _interface()
            comps = iface._extract_components_from_schematic(str(sch))

        assert len(comps) == 3
        refs = [c["reference"] for c in comps]
        assert refs == ["R1", "C1", "U1"]
        # Empty <footprint /> resolves to ""
        u1 = next(c for c in comps if c["reference"] == "U1")
        assert u1["footprint"] == ""

    def test_returns_empty_when_kicad_cli_missing(self, tmp_path: Any) -> None:
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)\n")

        with patch.object(_interface().__class__, "_find_kicad_cli_static", return_value=None):
            iface = _interface()
            comps = iface._extract_components_from_schematic(str(sch))

        assert comps == []

    def test_returns_empty_when_kicad_cli_fails(self, tmp_path: Any) -> None:
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)\n")

        with (
            patch.object(
                _interface().__class__, "_find_kicad_cli_static", return_value="/fake/kicad-cli"
            ),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=1, stderr="boom", stdout=""),
            ),
        ):
            iface = _interface()
            comps = iface._extract_components_from_schematic(str(sch))

        assert comps == []


# ---------------------------------------------------------------------------
# Grid layout for newly-added footprints — regression for the bug where
# every footprint added during sync_schematic_to_board landed at (0, 0),
# forcing the agent to issue N move_component calls before anything was
# visible.
# ---------------------------------------------------------------------------


def _stub_vector2i_factory():
    """Build a pcbnew.VECTOR2I stand-in that captures the (x, y) it was
    constructed with, so tests can assert on the positions assigned to
    each loaded footprint module."""

    class _StubVector2I:
        def __init__(self, x, y):
            self.x = x
            self.y = y

    return _StubVector2I


def _stub_loaded_module(name: str, bbox_w_nm: int = 0, bbox_h_nm: int = 0) -> MagicMock:
    """A fresh MagicMock per loaded footprint so SetPosition recorders
    don't bleed across modules.  Optionally configures the bounding box
    so tests can exercise the adaptive grid-spacing path (default 0 → the
    helper's min-pitch floor wins)."""
    module = MagicMock(name=name)
    module.SetPosition = MagicMock()
    module.SetReference = MagicMock()
    module.SetValue = MagicMock()
    module.SetFPID = MagicMock()
    bbox = MagicMock(name=f"{name}_bbox")
    bbox.GetWidth.return_value = bbox_w_nm
    bbox.GetHeight.return_value = bbox_h_nm
    module.GetBoundingBox.return_value = bbox
    return module


def _stub_existing_fp(reference: str, *, bbox_right_nm: int, anchor_x_nm: int = 0) -> MagicMock:
    """Existing-on-board footprint with a real bounding-box stub.  Tests
    must pin `bbox_right_nm` rather than relying on MagicMock auto-mock —
    `int(MagicMock())` happens to return 1 (not raise), so the grid-origin
    helper's bbox-vs-anchor fallback only kicks in if we don't stub bbox
    at all."""
    fp = MagicMock(name=f"existing_{reference}")
    fp.GetReference.return_value = reference
    fp.GetPosition.return_value = MagicMock(x=anchor_x_nm, y=0)
    bbox = MagicMock(name=f"{reference}_bbox")
    bbox.GetRight.return_value = bbox_right_nm
    fp.GetBoundingBox.return_value = bbox
    return fp


def _components(count: int) -> List[dict]:
    return [
        {
            "reference": f"R{i+1}",
            "value": "10k",
            "footprint": "Resistor_SMD:R_0603_1608Metric",
        }
        for i in range(count)
    ]


@pytest.mark.unit
class TestNewFootprintGridLayout:
    """The fix: lay new footprints out in a roughly-square grid (15 mm
    pitch, starting at 10 mm from the page origin on an empty board, or
    20 mm past the existing cluster).  Previously every newly-added
    footprint was stamped with SetPosition(VECTOR2I(0, 0)) — eight new
    components meant eight identical positions, eight manual moves."""

    def _run_add(self, components, existing_fps):
        sch = Path("/tmp/test.kicad_sch")
        loaded_modules = [_stub_loaded_module(f"loaded_{i}") for i in range(len(components))]

        board = MagicMock(name="board")
        board.GetFootprints.return_value = existing_fps

        with (
            patch.object(
                _interface().__class__,
                "_extract_components_from_schematic",
                return_value=components,
            ),
            patch("kicad_interface.pcbnew") as mock_pcbnew,
            patch("commands.library.LibraryManager") as mock_lm_cls,
        ):
            mock_pcbnew.VECTOR2I = _stub_vector2i_factory()
            mock_pcbnew.LIB_ID = MagicMock()
            mock_pcbnew.FootprintLoad.side_effect = loaded_modules
            lm = MagicMock()
            lm.libraries = {"Resistor_SMD": "/fake/Resistor_SMD.pretty"}
            mock_lm_cls.return_value = lm

            iface = _interface()
            added, skipped = iface._add_missing_footprints_from_schematic(board, str(sch))

        return added, skipped, loaded_modules

    def test_empty_board_grid_places_8_footprints_at_distinct_positions(self):
        """The headline fix: 8 footprints on an empty board → 8 distinct
        grid cells, NOT 8x (0, 0)."""
        added, skipped, modules = self._run_add(_components(8), existing_fps=[])

        assert len(added) == 8
        # Every loaded module had SetPosition called exactly once.
        positions = []
        for m in modules:
            assert m.SetPosition.call_count == 1
            (vec,), _ = m.SetPosition.call_args
            positions.append((vec.x, vec.y))

        # All distinct — this is the property that was broken.
        assert len(set(positions)) == 8, f"expected 8 distinct positions, got {positions}"
        # Grid origin: (10mm, 10mm) → 10_000_000 nm.
        assert positions[0] == (10_000_000, 10_000_000)
        # 3x3 grid (ceil(sqrt(8)) = 3), 15 mm pitch.
        # idx=1 → col=1 row=0 → (10+15, 10) mm.
        assert positions[1] == (25_000_000, 10_000_000)
        # idx=3 → col=0 row=1 → (10, 10+15) mm.
        assert positions[3] == (10_000_000, 25_000_000)

    def test_added_response_includes_position_in_mm(self):
        """Agents need to know WHERE the new footprints landed so they
        can issue follow-up move_component calls (or skip them)."""
        added, _, _ = self._run_add(_components(2), existing_fps=[])

        assert all("position" in entry for entry in added)
        assert added[0]["position"] == {"x_mm": 10.0, "y_mm": 10.0}
        # ceil(sqrt(2)) = 2 cols → idx=1 is (10+15, 10) = (25, 10)
        assert added[1]["position"] == {"x_mm": 25.0, "y_mm": 10.0}

    def test_non_empty_board_starts_grid_past_existing_cluster(self):
        """When the board already has footprints, the grid origin must
        offset past the rightmost bbox edge (not just the anchor x)."""
        # Existing footprint whose bbox right edge is at 50 mm.
        # Reference U99 so it doesn't collide with the new R1/R2/R3.
        existing = _stub_existing_fp("U99", bbox_right_nm=50_000_000, anchor_x_nm=40_000_000)

        added, _, modules = self._run_add(_components(3), existing_fps=[existing])

        assert len(added) == 3
        # First new footprint should be at (bbox_right + 20mm, 10mm) = (70, 10).
        (vec0,), _ = modules[0].SetPosition.call_args
        assert vec0.x == 70_000_000
        assert vec0.y == 10_000_000

    def test_grid_columns_are_ceil_sqrt_count(self):
        """For 4 footprints we want a 2x2, for 9 a 3x3, etc."""
        # 4 footprints → 2 cols.  idx=2 should be (col=0, row=1).
        added, _, modules = self._run_add(_components(4), existing_fps=[])
        (vec2,), _ = modules[2].SetPosition.call_args
        # col=0, row=1 → (10mm, 25mm)
        assert vec2.x == 10_000_000
        assert vec2.y == 25_000_000

    def test_load_failures_dont_create_grid_gaps(self):
        """When one footprint fails to load, the next still gets the
        NEXT grid cell — gaps in the grid would waste board space.

        Achieved by deferring grid index assignment until after the
        load filter pass (only successful loads are indexed)."""
        sch = Path("/tmp/test.kicad_sch")

        # R1 loads, R2 fails (FootprintLoad returns None), R3 loads.
        modules = [_stub_loaded_module("R1"), _stub_loaded_module("R3")]
        load_results = [modules[0], None, modules[1]]

        board = MagicMock(name="board")
        board.GetFootprints.return_value = []

        with (
            patch.object(
                _interface().__class__,
                "_extract_components_from_schematic",
                return_value=_components(3),
            ),
            patch("kicad_interface.pcbnew") as mock_pcbnew,
            patch("commands.library.LibraryManager") as mock_lm_cls,
        ):
            mock_pcbnew.VECTOR2I = _stub_vector2i_factory()
            mock_pcbnew.LIB_ID = MagicMock()
            mock_pcbnew.FootprintLoad.side_effect = load_results
            lm = MagicMock()
            lm.libraries = {"Resistor_SMD": "/fake/Resistor_SMD.pretty"}
            mock_lm_cls.return_value = lm

            iface = _interface()
            added, skipped = iface._add_missing_footprints_from_schematic(board, str(sch))

        assert len(added) == 2
        assert len(skipped) == 1
        # Successful R1 → idx 0 = (10, 10).  Successful R3 → idx 1 = (25, 10).
        (vec0,), _ = modules[0].SetPosition.call_args
        (vec1,), _ = modules[1].SetPosition.call_args
        assert (vec0.x, vec0.y) == (10_000_000, 10_000_000)
        assert (vec1.x, vec1.y) == (25_000_000, 10_000_000), (
            "successful loads must occupy consecutive grid cells; "
            "the failed load must not leave a hole"
        )


@pytest.mark.unit
class TestNewFootprintGridRegressions:
    """Code-review findings on the first grid-layout commit (c9d9076):

    - Grid origin used `fp.GetPosition().x` not `bbox.GetRight()` — a
      large IC anchored at x=50mm with a bbox extending to x=80mm would
      be overlapped by new modules placed at x=70mm.
    - `max_x = 0` initialisation meant all-negative-X existing FPs
      reset the grid origin to (20mm, 10mm) instead of past their
      easternmost edge.
    - Duplicate refs in `components` (mis-annotated schematic) bypassed
      dedup because `existing_refs.add(ref)` was deferred to the second
      pass — both rows hit `if ref in existing_refs: continue` False on
      the first pass and produced two footprints with the same ref.
    - Hardcoded 15 mm cell pitch caused new modules with bbox > 15 mm
      (QFP, BGA, large connectors) to overlap each other in the grid.
    """

    def _run_add(self, components, existing_fps, loaded_modules=None):
        from tests.test_sync_schematic_to_board_footprints import _interface

        sch = Path("/tmp/test.kicad_sch")
        if loaded_modules is None:
            loaded_modules = [_stub_loaded_module(f"loaded_{i}") for i in range(len(components))]
        board = MagicMock(name="board")
        board.GetFootprints.return_value = existing_fps

        with (
            patch.object(
                _interface().__class__,
                "_extract_components_from_schematic",
                return_value=components,
            ),
            patch("kicad_interface.pcbnew") as mock_pcbnew,
            patch("commands.library.LibraryManager") as mock_lm_cls,
        ):
            mock_pcbnew.VECTOR2I = _stub_vector2i_factory()
            mock_pcbnew.LIB_ID = MagicMock()
            mock_pcbnew.FootprintLoad.side_effect = loaded_modules
            lm = MagicMock()
            lm.libraries = {"Resistor_SMD": "/fake/Resistor_SMD.pretty"}
            mock_lm_cls.return_value = lm
            iface = _interface()
            added, skipped = iface._add_missing_footprints_from_schematic(board, str(sch))
        return added, skipped, loaded_modules

    def test_grid_origin_uses_bbox_right_edge_not_anchor(self):
        """A wide IC anchored at x=30 mm but extending to x=80 mm must
        push the grid origin to (80+20, 10), not (30+20, 10)."""
        existing = _stub_existing_fp("U99", bbox_right_nm=80_000_000, anchor_x_nm=30_000_000)
        added, _, modules = self._run_add(_components(1), existing_fps=[existing])

        (vec0,), _ = modules[0].SetPosition.call_args
        assert vec0.x == 100_000_000, (
            "grid origin must be past the existing footprint's bbox right "
            "edge (80mm), not its anchor (30mm) — large ICs would otherwise "
            "have their right edge overlapped by the new grid"
        )

    def test_grid_origin_handles_all_negative_x_existing_cluster(self):
        """Existing footprints placed at x=-30mm must still receive the
        "20mm past their rightmost edge" treatment — not be floored to
        the page-origin offset."""
        existing = _stub_existing_fp("U99", bbox_right_nm=-30_000_000, anchor_x_nm=-50_000_000)
        added, _, modules = self._run_add(_components(1), existing_fps=[existing])

        (vec0,), _ = modules[0].SetPosition.call_args
        # -30mm + 20mm gutter = -10mm.  Grid starts at -10mm, not 20mm.
        assert vec0.x == -10_000_000, (
            "all-negative-X cluster must honor 'past existing cluster' "
            "contract; previously max_x=0 floor produced (20mm, 10mm) "
            "regardless of where the existing footprints actually lived"
        )

    def test_duplicate_reference_in_components_is_deduped(self):
        """A schematic with two R1 rows (mis-annotated, half-annotated,
        or a hierarchical-sheet bug in kicad-cli's netlist export) must
        produce ONE footprint, not two — board.Add for both would leave
        the PCB with two `R1`s.  Old behavior pre-c9d9076 caught this
        via in-loop existing_refs.add; the two-pass rewrite needed an
        explicit re-add at the end of the filter pass."""
        components = [
            {"reference": "R1", "value": "10k", "footprint": "Resistor_SMD:R_0603_1608Metric"},
            {"reference": "R1", "value": "10k", "footprint": "Resistor_SMD:R_0603_1608Metric"},
            {"reference": "R2", "value": "10k", "footprint": "Resistor_SMD:R_0603_1608Metric"},
        ]
        # Only two unique refs → only two footprints get loaded.
        loaded = [_stub_loaded_module("R1"), _stub_loaded_module("R2")]
        added, skipped, _ = self._run_add(components, existing_fps=[], loaded_modules=loaded)

        refs = [entry["reference"] for entry in added]
        assert refs == ["R1", "R2"], f"duplicate R1 must be deduped; got {refs}"
        assert all(s.get("reason") != "duplicate" for s in skipped), (
            "the second R1 is silently dropped (same shape as old code) — "
            "no need to add it to skipped, but it must NOT be in added"
        )

    def test_grid_spacing_adapts_to_large_module_bbox(self):
        """A 20mm-wide QFP must NOT be placed at 15mm pitch — that would
        guarantee overlap between adjacent cells.  The helper picks
        max(min_pitch, bbox + padding) per axis from the largest loaded
        module."""
        # 20 mm × 20 mm bbox → cell pitch should be 25 mm (20 + 5 padding).
        loaded = [
            _stub_loaded_module(f"loaded_{i}", bbox_w_nm=20_000_000, bbox_h_nm=20_000_000)
            for i in range(4)
        ]
        added, _, modules = self._run_add(_components(4), existing_fps=[], loaded_modules=loaded)

        # Origin (10, 10), 2x2 grid, pitch 25 mm.
        (vec0,), _ = modules[0].SetPosition.call_args
        (vec1,), _ = modules[1].SetPosition.call_args
        (vec2,), _ = modules[2].SetPosition.call_args
        assert (vec0.x, vec0.y) == (10_000_000, 10_000_000)
        # idx=1 → col=1 row=0 → (10 + 25, 10) mm.
        assert (vec1.x, vec1.y) == (35_000_000, 10_000_000), (
            "20mm-wide module must use ≥25mm pitch (20mm bbox + 5mm padding); "
            "old hardcoded 15mm pitch would have placed at (25mm, 10mm) and "
            "the two bboxes [0..20] and [15..35] would overlap"
        )
        # idx=2 → col=0 row=1 → (10, 10 + 25) mm.
        assert (vec2.x, vec2.y) == (10_000_000, 35_000_000)


# ---------------------------------------------------------------------------
# Sourcing-field propagation: schematic custom fields (MPN, Manufacturer,
# "LCSC Part", Datasheet) must be copied onto the board footprints so a
# board-based export_bom can see the phase-1 sourcing pipeline.  Covers
# _extract_components_from_schematic (fields parsing), the skip-list
# (_is_propagatable_field), and _propagate_schematic_fields_to_board.
# ---------------------------------------------------------------------------


def _fp_with_fields(reference: str, current_fields: dict) -> MagicMock:
    """Board footprint whose GetFieldsText() returns a real dict and whose
    SetField records (name, value) calls for assertions."""
    fp = MagicMock(name=f"fp_{reference}")
    fp.GetReference.return_value = reference
    state = dict(current_fields)
    fp.GetFieldsText.return_value = state

    def _set_field(name, value):
        state[name] = value

    fp.SetField.side_effect = _set_field
    return fp


@pytest.mark.unit
class TestSchematicFieldPropagation:
    def test_extract_includes_custom_fields(self, tmp_path: Any) -> None:
        netlist_xml = """<?xml version="1.0" encoding="UTF-8"?>
<export version="E">
  <components>
    <comp ref="U1">
      <value>GD32F103VET6</value>
      <footprint>Package_QFP:LQFP-100</footprint>
      <fields>
        <field name="Manufacturer">GigaDevice</field>
        <field name="MPN">GD32F103VET6</field>
        <field name="LCSC Part">C80215</field>
        <field name="Sheetname">gd32_radio</field>
        <field name="ki_keywords">GigaDevice</field>
      </fields>
    </comp>
  </components>
</export>
"""
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)\n")

        def fake_run(cmd: Any, **kwargs: Any) -> Any:
            output_idx = cmd.index("--output") + 1
            Path(cmd[output_idx]).write_text(netlist_xml)
            return MagicMock(returncode=0, stderr="", stdout="")

        with (
            patch.object(
                _interface().__class__, "_find_kicad_cli_static", return_value="/fake/kicad-cli"
            ),
            patch("subprocess.run", side_effect=fake_run),
        ):
            comps = _interface()._extract_components_from_schematic(str(sch))

        (u1,) = comps
        assert u1["fields"]["MPN"] == "GD32F103VET6"
        assert u1["fields"]["LCSC Part"] == "C80215"
        # Raw extraction keeps everything; the skip-list is applied at propagation.
        assert u1["fields"]["Sheetname"] == "gd32_radio"

    def test_is_propagatable_field_skiplist(self) -> None:
        iface = _interface()
        assert iface._is_propagatable_field("MPN")
        assert iface._is_propagatable_field("LCSC Part")
        assert iface._is_propagatable_field("Datasheet")
        # Standard / handled-elsewhere and internal keys are skipped.
        for skip in ("Reference", "Value", "Footprint", "Sheetname", "Sheetfile",
                     "ki_keywords", "ki_description", "ki_fp_filters", ""):
            assert not iface._is_propagatable_field(skip), skip

    def test_propagate_sets_custom_fields_skipping_internal(self) -> None:
        fp = _fp_with_fields("U1", {"Reference": "U1", "Value": "GD32F103VET6"})
        board = MagicMock(name="board")
        board.GetFootprints.return_value = [fp]

        components = [
            {
                "reference": "U1",
                "value": "GD32F103VET6",
                "footprint": "Package_QFP:LQFP-100",
                "fields": {
                    "MPN": "GD32F103VET6",
                    "Manufacturer": "GigaDevice",
                    "LCSC Part": "C80215",
                    "Sheetname": "gd32_radio",
                    "ki_keywords": "GigaDevice",
                    "Footprint": "Package_QFP:LQFP-100",
                },
            }
        ]

        stats = _interface()._propagate_schematic_fields_to_board(board, components)

        written = {name for (name, _val), _ in fp.SetField.call_args_list}
        assert written == {"MPN", "Manufacturer", "LCSC Part"}
        # Internal / handled-elsewhere keys never written.
        assert "Sheetname" not in written
        assert "ki_keywords" not in written
        assert "Footprint" not in written
        assert stats == {"footprints_updated": 1, "fields_written": 3}

    def test_propagate_updates_changed_and_skips_unchanged(self) -> None:
        # U1 already has the right LCSC but a stale MPN; only MPN should be written.
        fp = _fp_with_fields("U1", {"LCSC Part": "C80215", "MPN": "OLD"})
        board = MagicMock(name="board")
        board.GetFootprints.return_value = [fp]
        components = [
            {"reference": "U1", "fields": {"MPN": "GD32F103VET6", "LCSC Part": "C80215"}}
        ]

        stats = _interface()._propagate_schematic_fields_to_board(board, components)

        calls = {name: val for (name, val), _ in fp.SetField.call_args_list}
        assert calls == {"MPN": "GD32F103VET6"}  # unchanged LCSC not rewritten
        assert stats == {"footprints_updated": 1, "fields_written": 1}

    def test_propagate_does_not_clobber_board_only_field_or_write_blanks(self) -> None:
        # Board carries a hand-added "Note" the schematic doesn't have, and the
        # schematic has a blank Datasheet — neither should be touched.
        fp = _fp_with_fields("U1", {"Note": "hand-added", "MPN": "OLD"})
        board = MagicMock(name="board")
        board.GetFootprints.return_value = [fp]
        components = [
            {"reference": "U1", "fields": {"MPN": "NEW", "Datasheet": "   ", "Note": ""}}
        ]

        _interface()._propagate_schematic_fields_to_board(board, components)

        calls = {name: val for (name, val), _ in fp.SetField.call_args_list}
        assert calls == {"MPN": "NEW"}  # blank Datasheet + blank Note skipped
        assert fp.GetFieldsText.return_value["Note"] == "hand-added"  # untouched

    def test_propagate_no_matching_footprint_is_noop(self) -> None:
        fp = _fp_with_fields("R1", {})
        board = MagicMock(name="board")
        board.GetFootprints.return_value = [fp]
        # Schematic component U1 has no board footprint (unlikely post-add, but
        # must not crash and must report zero updates).
        components = [{"reference": "U1", "fields": {"MPN": "X"}}]

        stats = _interface()._propagate_schematic_fields_to_board(board, components)
        assert stats == {"footprints_updated": 0, "fields_written": 0}
        fp.SetField.assert_not_called()
