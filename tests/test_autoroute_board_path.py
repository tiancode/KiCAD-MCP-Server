"""Regression tests for the autoroute board-path / DSN fixes (E2E round 7).

Covers the P9 findings:

  * B5 (critical) — autoroute IGNORED ``boardPath``: it exported the DSN from
    and imported the SES into the in-memory board, then saved to ``boardPath``.
    A boardPath naming a different file therefore routed the WRONG board and
    could clobber the target file. The fix loads the target file FRESH and
    routes THAT board:
      - external boardPath  -> route the file, leave the open board untouched,
                               emit a note + routed_board_path, no callbacks;
      - same / omitted path -> flush the open board, reload it afterward;
      - nonexistent path    -> FILE_NOT_FOUND.
  * B6 — Freerouting 2.2.4 StackOverflows on a DSN carrying pre-routed
    ``(wiring …)`` + full-board ``(plane …)``. They are stripped from the DSN
    handed to the router by default (gated by includePreRoutes/includePlanes),
    never from the .kicad_pcb.
  * B7 — a fresh LoadBoard re-reads the sibling .kicad_pro netclasses, so the
    exported DSN carries the real Power/RF (class …) widths; stripping never
    eats those class blocks.
  * B8 — connectivity is rebuilt between ImportSpecctraSES and the save so a
    DRC run immediately after import doesn't over-count by one.

Freerouting itself is never invoked — the subprocess + pcbnew are mocked.
"""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from commands import freerouting as fr_mod  # noqa: E402
from commands.freerouting import (  # noqa: E402
    FreeroutingCommands,
    _remove_sexpr_blocks,
    _strip_dsn_prerouting,
)

# ---------------------------------------------------------------------------
# Fakes
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
    """BOARD stand-in that records Save calls and mutates its track list."""

    def __init__(self, tracks, path):
        self._tracks = list(tracks)
        self._path = path
        self.saved_to = None
        self.save_count = 0

    def GetTracks(self):
        return list(self._tracks)

    def GetFileName(self):
        return self._path

    def Delete(self, t):
        self._tracks.remove(t)

    def Save(self, path):
        self.saved_to = path
        self.save_count += 1


def _fake_pcbnew(dsn_text, load_board=None):
    """pcbnew stub whose ExportSpecctraDSN writes ``dsn_text`` and records the
    board it was handed; LoadBoard returns ``load_board``."""
    pcb = MagicMock(name="pcbnew")
    captured = {"export_board": None, "dsn_path": None}

    def _export(board, dsn_path, *_a, **_k):
        captured["export_board"] = board
        captured["dsn_path"] = dsn_path
        with open(dsn_path, "w") as fh:
            fh.write(dsn_text)
        return True

    pcb.ExportSpecctraDSN.side_effect = _export
    pcb.ImportSpecctraSES.return_value = True
    if load_board is not None:
        pcb.LoadBoard.return_value = load_board
    return pcb, captured


def _fake_run_writes_ses(ses_text, stdout="INFO clean run\n"):
    def run(cmd, **_kw):
        do_idx = cmd.index("-do")
        ses_path = cmd[do_idx + 1]
        with open(ses_path, "w") as fh:
            fh.write(ses_text)
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    return run


def _make_cmds(board, tmp_path, signature_callback=None, board_reload_callback=None):
    cc = FreeroutingCommands(
        board=board,
        signature_callback=signature_callback,
        board_reload_callback=board_reload_callback,
    )
    cc._resolve_execution_mode = MagicMock(return_value={"mode": "direct", "use_docker": False})
    return cc


@pytest.fixture()
def jar(tmp_path):
    p = tmp_path / "freerouting.jar"
    p.write_text("not a real jar")
    return p


def _run(cc, fake_pcb, fake_run, params):
    with patch.object(fr_mod, "subprocess") as sp, patch.dict(sys.modules, {"pcbnew": fake_pcb}):
        sp.run.side_effect = fake_run
        sp.TimeoutExpired = TimeoutError
        return cc.autoroute(params)


SIMPLE_DSN = "(pcb\n)\n"
_SES_A = '(net "/A"\n  (wire (path F.Cu 200 0 0 1 1))\n)\n'


# ---------------------------------------------------------------------------
# B5 — autoroute honours boardPath (routes the file named, not self.board)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_autoroute_external_boardpath_routes_that_file_leaves_open_board(tmp_path, jar):
    """boardPath names a DIFFERENT file -> that file is loaded fresh, routed and
    saved; the open project board is never touched (no reload, no signature)."""
    loaded = _FakeBoard([_FakeTrack("INMEM")], str(tmp_path / "loaded.kicad_pcb"))
    ext_path = tmp_path / "other.kicad_pcb"
    ext_path.write_text("(kicad_pcb)\n")
    ext_board = _FakeBoard([], str(ext_path))

    sig_cb = MagicMock()
    reload_cb = MagicMock()
    cc = _make_cmds(loaded, tmp_path, signature_callback=sig_cb, board_reload_callback=reload_cb)

    fake_pcb, cap = _fake_pcbnew(SIMPLE_DSN, load_board=ext_board)
    out = _run(
        cc,
        fake_pcb,
        _fake_run_writes_ses(_SES_A),
        {"boardPath": str(ext_path), "freeroutingJar": str(jar)},
    )

    assert out["success"] is True
    # DSN exported from the FRESH external board, not the in-memory one (B5).
    assert cap["export_board"] is ext_board
    # The open project board is untouched: object identity + never saved.
    assert cc.board is loaded
    assert loaded.saved_to is None
    # External file was the one written.
    assert ext_board.saved_to == str(ext_path)
    # No reload / no signature callback for an external file (decision 4).
    reload_cb.assert_not_called()
    sig_cb.assert_not_called()
    assert out["routed_board_path"] == str(ext_path)
    assert "not modified" in out["note"]


@pytest.mark.unit
def test_autoroute_nonexistent_boardpath_is_file_not_found(tmp_path, jar):
    loaded = _FakeBoard([], str(tmp_path / "loaded.kicad_pcb"))
    cc = _make_cmds(loaded, tmp_path)
    fake_pcb, cap = _fake_pcbnew(SIMPLE_DSN)

    out = _run(
        cc,
        fake_pcb,
        _fake_run_writes_ses(_SES_A),
        {"boardPath": str(tmp_path / "nope.kicad_pcb"), "freeroutingJar": str(jar)},
    )

    assert out["success"] is False
    assert out["errorCode"] == "FILE_NOT_FOUND"
    # Never attempted to load or export anything.
    fake_pcb.LoadBoard.assert_not_called()
    assert cap["export_board"] is None


@pytest.mark.unit
def test_autoroute_same_file_flushes_then_reloads(tmp_path, jar):
    """boardPath == the open board's file -> flush it (signature callback),
    route a fresh load, then reload the parent (rebinds handlers)."""
    path = tmp_path / "board.kicad_pcb"
    path.write_text("(kicad_pcb)\n")
    board = _FakeBoard([], str(path))
    reloaded = _FakeBoard([], str(path))

    sig_cb = MagicMock()
    reload_cb = MagicMock(return_value=True)
    cc = _make_cmds(board, tmp_path, signature_callback=sig_cb, board_reload_callback=reload_cb)

    fake_pcb, cap = _fake_pcbnew(SIMPLE_DSN, load_board=reloaded)
    out = _run(
        cc,
        fake_pcb,
        _fake_run_writes_ses(_SES_A),
        {"boardPath": str(path), "freeroutingJar": str(jar)},
    )

    assert out["success"] is True
    # Pre-flush of the in-memory board fired the signature callback.
    sig_cb.assert_called_once_with(str(path))
    assert board.saved_to == str(path)
    # Routed the fresh reload, not the stale in-memory board.
    assert cap["export_board"] is reloaded
    # Same-file => reload the parent; no external note.
    reload_cb.assert_called_once_with(str(path))
    assert out["routed_board_path"] == str(path)
    assert "note" not in out


@pytest.mark.unit
def test_autoroute_no_boardpath_uses_open_board_file_and_reloads(tmp_path, jar):
    path = tmp_path / "board.kicad_pcb"
    path.write_text("(kicad_pcb)\n")
    board = _FakeBoard([], str(path))
    reloaded = _FakeBoard([], str(path))

    reload_cb = MagicMock(return_value=True)
    cc = _make_cmds(board, tmp_path, board_reload_callback=reload_cb)

    fake_pcb, cap = _fake_pcbnew(SIMPLE_DSN, load_board=reloaded)
    out = _run(
        cc,
        fake_pcb,
        _fake_run_writes_ses(_SES_A),
        {"freeroutingJar": str(jar)},  # no boardPath
    )

    assert out["success"] is True
    assert cap["export_board"] is reloaded
    reload_cb.assert_called_once_with(str(path))
    assert out["routed_board_path"] == str(path)


# ---------------------------------------------------------------------------
# B6 / B7 — DSN pre-routing / plane stripping preserves netclasses
# ---------------------------------------------------------------------------


MULTICLASS_DSN = """(pcb "board.dsn"
  (parser
    (string_quote ")
    (space_in_quoted_tokens on)
    (host_cad "KiCad's Pcbnew")
  )
  (structure
    (layer F.Cu (type signal))
    (layer B.Cu (type signal))
    (boundary (path pcb 0  0 0  80000 0  80000 -60000  0 -60000  0 0))
    (plane /GND (polygon F.Cu 0  0 0  80000 0  80000 -60000  0 -60000  0 0))
    (plane /GND (polygon B.Cu 0  0 0  80000 0  80000 -60000  0 -60000  0 0))
  )
  (network
    (net /GND (pins U1-1 U1-2))
    (net /+3V3 (pins U1-3))
    (net /ANT (pins U1-4))
    (class kicad_default /SWDIO /SWCLK
      (circuit (use_via "Via[0-1]_600:300_um"))
      (rule (width 250) (clearance 200))
    )
    (class Power /+3V3 /GND /VBUS
      (circuit (use_via "Via[0-1]_600:300_um"))
      (rule (width 500) (clearance 200))
    )
    (class RF /ANT
      (circuit (use_via "Via[0-1]_600:300_um"))
      (rule (width 400) (clearance 200))
    )
  )
  (wiring
    (wire (path F.Cu 500  44250 -20250  44000 -20000)(net /+3V3)(type route))
    (wire (path F.Cu 250  32750 -40500  33500 -39750)(net /SWDIO)(type route))
    (via "Via[0-1]_600:300_um"  40000 -50000 (net /GND)(type route))
  )
)
"""


@pytest.mark.unit
def test_strip_survives_string_quote_header():
    """Round-7 live-smoke regression: every real KiCad DSN starts with a
    ``(parser (string_quote ") …)`` block whose lone literal ``"`` must not be
    treated as a string delimiter — naive quote toggling left the scanner
    stuck in-quote and turned the whole strip into a silent no-op."""
    assert '(string_quote ")' in MULTICLASS_DSN
    out, info = _strip_dsn_prerouting(MULTICLASS_DSN)
    assert info["wiring_removed"] is True
    assert info["planes_removed"] == 2
    # The parser header itself is preserved untouched.
    assert '(string_quote ")' in out
    assert "(space_in_quoted_tokens on)" in out


@pytest.mark.unit
def test_strip_removes_wiring_and_planes_by_default_keeps_classes():
    out, info = _strip_dsn_prerouting(MULTICLASS_DSN)
    assert "(wiring" not in out
    assert "(plane " not in out
    assert info["wiring_removed"] is True
    assert info["planes_removed"] == 2
    # B7: every netclass — and its Power/RF widths — survives stripping.
    assert "(class kicad_default" in out
    assert "(class Power" in out and "(width 500)" in out
    assert "(class RF" in out and "(width 400)" in out
    # The (net …) declarations and boundary are untouched too.
    assert "(net /GND" in out
    assert "(boundary" in out


@pytest.mark.unit
def test_strip_keeps_wiring_when_include_pre_routes():
    out, info = _strip_dsn_prerouting(MULTICLASS_DSN, include_pre_routes=True)
    assert "(wiring" in out
    assert info["wiring_removed"] is False
    # Planes still stripped (independent gate).
    assert "(plane " not in out
    assert info["planes_removed"] == 2


@pytest.mark.unit
def test_strip_keeps_planes_when_include_planes():
    out, info = _strip_dsn_prerouting(MULTICLASS_DSN, include_planes=True)
    assert "(plane " in out
    assert info["planes_removed"] == 0
    assert "(wiring" not in out
    assert info["wiring_removed"] is True


@pytest.mark.unit
def test_strip_noop_when_both_included():
    out, info = _strip_dsn_prerouting(MULTICLASS_DSN, include_pre_routes=True, include_planes=True)
    assert out == MULTICLASS_DSN
    assert info == {"wiring_removed": False, "planes_removed": 0}


@pytest.mark.unit
def test_strip_is_quote_aware_and_token_bounded():
    # A class net name carries parens INSIDE quotes; the plane matcher must not
    # unbalance on them, and a hypothetical "(planet …)" token must be left be.
    dsn = (
        "(structure\n"
        "  (plane /GND (polygon F.Cu 0  0 0  1 1))\n"
        "  (planet_keepme (foo))\n"
        ")\n"
        "(network\n"
        '  (class kicad_default "unconnected-(J1-CC1-PadA5)" /GND\n'
        "    (rule (width 250))\n"
        "  )\n"
        ")\n"
        "(wiring\n"
        "  (wire (path F.Cu 250  0 0  1 1)(net /GND)(type route))\n"
        ")\n"
    )
    out, info = _strip_dsn_prerouting(dsn)
    assert "(wiring" not in out
    assert "(plane " not in out
    assert info["planes_removed"] == 1
    # The quoted net (with parens) survived intact inside its class.
    assert '"unconnected-(J1-CC1-PadA5)"' in out
    assert "(class kicad_default" in out
    # Token boundary: (planet_keepme …) is NOT a plane.
    assert "(planet_keepme" in out


@pytest.mark.unit
def test_remove_sexpr_blocks_counts_and_trims():
    text = "a\n(wiring\n  (wire 1)\n)\nb\n"
    out, n = _remove_sexpr_blocks(text, "wiring")
    assert n == 1
    assert "(wiring" not in out
    assert "a\n" in out and "b\n" in out


@pytest.mark.unit
def test_autoroute_strips_prerouting_from_the_dsn_fed_to_freerouting(tmp_path, jar):
    """End-to-end: the DSN Freerouting sees has no pre-routed wiring but KEEPS
    the copper planes and the Power/RF netclasses. Live round-7 testing showed
    the (wiring …) block alone triggers the 2.2.4 crash, while stripping the
    planes turns the GND tree into a trace-routing job that times out — so
    planes stay by default (includePlanes=false strips them)."""
    path = tmp_path / "board.kicad_pcb"
    path.write_text("(kicad_pcb)\n")
    board = _FakeBoard([], str(path))
    cc = _make_cmds(board, tmp_path, board_reload_callback=MagicMock(return_value=True))

    fake_pcb, cap = _fake_pcbnew(MULTICLASS_DSN, load_board=board)
    out = _run(
        cc,
        fake_pcb,
        _fake_run_writes_ses(_SES_A),
        {"boardPath": str(path), "freeroutingJar": str(jar)},
    )
    assert out["success"] is True

    dsn_on_disk = open(cap["dsn_path"]).read()
    assert "(wiring" not in dsn_on_disk
    assert "(plane " in dsn_on_disk  # planes kept by default
    assert "(class Power" in dsn_on_disk and "(width 500)" in dsn_on_disk
    assert "(class RF" in dsn_on_disk and "(width 400)" in dsn_on_disk
    assert out["dsn_prerouting_stripped"] == {"wiring_removed": True, "planes_removed": 0}


@pytest.mark.unit
def test_autoroute_include_planes_false_strips_planes(tmp_path, jar):
    """includePlanes=false opts into stripping the (plane …) entries too."""
    path = tmp_path / "board.kicad_pcb"
    path.write_text("(kicad_pcb)\n")
    board = _FakeBoard([], str(path))
    cc = _make_cmds(board, tmp_path, board_reload_callback=MagicMock(return_value=True))

    fake_pcb, cap = _fake_pcbnew(MULTICLASS_DSN, load_board=board)
    out = _run(
        cc,
        fake_pcb,
        _fake_run_writes_ses(_SES_A),
        {"boardPath": str(path), "freeroutingJar": str(jar), "includePlanes": False},
    )
    assert out["success"] is True
    dsn_on_disk = open(cap["dsn_path"]).read()
    assert "(plane " not in dsn_on_disk
    assert out["dsn_prerouting_stripped"]["planes_removed"] == 2


@pytest.mark.unit
def test_autoroute_include_pre_routes_keeps_wiring_in_dsn(tmp_path, jar):
    path = tmp_path / "board.kicad_pcb"
    path.write_text("(kicad_pcb)\n")
    board = _FakeBoard([], str(path))
    cc = _make_cmds(board, tmp_path, board_reload_callback=MagicMock(return_value=True))

    fake_pcb, cap = _fake_pcbnew(MULTICLASS_DSN, load_board=board)
    out = _run(
        cc,
        fake_pcb,
        _fake_run_writes_ses(_SES_A),
        {
            "boardPath": str(path),
            "freeroutingJar": str(jar),
            "includePreRoutes": True,
            "includePlanes": True,
        },
    )
    assert out["success"] is True
    dsn_on_disk = open(cap["dsn_path"]).read()
    assert "(wiring" in dsn_on_disk
    assert "(plane " in dsn_on_disk
    # No stripping happened, so no report field.
    assert "dsn_prerouting_stripped" not in out


# ---------------------------------------------------------------------------
# B8 — connectivity rebuilt between SES import and save
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_apply_ses_rebuilds_connectivity_between_import_and_save(tmp_path):
    order = []
    board = MagicMock(name="board")
    board.GetTracks.return_value = []
    board.BuildListOfNets.side_effect = lambda: order.append("build_nets")
    board.BuildConnectivity.side_effect = lambda: order.append("build_conn")
    board.Save.side_effect = lambda p: order.append("save")

    ses = tmp_path / "x.ses"
    ses.write_text(_SES_A)

    fake_pcb = MagicMock(name="pcbnew")
    fake_pcb.ImportSpecctraSES.side_effect = lambda b, p: order.append("import") or True

    cc = FreeroutingCommands(board=board)
    with patch.dict(sys.modules, {"pcbnew": fake_pcb}):
        res = cc._apply_ses(
            str(ses), str(tmp_path / "x.kicad_pcb"), board=board, fire_signature=False
        )

    assert res["ok"] is True
    # B8: BuildConnectivity runs AFTER the import and BEFORE the save so the
    # saved board is already settled (no transient +1 DRC marker).
    assert order.index("import") < order.index("build_conn") < order.index("save")


@pytest.mark.unit
def test_apply_ses_skips_connectivity_rebuild_when_unavailable(tmp_path):
    """A board without Build* methods must not raise (hasattr-guarded)."""
    board = _FakeBoard([], str(tmp_path / "x.kicad_pcb"))
    ses = tmp_path / "x.ses"
    ses.write_text(_SES_A)

    fake_pcb = MagicMock(name="pcbnew")
    fake_pcb.ImportSpecctraSES.return_value = True

    cc = FreeroutingCommands(board=board)
    with patch.dict(sys.modules, {"pcbnew": fake_pcb}):
        res = cc._apply_ses(
            str(ses), str(tmp_path / "x.kicad_pcb"), board=board, fire_signature=False
        )
    assert res["ok"] is True
    assert board.saved_to == str(tmp_path / "x.kicad_pcb")
