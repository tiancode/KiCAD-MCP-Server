"""Regression tests for finding F9 — SWIG force-fill reaching the board edge.

``copper_pour(action=refill, force=true)`` (Python ``refill_zones``, SWIG
path) filled zones right up to Edge.Cuts (0.0 mm) when the board's
copper-to-edge clearance (``m_CopperEdgeClearance``) was unset/zero, producing
a ``copper_edge_clearance`` DRC error.  The fix defaults that clearance to
0.5 mm and RE-LOADS the board before filling (the SWIG ZONE_FILLER only honours
the edge clearance read from a freshly-loaded board, not an in-memory setter).

These tests run against the stubbed ``pcbnew`` and monkeypatch
``subprocess.run`` — they pin the shipped subprocess-script content and the
warning wiring.  The real-pcbnew + ``kicad-cli pcb drc`` proof is documented in
the work-package report (fill inset 0.55 mm, zero zone-vs-edge violations).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _iface(board_path: Path):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    fake_board = MagicMock()
    fake_board.GetAreaCount.return_value = 1
    fake_board.GetFileName.return_value = str(board_path)
    iface.board = fake_board
    iface._save_board_and_record = MagicMock()
    iface._safe_load_board = MagicMock(return_value=MagicMock())
    iface._update_command_handlers = MagicMock()
    iface._record_board_signature = MagicMock()
    return iface


def test_fill_script_insets_by_edge_clearance(monkeypatch, tmp_path):
    """The subprocess fill script must default m_CopperEdgeClearance and
    reload before filling so the fill does not reach Edge.Cuts."""
    from handlers.routing import handle_refill_zones

    board_path = tmp_path / "demo.kicad_pcb"
    board_path.write_text("(kicad_pcb)\n", encoding="utf-8")
    iface = _iface(board_path)

    captured = {}

    def _fake_run(cmd, *a, **kw):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)

    out = handle_refill_zones(iface, {"force": True})
    assert out["success"] is True

    # cmd == [python, "-c", <script>]
    script = captured["cmd"][2]
    assert "m_CopperEdgeClearance" in script
    assert "FromMM(0.5)" in script
    # It must re-load the board after bumping the setting (LoadBoard appears
    # more than once) so the ZONE_FILLER honours the new clearance.
    assert script.count("LoadBoard") >= 2
    assert "ZONE_FILLER" in script


def test_edge_clearance_default_surfaces_warning(monkeypatch, tmp_path):
    """When the subprocess reports it defaulted the edge clearance, the
    handler surfaces a dedicated warning so the agent knows the design
    setting changed."""
    from handlers.routing import handle_refill_zones

    board_path = tmp_path / "demo.kicad_pcb"
    board_path.write_text("(kicad_pcb)\n", encoding="utf-8")
    iface = _iface(board_path)

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: MagicMock(returncode=0, stdout="edge_clearance_defaulted\nok", stderr=""),
    )

    out = handle_refill_zones(iface, {"force": True})
    assert out["success"] is True
    warnings = out.get("warnings", [])
    assert any("copper-to-edge clearance" in w for w in warnings)
    assert any("0.5 mm" in w for w in warnings)


def test_no_edge_warning_when_clearance_already_set(monkeypatch, tmp_path):
    """A board whose edge clearance is already positive fills normally — no
    edge-default warning, only the standard force=true caveat."""
    from handlers.routing import handle_refill_zones

    board_path = tmp_path / "demo.kicad_pcb"
    board_path.write_text("(kicad_pcb)\n", encoding="utf-8")
    iface = _iface(board_path)

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: MagicMock(returncode=0, stdout="ok", stderr=""),
    )

    out = handle_refill_zones(iface, {"force": True})
    assert out["success"] is True
    warnings = out.get("warnings", [])
    assert any("force=true" in w for w in warnings)
    assert not any("copper-to-edge clearance" in w for w in warnings)
