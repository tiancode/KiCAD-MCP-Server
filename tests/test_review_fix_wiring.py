"""Wiring-layer regression tests for the 2026-07 review fixes.

The features' pure command modules were unit-tested but their handlers were
not, which let several contract bugs through (wrong stats key, missing price
normalization, anchor-vs-center placement, dryRun auto-save). These tests
exercise the handler layer with mocked boards/managers.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from handlers.autoplace import handle_auto_place_components
from handlers.jlcpcb import handle_check_bom_availability

_NM = 1_000_000


# ---------------------------------------------------------------------------
# check_bom_availability wiring
# ---------------------------------------------------------------------------


def _bom_iface(footprints, stats=None, parts_rows=None):
    iface = MagicMock()
    iface.board.GetFootprints.return_value = footprints
    manager = iface.jlcpcb_parts
    manager.get_database_stats.return_value = stats or {"total_parts": 1000}
    manager.get_part_info.return_value = None
    manager.search_parts_meta.return_value = {"parts": list(parts_rows or [])}
    manager.normalize_price_breaks.side_effect = lambda raw: (
        [{"qty": 1, "price": 0.01}] if raw else []
    )
    return iface


def _footprint(ref, value, fpid):
    fp = MagicMock()
    fp.GetReference.return_value = ref
    fp.GetValue.return_value = value
    fp.GetFPID.return_value.GetUniStringLibId.return_value = fpid
    fp.GetFieldByName.return_value = None
    fp.GetPropertyNative.return_value = ""
    return fp


@pytest.mark.unit
class TestCheckBomAvailabilityWiring:
    def test_populated_database_is_not_reported_empty(self):
        """Review fix F1: the handler must read total_parts, not total_components."""
        iface = _bom_iface([_footprint("R1", "10k", "Resistor_SMD:R_0603_1608Metric")])
        result = handle_check_bom_availability(iface, {})
        assert result["success"], result
        assert "empty" not in result.get("message", "")

    def test_truly_empty_database_still_refused(self):
        iface = _bom_iface([_footprint("R1", "10k", "R_0603")], stats={"total_parts": 0})
        result = handle_check_bom_availability(iface, {})
        assert result["success"] is False
        assert "download_jlcpcb_database" in result["hint"]

    def test_search_matched_lines_get_normalized_prices(self):
        """Review fix F5: raw search rows carry price_json only; the handler
        must normalize into price_breaks so search-matched lines are priced."""
        row = {
            "lcsc": "C25804",
            "mfr": "0603WAF1002T5E",
            "stock": 5000,
            "package": "0603",
            "price_json": '[{"qFrom":1,"price":0.01}]',
        }
        iface = _bom_iface(
            [_footprint("R1", "10k", "Resistor_SMD:R_0603_1608Metric")], parts_rows=[row]
        )
        result = handle_check_bom_availability(iface, {})
        assert result["success"], result
        (line,) = result["lines"]
        assert line["status"] == "ok"
        assert line["match"]["unitPrice"] == pytest.approx(0.01)
        assert result["summary"]["costComplete"] is True


# ---------------------------------------------------------------------------
# auto_place_components wiring
# ---------------------------------------------------------------------------


def _placeable_fp(ref, value, anchor_mm, box_mm, nets, locked=False):
    """Footprint whose keepout box may be asymmetric around the anchor."""
    fp = MagicMock()
    fp.GetReference.return_value = ref
    fp.GetValue.return_value = value
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
    # No courtyard on the mock — handler falls back to the bounding box.
    fp.GetCourtyard.side_effect = AttributeError("no courtyard in stub")
    fp.IsLocked.return_value = locked
    fp.SetPosition = MagicMock()
    return fp


def _autoplace_iface(footprints, board_mm=(0, 0, 100, 100)):
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
class TestAutoPlaceWiring:
    def test_anchor_offset_preserved_on_write_back(self):
        """Review fix F4: a pin-1-origin part (anchor at box corner) must keep
        its anchor-to-center offset when moved — SetPosition receives
        center + offset, not the raw center."""
        # Anchor at (10, 10); box spans (10,10)-(20,18): center (15, 14),
        # so offset = anchor - center = (-5, -4).
        fp_a = _placeable_fp("U1", "MCU", (10, 10), (10, 10, 20, 18), {"N1"})
        fp_b = _placeable_fp("R1", "10k", (40, 40), (39.5, 39.5, 40.5, 40.5), {"N1"})
        iface = _autoplace_iface([fp_a, fp_b])
        result = handle_auto_place_components(iface, {})
        assert result["success"], result

        placement = {p["reference"]: p for p in result["placements"]}["U1"]
        ((args, _),) = fp_a.SetPosition.call_args_list
        vec = args[0]
        # VECTOR2I called with ints: recover mm
        set_x, set_y = vec if isinstance(vec, tuple) else (None, None)
        # pcbnew is stubbed with MagicMock: VECTOR2I(x, y) returns a mock whose
        # call args carry the values; inspect the constructor call instead.
        import pcbnew

        ctor_args = pcbnew.VECTOR2I.call_args_list[-1][0]
        assert ctor_args[0] / _NM == pytest.approx(placement["x"] - 5.0, abs=1e-6)
        assert ctor_args[1] / _NM == pytest.approx(placement["y"] - 4.0, abs=1e-6)

    def test_dry_run_moves_nothing_and_reports(self):
        fp_a = _placeable_fp("U1", "MCU", (10, 10), (5, 5, 15, 15), {"N1"})
        iface = _autoplace_iface([fp_a])
        result = handle_auto_place_components(iface, {"dryRun": True})
        assert result["success"]
        assert result["dryRun"] is True
        assert result["moved"] == 0
        fp_a.SetPosition.assert_not_called()


# ---------------------------------------------------------------------------
# dryRun must not trigger the mutating-command auto-save (review fix F7)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDryRunSkipsAutoSave:
    def _iface(self):
        from unittest.mock import patch

        with patch("kicad_interface.USE_IPC_BACKEND", False):
            from kicad_interface import KiCADInterface

            iface = KiCADInterface.__new__(KiCADInterface)
        iface.board = MagicMock()
        iface.use_ipc = False
        iface.ipc_board_api = None
        iface._auto_save_board = MagicMock(return_value={"saved": True})
        iface._cross_backend_conflict = MagicMock(return_value=None)
        iface._annotate_stale_vs_disk = MagicMock()
        return iface

    def _dispatch(self, iface, response):
        from kicad_interface import KiCADInterface

        iface.command_routes = {"auto_place_components": lambda params: dict(response)}
        return KiCADInterface._dispatch_command(iface, "auto_place_components", {})

    def test_dry_run_result_skips_auto_save(self):
        iface = self._iface()
        result = self._dispatch(iface, {"success": True, "dryRun": True, "moved": 0})
        assert result["success"]
        iface._auto_save_board.assert_not_called()

    def test_real_run_still_auto_saves(self):
        iface = self._iface()
        result = self._dispatch(iface, {"success": True, "dryRun": False, "moved": 3})
        assert result["success"]
        iface._auto_save_board.assert_called_once()
