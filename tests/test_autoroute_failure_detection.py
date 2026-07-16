"""Tests for autoroute honest-failure detection + SES replace-semantics.

Regression coverage for E2E finding B4: Freerouting 2.2.4 can throw mid-run
(the ``to_trace_entries`` NPE on boards with pre-routed traces), log
``ERROR Error during routing passes``, **exit 0**, and still write an echo
SES — the old code reported ``success: True`` and the SES import duplicated
the pre-routed traces.  These tests pin:

  - ``_detect_routing_failure`` catches the fatal signatures but never
    false-positives on normal INFO/WARN routing chatter.
  - ``_ses_routed_nets`` reports only nets that carry a wire/via.
  - ``_apply_ses`` REPLACES routing on the SES's nets (removes old tracks
    on exactly those nets, leaves other nets untouched).
  - ``autoroute`` returns ``success: False`` + a hint when a crashed run
    routes 0 new nets, imports + flags ``routing_incomplete`` on a partial
    crash, and never lets a crashed pass win best-of-N.

Style mirrors test_autoroute_best_of_n.py (mocked subprocess + pcbnew).
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))

from commands import freerouting as fr_mod  # noqa: E402
from commands.freerouting import (  # noqa: E402
    FreeroutingCommands,
    _detect_routing_failure,
    _ses_routed_nets,
)

# ---------------------------------------------------------------------------
# Captured real Freerouting v2.2.4 output snippets (from the B4 repro)
# ---------------------------------------------------------------------------

NPE_STDOUT = """2026-07-12 17:16:02.089 INFO   Freerouting v2.2.4 (build-date: 2026-05-13)
2026-07-12 17:16:03.253 INFO   Opening 'board.dsn'...
2026-07-12 17:16:04.044 WARN   DSN file 'board.dsn' was loaded with 1 warning(s).
2026-07-12 17:16:10.368 INFO   [76B5AF] Starting routing of 'board' on 15 threads...
2026-07-12 17:16:13.740 ERROR  Error during routing passes
java.lang.NullPointerException: Cannot load from object array because "to_trace_entries" is null
\tat app.freerouting.board.ShapeSearchTree.merge_entries_in_front(ShapeSearchTree.java:175)
\tat app.freerouting.board.SearchTreeManager.merge_entries_in_front(SearchTreeManager.java:245)
2026-07-12 17:16:14.711 INFO   [76B5AF] Auto-router session completed: started with 1 unrouted nets, completed in 4.34 seconds, final score: 993.13 (1 unrouted).
2026-07-12 17:16:18.296 INFO   Saving 'board.ses'...
"""

CLEAN_STDOUT = """2026-07-12 17:18:15.837 INFO   Freerouting v2.2.4 (build-date: 2026-05-13)
WARNING: Final field userProfileSettings in class app.freerouting.settings.GlobalSettings has been mutated reflectively
WARNING: Use --enable-final-field-mutation=ALL-UNNAMED to avoid a warning
2026-07-12 17:18:17.008 INFO   Opening 'board.dsn'...
2026-07-12 17:18:17.044 WARN   DSN file 'board.dsn' was loaded with 1 warning(s).
2026-07-12 17:18:24.055 INFO   [3DC38C] Starting routing of 'board' on 15 threads...
2026-07-12 17:18:26.452 INFO   [3DC38C] Auto-router pass #1 was completed in 2.29 seconds with the score of 939.03 (8 unrouted).
2026-07-12 17:18:28.284 INFO   [3DC38C] Auto-router session completed: started with 37 unrouted nets, completed in 4.23 seconds, final score: 983.85 (2 unrouted).
2026-07-12 17:18:28.831 WARN   [3DC38C] after autoroute: 4 traces not 45 degree
2026-07-12 17:18:29.355 INFO   Saving 'board.ses'...
"""


# ---------------------------------------------------------------------------
# _detect_routing_failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_detect_failure_catches_npe_and_returns_exception_line():
    err = _detect_routing_failure(NPE_STDOUT)
    assert err is not None
    assert "NullPointerException" in err
    assert "to_trace_entries" in err


@pytest.mark.unit
def test_detect_failure_none_on_clean_output():
    # A successful run — even with WARN lines and "warning(s)" text — must not
    # trip the detector.
    assert _detect_routing_failure(CLEAN_STDOUT) is None


@pytest.mark.unit
def test_detect_failure_none_on_empty_output():
    assert _detect_routing_failure("") is None
    assert _detect_routing_failure(None) is None  # type: ignore[arg-type]


@pytest.mark.unit
def test_detect_failure_catches_error_during_passes_without_stack():
    # Even if the Java stack trace were suppressed, the ERROR line is fatal.
    txt = "INFO Starting\n2026 ERROR  Error during routing passes\nINFO done\n"
    err = _detect_routing_failure(txt)
    assert err is not None
    assert "Error during routing passes" in err


@pytest.mark.unit
def test_detect_failure_catches_out_of_memory():
    txt = 'Exception in thread "main" java.lang.OutOfMemoryError: Java heap space\n'
    err = _detect_routing_failure(txt)
    assert err is not None
    assert "OutOfMemoryError" in err


@pytest.mark.unit
def test_detect_failure_catches_stackoverflow():
    # E2E finding B6: the pre-routed + zoned GD32 DSN makes Freerouting 2.2.4
    # throw a StackOverflowError in its DSN "Opening" phase. The detector must
    # catch it (the safety net that complements DSN stripping).
    txt = (
        "2026-07-13 12:00:00.000 INFO   Opening 'board.dsn'...\n"
        "2026-07-13 12:00:00.100 ERROR  Error during routing passes\n"
        "java.lang.StackOverflowError\n"
        "\tat app.freerouting.geometry.planar.Simplex.to_IntOctagon(Simplex.java:512)\n"
    )
    # A crash is detected (non-None). The first fatal line returned is the
    # ERROR summary (StackOverflowError carries no ": message", so it isn't
    # preferred), but the java signature itself is matched too.
    assert _detect_routing_failure(txt) is not None
    assert (
        _detect_routing_failure("java.lang.StackOverflowError\n") == "java.lang.StackOverflowError"
    )


# ---------------------------------------------------------------------------
# _ses_routed_nets
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ses_routed_nets_only_counts_nets_with_wire_or_via():
    ses = (
        '(net "IO7"\n  (wire (path F.Cu 200 0 0 1 1))\n)\n'
        '(net "EMPTY_NET"\n)\n'  # present but no wire/via -> must be ignored
        '(net "GND"\n  (via "V" 100 200)\n)\n'
    )
    nets = _ses_routed_nets(ses)
    assert nets == {"IO7", "GND"}
    assert "EMPTY_NET" not in nets


@pytest.mark.unit
def test_ses_routed_nets_strips_quotes_and_handles_spaces():
    ses = '(net "USB_D-"\n  (wire (path F.Cu 200 0 0 1 1))\n)\n'
    assert _ses_routed_nets(ses) == {"USB_D-"}


@pytest.mark.unit
def test_ses_routed_nets_empty_text():
    assert _ses_routed_nets("") == set()


# ---------------------------------------------------------------------------
# Replace-semantics: _apply_ses
# ---------------------------------------------------------------------------


class _FakeTrack:
    def __init__(self, net, is_via=False):
        self._net = net
        self._is_via = is_via

    def GetNetname(self):
        return self._net

    def GetClass(self):
        return "PCB_VIA" if self._is_via else "PCB_TRACK"


class _FakeBoard:
    """Minimal BOARD stand-in whose Delete actually mutates the track list."""

    def __init__(self, tracks, path):
        self._tracks = list(tracks)
        self._path = path
        self.saved_to = None

    def GetTracks(self):
        return list(self._tracks)

    def GetFileName(self):
        return self._path

    def Delete(self, t):
        self._tracks.remove(t)

    def Save(self, path):
        self.saved_to = path


def _cmds_with_board(board):
    cc = FreeroutingCommands.__new__(FreeroutingCommands)
    cc.board = board
    cc._signature_callback = None
    return cc


@pytest.mark.unit
def test_apply_ses_replaces_tracks_on_ses_nets_only(tmp_path):
    """Pre-seed net X (2 tracks) + net Y (1 track); import a SES routing X.

    X's old tracks must be removed and the imported X track added (single set,
    no duplication); Y must be untouched.
    """
    board = _FakeBoard(
        [_FakeTrack("X"), _FakeTrack("X"), _FakeTrack("Y")],
        str(tmp_path / "b.kicad_pcb"),
    )
    ses = tmp_path / "b.ses"
    ses.write_text('(net "X"\n  (wire (path F.Cu 200 0 0 1 1))\n)\n')

    fake_pcb = MagicMock(name="pcbnew")

    def _import(b, _ses_path):
        # Model Freerouting's import: adds fresh routing for net X.
        b._tracks.append(_FakeTrack("X"))
        return True

    fake_pcb.ImportSpecctraSES.side_effect = _import

    cc = _cmds_with_board(board)
    with patch.dict(sys.modules, {"pcbnew": fake_pcb}):
        res = cc._apply_ses(str(ses), str(tmp_path / "b.kicad_pcb"))

    assert res["ok"] is True
    assert res["removed_tracks"] == 2  # both old X tracks ripped
    assert res["replaced_nets"] == ["X"]
    nets = [t.GetNetname() for t in board.GetTracks()]
    assert nets.count("X") == 1, "X must be a single (replaced) set, not duplicated"
    assert nets.count("Y") == 1, "net Y (absent from SES) must be untouched"
    assert board.saved_to == str(tmp_path / "b.kicad_pcb")


@pytest.mark.unit
def test_apply_ses_import_failure_bubbles_error(tmp_path):
    board = _FakeBoard([_FakeTrack("X")], str(tmp_path / "b.kicad_pcb"))
    ses = tmp_path / "b.ses"
    ses.write_text('(net "X"\n  (wire (path F.Cu 200 0 0 1 1))\n)\n')

    fake_pcb = MagicMock(name="pcbnew")
    fake_pcb.ImportSpecctraSES.side_effect = Exception("import boom")

    cc = _cmds_with_board(board)
    with patch.dict(sys.modules, {"pcbnew": fake_pcb}):
        res = cc._apply_ses(str(ses), str(tmp_path / "b.kicad_pcb"))

    assert res["ok"] is False
    assert res["error"]["success"] is False
    assert "import boom" in res["error"]["errorDetails"]


# ---------------------------------------------------------------------------
# autoroute — full pipeline failure handling
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_jar(tmp_path):
    p = tmp_path / "freerouting.jar"
    p.write_text("not a real jar")
    return p


def _patch_exec_mode(cc):
    cc._resolve_execution_mode = MagicMock(return_value={"mode": "direct", "use_docker": False})


def _autoroute_pcbnew():
    """pcbnew stub: ExportSpecctraDSN writes a placeholder; ImportSpecctraSES
    is a tracked MagicMock (default returns True)."""
    pcb = MagicMock(name="pcbnew")

    def _fake_export(_board, dsn_path, *_a, **_k):
        with open(dsn_path, "w") as f:
            f.write("  (wiring\n  )\n")
        return True

    pcb.ExportSpecctraDSN.side_effect = _fake_export
    pcb.ImportSpecctraSES.return_value = True
    return pcb


def _run_autoroute(cc, workdir, fake_jar, fake_run, fake_pcb, **params):
    # B5: autoroute now routes a FRESH pcbnew.LoadBoard of the target file.
    # boardPath == cc.board's own file (the same-file case), so point LoadBoard
    # back at cc.board — the _FakeBoard the assertions inspect.
    fake_pcb.LoadBoard.return_value = cc.board
    with patch.object(fr_mod, "subprocess") as sp, patch.dict(sys.modules, {"pcbnew": fake_pcb}):
        sp.run.side_effect = fake_run
        sp.TimeoutExpired = TimeoutError
        args = {
            "boardPath": str(workdir / "test.kicad_pcb"),
            "freeroutingJar": str(fake_jar),
        }
        args.update(params)
        return cc.autoroute(args)


@pytest.mark.unit
def test_autoroute_crash_zero_new_nets_fails_honestly(tmp_path, fake_jar):
    """NPE + SES echoes only pre-routed nets -> success:False, hint, no import."""
    workdir = tmp_path
    (workdir / "test.kicad_pcb").write_text("(kicad_pcb)\n")
    ses_path = workdir / "test.ses"
    # Board is pre-routed on IO7 + IO10 (the exact B4 scenario).
    board = _FakeBoard(
        [_FakeTrack("IO7"), _FakeTrack("IO10"), _FakeTrack("IO10")],
        str(workdir / "test.kicad_pcb"),
    )
    cc = _cmds_with_board(board)
    _patch_exec_mode(cc)

    def fake_run(cmd, **kw):
        # Echo SES: only the pre-existing nets, nothing new. Freerouting exits 0.
        ses_path.write_text(
            '(net "IO7"\n  (wire (path F.Cu 200 0 0 1 1))\n)\n'
            '(net "IO10"\n  (wire (path F.Cu 200 0 0 1 1))\n)\n'
        )
        return types.SimpleNamespace(returncode=0, stdout=NPE_STDOUT, stderr="")

    fake_pcb = _autoroute_pcbnew()
    out = _run_autoroute(cc, workdir, fake_jar, fake_run, fake_pcb)

    assert out["success"] is False
    assert "0 nets routed" in out["message"]
    assert "NullPointerException" in out["errorDetails"]
    assert "hint" in out and "pre-routed" in out["hint"]
    assert out["freerouting_error"]
    # Board must be left exactly as it was — no import, no duplication.
    fake_pcb.ImportSpecctraSES.assert_not_called()
    assert len(board.GetTracks()) == 3


@pytest.mark.unit
def test_autoroute_partial_crash_imports_and_flags_incomplete(tmp_path, fake_jar):
    """NPE but a NEW net got routed -> import with warnings + routing_incomplete."""
    workdir = tmp_path
    (workdir / "test.kicad_pcb").write_text("(kicad_pcb)\n")
    ses_path = workdir / "test.ses"
    board = _FakeBoard([_FakeTrack("IO7")], str(workdir / "test.kicad_pcb"))
    cc = _cmds_with_board(board)
    _patch_exec_mode(cc)

    def fake_run(cmd, **kw):
        # Echo IO7 (pre-routed) + a genuinely NEW net IO99.
        ses_path.write_text(
            '(net "IO7"\n  (wire (path F.Cu 200 0 0 1 1))\n)\n'
            '(net "IO99"\n  (wire (path F.Cu 200 0 0 1 1))\n)\n'
        )
        return types.SimpleNamespace(returncode=0, stdout=NPE_STDOUT, stderr="")

    fake_pcb = _autoroute_pcbnew()
    out = _run_autoroute(cc, workdir, fake_jar, fake_run, fake_pcb)

    assert out["success"] is True
    assert out["routing_incomplete"] is True
    assert out["newly_routed_nets"] == ["IO99"]
    assert any("fatal error" in w for w in out["warnings"])
    fake_pcb.ImportSpecctraSES.assert_called_once()
    # IO7 (in the SES) was ripped before import (replace semantics).
    assert out["replaced_existing_tracks"] == 1


@pytest.mark.unit
def test_autoroute_clean_run_reports_replace_stats(tmp_path, fake_jar):
    """A clean run imports with replace-semantics and reports nets_routed."""
    workdir = tmp_path
    (workdir / "test.kicad_pcb").write_text("(kicad_pcb)\n")
    ses_path = workdir / "test.ses"
    board = _FakeBoard([], str(workdir / "test.kicad_pcb"))
    cc = _cmds_with_board(board)
    _patch_exec_mode(cc)

    def fake_run(cmd, **kw):
        ses_path.write_text(
            '(net "IO1"\n  (wire (path F.Cu 200 0 0 1 1))\n)\n'
            '(net "IO2"\n  (wire (path F.Cu 200 0 0 1 1))\n)\n'
        )
        return types.SimpleNamespace(returncode=0, stdout=CLEAN_STDOUT, stderr="")

    fake_pcb = _autoroute_pcbnew()
    out = _run_autoroute(cc, workdir, fake_jar, fake_run, fake_pcb)

    assert out["success"] is True
    assert "attempts" not in out  # single-attempt shape preserved
    assert out["nets_routed"] == 2
    assert out["replaced_existing_tracks"] == 0
    fake_pcb.ImportSpecctraSES.assert_called_once()


# ---------------------------------------------------------------------------
# Best-of-N interaction: a crashed pass must not win
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_crashed_pass_does_not_win_best_of_n(tmp_path, fake_jar):
    """Attempt 2 crashes but has MORE nets; the clean attempt 1 must still win."""
    workdir = tmp_path
    (workdir / "test.kicad_pcb").write_text("(kicad_pcb)\n")
    ses_path = workdir / "test.ses"
    board = _FakeBoard([], str(workdir / "test.kicad_pcb"))
    cc = _cmds_with_board(board)
    _patch_exec_mode(cc)

    calls = {"n": 0}

    def fake_run(cmd, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            # Crashed pass writes a fat SES (9 nets) but logs the NPE.
            ses_path.write_text(
                "".join(f'(net "N{i}"\n  (wire (path F.Cu 200 0 0 1 1))\n)\n' for i in range(9))
            )
            return types.SimpleNamespace(returncode=0, stdout=NPE_STDOUT, stderr="")
        # Clean passes: 3 nets.
        ses_path.write_text(
            "".join(f'(net "C{i}"\n  (wire (path F.Cu 200 0 0 1 1))\n)\n' for i in range(3))
        )
        return types.SimpleNamespace(returncode=0, stdout=CLEAN_STDOUT, stderr="")

    fake_pcb = _autoroute_pcbnew()
    out = _run_autoroute(cc, workdir, fake_jar, fake_run, fake_pcb, attempts=3)

    assert out["success"] is True
    assert out["best_attempt"] in (1, 3), "a clean attempt must win, never the crash (#2)"
    crashed = next(a for a in out["attempts"] if a["attempt"] == 2)
    assert crashed["ok"] is False
    assert "routing_error" in crashed
    # The imported SES is a clean one (3 C-nets), not the crashed 9-net SES.
    assert out["nets_routed"] == 3


@pytest.mark.unit
def test_all_attempts_crash_zero_new_fails(tmp_path, fake_jar):
    """Every attempt crashes with an echo-only SES -> total failure."""
    workdir = tmp_path
    (workdir / "test.kicad_pcb").write_text("(kicad_pcb)\n")
    ses_path = workdir / "test.ses"
    board = _FakeBoard([_FakeTrack("IO7")], str(workdir / "test.kicad_pcb"))
    cc = _cmds_with_board(board)
    _patch_exec_mode(cc)

    def fake_run(cmd, **kw):
        ses_path.write_text('(net "IO7"\n  (wire (path F.Cu 200 0 0 1 1))\n)\n')
        return types.SimpleNamespace(returncode=0, stdout=NPE_STDOUT, stderr="")

    fake_pcb = _autoroute_pcbnew()
    out = _run_autoroute(cc, workdir, fake_jar, fake_run, fake_pcb, attempts=3)

    assert out["success"] is False
    assert "0 nets routed" in out["message"]
    fake_pcb.ImportSpecctraSES.assert_not_called()
