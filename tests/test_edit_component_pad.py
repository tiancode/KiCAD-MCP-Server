"""Tests for edit_component_pad — pad repair on PLACED footprints.

Real case (GD32F103VET6 E2E): easyeda footprint BAT-SMD_CR1220-2 (battery
holder BT1) ships two thru_hole pads with EMPTY pad numbers and copper
diameter == drill diameter -> two unfixable annular_width DRC errors.
edit_footprint_pad only patches .kicad_mod files and only numbered pads, so
the placed instance could not be repaired at all.

Stub-level tests exercise selection (padNumber / padIndex / padType / all),
the annular-ring guard + force override, and the before/after reporting.
The integration test at the bottom drives the same code against REAL pcbnew
(via the system python3) on a scratch .kicad_pcb.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))

import pcbnew  # noqa: E402  (conftest stub)
from commands.component import ComponentCommands  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _mm(v: float) -> int:
    return int(round(v * 1_000_000))


def _vec(x_nm: int, y_nm: int) -> SimpleNamespace:
    return SimpleNamespace(x=x_nm, y=y_nm)


def _pad(
    number: str = "",
    attrib=None,
    shape=None,
    size=(1.0, 1.0),
    drill=(1.0, 1.0),
    pos=(0.0, 0.0),
) -> MagicMock:
    """A pad MagicMock whose setters update the matching getters."""
    p = MagicMock()
    p.GetNumber.return_value = number
    p.GetAttribute.return_value = attrib if attrib is not None else pcbnew.PAD_ATTRIB_PTH
    p.GetShape.return_value = shape if shape is not None else pcbnew.PAD_SHAPE_CIRCLE
    p.GetSize.return_value = _vec(_mm(size[0]), _mm(size[1]))
    p.GetDrillSize.return_value = _vec(_mm(drill[0]), _mm(drill[1]))
    p.GetPosition.return_value = _vec(_mm(pos[0]), _mm(pos[1]))

    def _set_size(v):
        p.GetSize.return_value = v

    def _set_drill(v):
        p.GetDrillSize.return_value = v

    def _set_number(n):
        p.GetNumber.return_value = n

    def _set_attr(a):
        p.GetAttribute.return_value = a

    def _set_shape(s):
        p.GetShape.return_value = s

    p.SetSize.side_effect = _set_size
    p.SetDrillSize.side_effect = _set_drill
    p.SetNumber.side_effect = _set_number
    p.SetAttribute.side_effect = _set_attr
    p.SetShape.side_effect = _set_shape
    return p


def _cmd(pads, reference="BT1"):
    module = MagicMock()
    module.Pads.return_value = list(pads)
    board = MagicMock()
    board.FindFootprintByReference.side_effect = lambda ref: module if ref == reference else None
    cc = ComponentCommands.__new__(ComponentCommands)
    cc.board = board
    return cc


@pytest.fixture(autouse=True)
def _real_vector2i(monkeypatch):
    """SetSize/SetDrillSize receive pcbnew.VECTOR2I(...); make it a real
    (x, y) namespace so the wired getters return inspectable values."""
    monkeypatch.setattr(pcbnew, "VECTOR2I", lambda x, y: SimpleNamespace(x=x, y=y))


def _bt1_pads():
    """The exact BT1 defect: two thru_hole pads, EMPTY numbers, copper == drill."""
    return [
        _pad(number="", size=(1.0, 1.0), drill=(1.0, 1.0), pos=(0, 0)),
        _pad(number="", size=(1.0, 1.0), drill=(1.0, 1.0), pos=(5, 0)),
    ]


# ---------------------------------------------------------------------------
# Acceptance: the BT1 repair
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bt1_repair_copper_over_drill_on_empty_numbered_pads():
    """Target the unnumbered thru-hole pads and set copper > drill."""
    pads = _bt1_pads()
    out = _cmd(pads).edit_component_pad(
        {"reference": "BT1", "padNumber": "", "all": True, "size": 1.7}
    )
    assert out["success"], out
    assert out["matched"] == 2
    for i, entry in enumerate(out["pads"]):
        assert entry["before"]["size"]["x"] == 1.0
        assert entry["after"]["size"]["x"] == 1.7
        assert entry["after"]["drill"]["x"] == 1.0
        assert entry["changes"] == ["size"]
    # the pad objects actually changed
    for p in pads:
        assert p.GetSize.return_value.x == _mm(1.7)
        assert p.GetDrillSize.return_value.x == _mm(1.0)


@pytest.mark.unit
def test_bt1_assign_numbers_by_index():
    pads = _bt1_pads()
    cc = _cmd(pads)
    out0 = cc.edit_component_pad({"reference": "BT1", "padIndex": 0, "newPadNumber": "1"})
    out1 = cc.edit_component_pad({"reference": "BT1", "padIndex": 1, "newPadNumber": "2"})
    assert out0["success"] and out1["success"]
    assert out0["pads"][0]["before"]["number"] == ""
    assert out0["pads"][0]["after"]["number"] == "1"
    assert pads[0].GetNumber.return_value == "1"
    assert pads[1].GetNumber.return_value == "2"


# ---------------------------------------------------------------------------
# Selection semantics
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_pad_number_multi_match_refused_without_all():
    out = _cmd(_bt1_pads()).edit_component_pad({"reference": "BT1", "padNumber": "", "size": 1.7})
    assert out["success"] is False
    assert len(out["candidates"]) == 2
    assert out["candidates"][0]["index"] == 0
    assert out["candidates"][1]["index"] == 1


@pytest.mark.unit
def test_pad_number_targets_single_match():
    pads = [_pad(number="1"), _pad(number="2")]
    out = _cmd(pads).edit_component_pad({"reference": "BT1", "padNumber": "2", "size": 2.0})
    assert out["success"], out
    assert out["matched"] == 1
    assert out["pads"][0]["index"] == 1
    pads[0].SetSize.assert_not_called()
    pads[1].SetSize.assert_called_once()


@pytest.mark.unit
def test_pad_type_filter_restricts_matches():
    pads = [
        _pad(number="1", attrib=pcbnew.PAD_ATTRIB_SMD, drill=(0, 0)),
        _pad(number="", attrib=pcbnew.PAD_ATTRIB_PTH),
    ]
    out = _cmd(pads).edit_component_pad(
        {"reference": "BT1", "padType": "thru_hole", "all": True, "size": 1.7}
    )
    assert out["success"], out
    assert out["matched"] == 1
    pads[0].SetSize.assert_not_called()
    pads[1].SetSize.assert_called_once()


@pytest.mark.unit
def test_pad_index_out_of_range():
    out = _cmd(_bt1_pads()).edit_component_pad({"reference": "BT1", "padIndex": 5, "size": 1.7})
    assert out["success"] is False
    assert "out of range" in out["message"]


@pytest.mark.unit
def test_missing_selector_lists_pads():
    out = _cmd(_bt1_pads()).edit_component_pad({"reference": "BT1", "size": 1.7})
    assert out["success"] is False
    assert "Missing pad selector" in out["message"]
    assert "[0]" in out["errorDetails"] and "[1]" in out["errorDetails"]


@pytest.mark.unit
def test_component_not_found():
    out = _cmd(_bt1_pads()).edit_component_pad({"reference": "U99", "size": 1.7})
    assert out["success"] is False
    assert "Component not found" in out["message"]


@pytest.mark.unit
def test_nothing_to_edit_refused():
    out = _cmd(_bt1_pads()).edit_component_pad({"reference": "BT1", "padIndex": 0})
    assert out["success"] is False
    assert "Nothing to edit" in out["message"]


# ---------------------------------------------------------------------------
# Annular-ring guard — never recreate the original defect
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_zero_annular_result_refused_without_force():
    """Setting copper == drill on a plated pad is refused (that's the exact
    defect this tool exists to repair)."""
    pads = [_pad(number="1", size=(1.6, 1.6), drill=(1.0, 1.0))]
    out = _cmd(pads).edit_component_pad({"reference": "BT1", "padNumber": "1", "size": 1.0})
    assert out["success"] is False
    assert out["needs_force"] is True
    assert out["violations"][0]["annular_mm"] == 0.0
    pads[0].SetSize.assert_not_called()


@pytest.mark.unit
def test_drill_only_edit_that_swallows_copper_refused():
    pads = [_pad(number="1", size=(1.0, 1.0), drill=(0.5, 0.5))]
    out = _cmd(pads).edit_component_pad({"reference": "BT1", "padNumber": "1", "drill": 1.2})
    assert out["success"] is False
    assert out["needs_force"] is True
    pads[0].SetDrillSize.assert_not_called()


@pytest.mark.unit
def test_force_overrides_annular_guard_with_warning():
    pads = [_pad(number="1", size=(1.6, 1.6), drill=(1.0, 1.0))]
    out = _cmd(pads).edit_component_pad(
        {"reference": "BT1", "padNumber": "1", "size": 1.0, "force": True}
    )
    assert out["success"], out
    assert "annular" in out["warning"]
    pads[0].SetSize.assert_called_once()


@pytest.mark.unit
def test_npth_exempt_from_annular_guard():
    """copper == drill is the NORMAL state for NPTH — no refusal."""
    pads = [_pad(number="", attrib=pcbnew.PAD_ATTRIB_NPTH, size=(2.0, 2.0), drill=(2.0, 2.0))]
    out = _cmd(pads).edit_component_pad({"reference": "BT1", "padIndex": 0, "drill": 1.8})
    assert out["success"], out


@pytest.mark.unit
def test_converting_to_npth_skips_guard():
    pads = [_pad(number="1", size=(1.0, 1.0), drill=(1.0, 1.0))]
    out = _cmd(pads).edit_component_pad(
        {"reference": "BT1", "padNumber": "1", "newPadType": "npth"}
    )
    assert out["success"], out
    assert pads[0].GetAttribute.return_value == pcbnew.PAD_ATTRIB_NPTH


@pytest.mark.unit
def test_smd_pad_without_drill_not_guarded():
    pads = [_pad(number="1", attrib=pcbnew.PAD_ATTRIB_SMD, size=(1.0, 0.5), drill=(0, 0))]
    out = _cmd(pads).edit_component_pad({"reference": "BT1", "padNumber": "1", "size": 0.8})
    assert out["success"], out


# ---------------------------------------------------------------------------
# Shape / combined edits
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_combined_edit_reports_all_changes():
    pads = _bt1_pads()
    out = _cmd(pads).edit_component_pad(
        {
            "reference": "BT1",
            "padIndex": 0,
            "size": 1.7,
            "drill": 0.9,
            "shape": "oval",
            "newPadNumber": "1",
        }
    )
    assert out["success"], out
    assert out["pads"][0]["changes"] == ["size", "drill", "shape", "number"]
    assert out["pads"][0]["after"]["shape"] == "oval"
    assert out["pads"][0]["after"]["number"] == "1"
    assert out["pads"][0]["after"]["drill"]["x"] == 0.9


@pytest.mark.unit
def test_oval_drill_via_xy_dict():
    pads = [_pad(number="1", size=(2.0, 3.0))]
    out = _cmd(pads).edit_component_pad(
        {"reference": "BT1", "padNumber": "1", "drill": {"x": 1.0, "y": 2.0}}
    )
    assert out["success"], out
    assert pads[0].GetDrillSize.return_value.x == _mm(1.0)
    assert pads[0].GetDrillSize.return_value.y == _mm(2.0)


@pytest.mark.unit
def test_no_board_loaded():
    cc = ComponentCommands.__new__(ComponentCommands)
    cc.board = None
    out = cc.edit_component_pad({"reference": "BT1", "size": 1.0})
    assert out["success"] is False
    assert "No board" in out["message"]


# ---------------------------------------------------------------------------
# Integration: real pcbnew on a scratch .kicad_pcb (subprocess, because
# conftest.py replaces pcbnew with a MagicMock inside this process).
# ---------------------------------------------------------------------------

_INTEGRATION_SCRIPT = textwrap.dedent("""
    import sys

    sys.path.insert(0, sys.argv[2])  # <repo>/python
    import pcbnew

    board_path = sys.argv[1]

    # --- Build the BT1 defect: 2 PTH pads, empty numbers, copper == drill ---
    board = pcbnew.CreateEmptyBoard()
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("BT1")
    fp.SetPosition(pcbnew.VECTOR2I(0, 0))
    for i in range(2):
        pad = pcbnew.PAD(fp)
        pad.SetNumber("")
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(1.0), pcbnew.FromMM(1.0)))
        pad.SetDrillSize(pcbnew.VECTOR2I(pcbnew.FromMM(1.0), pcbnew.FromMM(1.0)))
        pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(5 * i), 0))
        fp.Add(pad)
    board.Add(fp)
    board.Save(board_path)

    # --- Repair through the MCP command implementation ---
    board2 = pcbnew.LoadBoard(board_path)
    from commands.component import ComponentCommands

    cc = ComponentCommands.__new__(ComponentCommands)
    cc.board = board2

    # Unforced copper==drill must refuse
    refused = cc.edit_component_pad(
        {"reference": "BT1", "padNumber": "", "all": True, "size": 1.0}
    )
    assert refused["success"] is False and refused.get("needs_force"), refused

    out = cc.edit_component_pad(
        {"reference": "BT1", "padNumber": "", "all": True, "size": 1.7}
    )
    assert out["success"], out
    assert out["matched"] == 2, out
    assert out["pads"][0]["before"]["size"]["x"] == 1.0
    assert out["pads"][0]["after"]["size"]["x"] == 1.7

    num = cc.edit_component_pad({"reference": "BT1", "padIndex": 0, "newPadNumber": "1"})
    assert num["success"], num

    # --- Verify the pad objects changed and the repair survives a reload ---
    board2.Save(board_path)
    board3 = pcbnew.LoadBoard(board_path)
    pads = list(board3.FindFootprintByReference("BT1").Pads())
    assert len(pads) == 2
    numbers = sorted(p.GetNumber() for p in pads)
    assert numbers == ["", "1"], numbers
    for p in pads:
        assert p.GetSize().x == pcbnew.FromMM(1.7), p.GetSize().x
        assert p.GetDrillSize().x == pcbnew.FromMM(1.0)

    print("INTEGRATION-OK")
    """)


@pytest.mark.integration
def test_bt1_repair_against_real_pcbnew(tmp_path):
    """End-to-end on real pcbnew: build the BT1 defect on a scratch board,
    repair it with edit_component_pad, verify persistence across reload."""
    probe = subprocess.run(
        ["python3", "-c", "import pcbnew"],
        capture_output=True,
        timeout=120,
    )
    if probe.returncode != 0:
        pytest.skip("system python3 cannot import real pcbnew")

    script = tmp_path / "driver.py"
    script.write_text(_INTEGRATION_SCRIPT, encoding="utf-8")
    board_path = tmp_path / "bt1_scratch.kicad_pcb"
    result = subprocess.run(
        ["python3", str(script), str(board_path), str(PYTHON_DIR)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "INTEGRATION-OK" in result.stdout
