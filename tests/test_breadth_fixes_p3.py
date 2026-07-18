"""P3 breadth-fix regressions: truthful error codes, case-insensitive formats,
idempotent add_net wording, unified pad-size keys, readable default 2D view.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.component import ComponentCommands  # noqa: E402
from commands.export import ExportCommands  # noqa: E402
from commands.footprint import FootprintCreator, _normalize_wh  # noqa: E402
from commands.routing import RoutingCommands  # noqa: E402

# ---- P3-1: truthful error codes -------------------------------------------


@pytest.mark.unit
def test_place_component_duplicate_reference_code():
    board = MagicMock()
    board.FindFootprintByReference.return_value = MagicMock()  # ref already exists
    cc = ComponentCommands(board=board, library_manager=MagicMock())
    res = cc.place_component(
        {"componentId": "R_0805", "reference": "R1", "position": {"x": 1, "y": 1}}
    )
    assert res["success"] is False
    assert res["errorCode"] == "DUPLICATE_REFERENCE"
    assert res["errorCode"] != "INTERNAL_ERROR"


@pytest.mark.unit
def test_check_bom_availability_empty_board_code():
    from handlers.jlcpcb import handle_check_bom_availability

    iface = MagicMock()
    iface.board.GetFootprints.return_value = []
    iface.jlcpcb_parts.get_database_stats.return_value = {"total_parts": 5}
    res = handle_check_bom_availability(iface, {"boardQty": 10})
    assert res["success"] is False
    assert res["errorCode"] == "EMPTY_BOARD"
    assert res["errorCode"] != "INTERNAL_ERROR"


# ---- P3-3: add_net idempotency wording ------------------------------------


def _net_board(exists: bool, netcode: int = 1) -> MagicMock:
    board = MagicMock()
    info = MagicMock()
    nets_map = MagicMock()
    nets_map.has_key.return_value = exists
    existing = MagicMock()
    existing.GetNetCode.return_value = netcode
    nets_map.__getitem__.return_value = existing
    info.NetsByName.return_value = nets_map
    board.GetNetInfo.return_value = info
    board.GetNetClasses.return_value = MagicMock()
    return board


@pytest.mark.unit
def test_add_net_existing_reports_already_existed():
    rc = RoutingCommands(_net_board(exists=True, netcode=7))
    res = rc.add_net({"name": "SIG1"})
    assert res["success"] is True
    assert res["already_existed"] is True
    assert res["message"] == "Net 'SIG1' already exists (netcode 7)"


@pytest.mark.unit
def test_add_net_new_reports_added():
    rc = RoutingCommands(_net_board(exists=False))
    res = rc.add_net({"name": "NEWNET"})
    assert res["success"] is True
    assert res["already_existed"] is False
    assert res["message"] == "Added net: NEWNET"


# ---- P3-2: export_bom accepts lowercase format ----------------------------


def _bom_board():
    board = MagicMock(name="board")
    fp = MagicMock(name="R1")
    fp.GetReference.return_value = "R1"
    fp.GetValue.return_value = "10k"
    fpid = MagicMock()
    fpid.GetUniStringLibId.return_value = "Resistor_SMD:R_0603_1608Metric"
    fp.GetFPID.return_value = fpid
    fp.GetLayer.return_value = 0
    board.GetFootprints.return_value = [fp]
    board.GetLayerName.return_value = "F.Cu"
    return board


@pytest.mark.unit
def test_export_bom_accepts_lowercase_format(tmp_path):
    out = tmp_path / "bom.csv"
    res = ExportCommands(_bom_board()).export_bom({"outputPath": str(out), "format": "csv"})
    assert res["success"] is True
    # Echo is normalised to the canonical uppercase form.
    assert res["file"]["format"] == "CSV"
    assert out.exists()


# ---- P3-4: unified pad-size keys ({x,y} == {w,h} == number) ----------------


@pytest.mark.unit
def test_normalize_wh_accepts_all_shapes():
    assert _normalize_wh(1.5) == {"w": 1.5, "h": 1.5}
    assert _normalize_wh({"w": 2, "h": 3}) == {"w": 2.0, "h": 3.0}
    assert _normalize_wh({"x": 2, "y": 3}) == {"w": 2.0, "h": 3.0}
    assert _normalize_wh({"x": 4}) == {"w": 4.0, "h": 4.0}  # single axis -> square
    assert _normalize_wh(None) is None
    assert _normalize_wh({}) is None
    assert _normalize_wh(True) is None  # bool is not a size


@pytest.mark.unit
def test_edit_footprint_pad_accepts_xy_size(tmp_path):
    mod = tmp_path / "T.kicad_mod"
    mod.write_text(
        '(footprint "T" (layer "F.Cu")\n'
        '  (pad "1" smd roundrect (at 0 0) (size 1 1) (layers "F.Cu"))\n'
        ")\n",
        encoding="utf-8",
    )
    # edit_component_pad's {x,y} key style must also work on edit_footprint_pad.
    res = FootprintCreator().edit_footprint_pad(
        footprint_path=str(mod), pad_number="1", size={"x": 1.5, "y": 2}
    )
    assert res["success"] is True
    assert "(size 1.5 2)" in mod.read_text(encoding="utf-8")


# ---- P3-7: readable default 2D-view resolution (alpha crop reports dims) ----


@pytest.mark.unit
def test_alpha_crop_returns_content_dimensions():
    Image = pytest.importorskip("PIL.Image")
    from commands.board.view import _alpha_crop

    img = Image.new("RGBA", (100, 80), (0, 0, 0, 0))
    for x in range(40, 60):  # 20 px wide
        for y in range(30, 40):  # 10 px tall
            img.putpixel((x, y), (255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    _, dims = _alpha_crop(buf.getvalue(), margin_px=5)
    # content bbox (40,30)-(60,40) grown 5 px each side and clamped => 30x20.
    assert dims == (30, 20)
