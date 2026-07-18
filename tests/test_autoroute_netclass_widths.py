"""Regression test for autoroute honouring .kicad_pro net-class widths.

E2E finding (P2): create_netclass persists Power(0.5mm)/USB(0.3mm) to the
.kicad_pro, but autoroute exported a DSN that routed every net at the board
default (0.2mm) — a headless ``pcbnew.LoadBoard`` does not reliably reconstruct
the project net classes onto the BOARD the Specctra exporter reads.

The fix applies the project net classes (widths/clearances + memberships) to the
freshly-loaded board via the pcbnew NETCLASS API BEFORE ``ExportSpecctraDSN``.
Real DSN content is produced by pcbnew C++ (unavailable/stubbed here), so we pin
the state we CAN observe: the board's netclass table received the Power class at
0.5mm and the power nets were assigned to it before export was invoked.

Freerouting itself is never run — subprocess + pcbnew are mocked.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands import freerouting as fr_mod  # noqa: E402
from commands.freerouting import FreeroutingCommands  # noqa: E402

_NM = 1_000_000


# ---------------------------------------------------------------------------
# Fakes rich enough to exercise _apply_project_netclasses
# ---------------------------------------------------------------------------


class _FakeNetClass:
    def __init__(self, name):
        self.name = name
        self.track_width = None
        self.clearance = None
        self.via_diameter = None
        self.via_drill = None

    def SetTrackWidth(self, v):
        self.track_width = v

    def SetClearance(self, v):
        self.clearance = v

    def SetViaDiameter(self, v):
        self.via_diameter = v

    def SetViaDrill(self, v):
        self.via_drill = v

    # Setters that may be missing on some KiCad versions still exist here.
    def SetMicroViaDiameter(self, v):
        pass

    def SetMicroViaDrill(self, v):
        pass

    def SetDiffPairWidth(self, v):
        pass

    def SetDiffPairGap(self, v):
        pass


class _FakeNetClasses:
    """std::map-like container with Find/Add, mirroring KiCad 9/10."""

    def __init__(self):
        self.added = {}

    def Find(self, name):
        return self.added.get(name)

    def Add(self, nc):
        self.added[nc.name] = nc


class _FakeNet:
    def __init__(self, name):
        self.name = name
        self.assigned_class = None

    def SetClass(self, nc):
        self.assigned_class = nc


class _FakeNetInfo:
    def __init__(self, nets):
        self._nets = nets

    def NetsByName(self):
        # A plain dict provides keys() + __getitem__, which is all the helper uses.
        return self._nets


class _RichBoard:
    def __init__(self, path, net_names):
        self._path = path
        self._tracks = []
        self.saved_to = None
        self.net_classes = _FakeNetClasses()
        self._nets = {n: _FakeNet(n) for n in net_names}
        self.synchronized = 0

    def GetFileName(self):
        return self._path

    def GetTracks(self):
        return list(self._tracks)

    def Delete(self, t):
        self._tracks.remove(t)

    def Save(self, path):
        self.saved_to = path

    def GetNetClasses(self):
        return self.net_classes

    def GetNetInfo(self):
        return _FakeNetInfo(self._nets)

    def SynchronizeNetsAndNetClasses(self, *args):
        self.synchronized += 1

    def BuildListOfNets(self):
        pass

    def BuildConnectivity(self):
        pass


_KICAD_PRO = """{
  "net_settings": {
    "classes": [
      {"name": "Default", "track_width": 0.25, "clearance": 0.2},
      {"name": "Power", "track_width": 0.5, "clearance": 0.25},
      {"name": "USB", "track_width": 0.3, "clearance": 0.2}
    ],
    "netclass_assignments": {
      "+3V3": "Power",
      "+5V": "Power",
      "GND": "Power",
      "/USB_DP": "USB",
      "/USB_DM": "USB"
    }
  }
}
"""

SIMPLE_DSN = "(pcb\n)\n"
_SES = '(net "+3V3"\n  (wire (path F.Cu 500 0 0 1 1))\n)\n'


def _fake_pcbnew(dsn_text, load_board):
    pcb = MagicMock(name="pcbnew")
    captured = {"export_board": None, "power_class_at_export": None}

    def _export(board, dsn_path, *_a, **_k):
        captured["export_board"] = board
        # Snapshot the applied netclass so we prove it landed BEFORE export.
        power = board.net_classes.added.get("Power")
        captured["power_class_at_export"] = power.track_width if power is not None else None
        with open(dsn_path, "w") as fh:
            fh.write(dsn_text)
        return True

    pcb.ExportSpecctraDSN.side_effect = _export
    pcb.ImportSpecctraSES.return_value = True
    pcb.LoadBoard.return_value = load_board
    # Distinct NETCLASS objects per class name.
    pcb.NETCLASS.side_effect = lambda name: _FakeNetClass(name)
    return pcb, captured


def _fake_run_writes_ses(ses_text):
    def run(cmd, **_kw):
        do_idx = cmd.index("-do")
        with open(cmd[do_idx + 1], "w") as fh:
            fh.write(ses_text)
        return types.SimpleNamespace(returncode=0, stdout="INFO clean run\n", stderr="")

    return run


def _run(cc, fake_pcb, fake_run, params):
    with patch.object(fr_mod, "subprocess") as sp, patch.dict(sys.modules, {"pcbnew": fake_pcb}):
        sp.run.side_effect = fake_run
        sp.TimeoutExpired = TimeoutError
        return cc.autoroute(params)


@pytest.fixture()
def jar(tmp_path):
    p = tmp_path / "freerouting.jar"
    p.write_text("not a real jar")
    return p


@pytest.mark.unit
def test_autoroute_applies_project_netclass_widths_before_dsn_export(tmp_path, jar):
    board_path = tmp_path / "brd.kicad_pcb"
    board_path.write_text("(kicad_pcb)\n")
    (tmp_path / "brd.kicad_pro").write_text(_KICAD_PRO)

    reloaded = _RichBoard(str(board_path), ["+3V3", "+5V", "GND", "SIG1", "/USB_DP"])
    open_board = _RichBoard(str(board_path), [])  # the in-memory board

    cc = FreeroutingCommands(board=open_board)
    cc._resolve_execution_mode = MagicMock(return_value={"mode": "direct", "use_docker": False})

    fake_pcb, cap = _fake_pcbnew(SIMPLE_DSN, load_board=reloaded)
    out = _run(cc, fake_pcb, _fake_run_writes_ses(_SES), {"freeroutingJar": str(jar)})

    assert out["success"] is True, out

    # The DSN was exported from the freshly-loaded board.
    assert cap["export_board"] is reloaded
    # The Power class (0.5mm -> 500000 nm) was applied BEFORE the DSN export.
    assert cap["power_class_at_export"] == int(0.5 * _NM)

    # Netclass table carries every project class with the right widths.
    added = reloaded.net_classes.added
    assert added["Power"].track_width == int(0.5 * _NM)
    assert added["USB"].track_width == int(0.3 * _NM)
    assert added["Default"].track_width == int(0.25 * _NM)
    assert added["Power"].clearance == int(0.25 * _NM)

    # Power nets were assigned to Power; a signal net was left on Default.
    assert reloaded._nets["+3V3"].assigned_class is added["Power"]
    assert reloaded._nets["GND"].assigned_class is added["Power"]
    assert reloaded._nets["/USB_DP"].assigned_class is added["USB"]
    assert reloaded._nets["SIG1"].assigned_class is None
    assert reloaded.synchronized >= 1

    # And the response advertises what was applied.
    applied = out["netclasses_applied"]
    assert applied["applied"] is True
    assert applied["assignedNets"] == 4  # +3V3, +5V, GND, /USB_DP
    names = {c["name"] for c in applied["classes"]}
    assert {"Default", "Power", "USB"} <= names


@pytest.mark.unit
def test_autoroute_without_project_file_reports_not_applied(tmp_path, jar):
    """No sibling .kicad_pro -> the step is a no-op, not an error, and the
    response carries no netclasses_applied block."""
    board_path = tmp_path / "brd.kicad_pcb"
    board_path.write_text("(kicad_pcb)\n")
    # No .kicad_pro written.

    reloaded = _RichBoard(str(board_path), ["N1"])
    open_board = _RichBoard(str(board_path), [])

    cc = FreeroutingCommands(board=open_board)
    cc._resolve_execution_mode = MagicMock(return_value={"mode": "direct", "use_docker": False})

    fake_pcb, cap = _fake_pcbnew(SIMPLE_DSN, load_board=reloaded)
    out = _run(cc, fake_pcb, _fake_run_writes_ses(_SES), {"freeroutingJar": str(jar)})

    assert out["success"] is True, out
    assert "netclasses_applied" not in out
    assert reloaded.net_classes.added == {}


@pytest.mark.unit
def test_apply_project_netclasses_is_best_effort_on_odd_board(tmp_path):
    """A board whose GetNetClasses raises must not crash the helper."""
    board_path = tmp_path / "brd.kicad_pcb"
    board_path.write_text("(kicad_pcb)\n")
    (tmp_path / "brd.kicad_pro").write_text(_KICAD_PRO)

    board = MagicMock()
    board.GetFileName.return_value = str(board_path)
    board.GetNetClasses.side_effect = RuntimeError("no netclasses on this board")

    cc = FreeroutingCommands(board=None)
    summary = cc._apply_project_netclasses(board)
    assert summary["applied"] is False
    assert summary["assignedNets"] == 0
