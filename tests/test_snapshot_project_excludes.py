"""Regression: snapshot_project must not copy transient artifacts (and must
render the board to PDF, C5).

Phase C E2E found (1) snapshots bloated with KiCad lock files (~*.lck), the
MCP's own .mcp-backups/ and .history/ dirs, etc. — the snapshot must exclude
those while keeping the real design files; and (2) the tool + README promised
"renders board to PDF" but no PDF was ever produced.  The handler now does a
best-effort kicad-cli render into the snapshot dir, exposes a `pdf` field, and
degrades truthfully via `pdfNote` when it can't render.
"""

import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

import utils.kicad_cli as kcli  # noqa: E402
from handlers.project import handle_snapshot_project  # noqa: E402


def _build_fake_project(root: Path) -> Path:
    proj = root / "myproj"
    proj.mkdir()
    # Real design files — must be kept.
    (proj / "myproj.kicad_pcb").write_text("(kicad_pcb)")
    (proj / "myproj.kicad_sch").write_text("(kicad_sch)")
    (proj / "myproj.kicad_pro").write_text("{}")
    # Transient artifacts — must be excluded.
    (proj / "~myproj.kicad_pro.lck").write_text("lock")
    (proj / ".history").mkdir()
    (proj / ".history" / "old.kicad_pcb").write_text("old")
    (proj / ".mcp-backups").mkdir()
    (proj / ".mcp-backups" / "backup.kicad_pcb").write_text("bak")
    (proj / "__pycache__").mkdir()
    (proj / "__pycache__" / "x.pyc").write_text("")
    (proj / ".git").mkdir()
    (proj / ".git" / "config").write_text("[core]")
    return proj


@pytest.mark.unit
def test_snapshot_excludes_transient_artifacts_keeps_design(tmp_path, monkeypatch):
    # Keep the PDF render off the real kicad-cli in this unit test — we only
    # care about file exclusion here.  find_kicad_cli()->None degrades cleanly
    # (no subprocess), leaving the snapshot copy untouched.
    monkeypatch.setattr(kcli, "find_kicad_cli", lambda: None)

    proj = _build_fake_project(tmp_path)
    iface = types.SimpleNamespace(board=None)

    result = handle_snapshot_project(iface, {"projectPath": str(proj)})
    assert result["success"], result
    # PDF fields are always present in the response shape.
    assert "pdf" in result and "pdfNote" in result
    snap = Path(result["snapshotPath"])
    assert snap.is_dir()

    # Design files preserved.
    assert (snap / "myproj.kicad_pcb").exists()
    assert (snap / "myproj.kicad_sch").exists()
    assert (snap / "myproj.kicad_pro").exists()

    # Transient artifacts excluded.
    assert not (snap / "~myproj.kicad_pro.lck").exists()
    assert not (snap / ".history").exists()
    assert not (snap / ".mcp-backups").exists()
    assert not (snap / "__pycache__").exists()
    assert not (snap / ".git").exists()

    # The snapshots dir itself is never recursively copied.
    assert not (snap / "snapshots").exists()


@pytest.mark.unit
def test_snapshot_renders_pdf_into_snapshot_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(kcli, "find_kicad_cli", lambda: "kicad-cli")
    monkeypatch.setattr(kcli, "c_locale_env", lambda *a, **k: {})

    def _fake_run(cmd, **kwargs):
        # `... export pdf --output <file> --mode-single --layers <...> <board>`
        out = cmd[cmd.index("--output") + 1]
        Path(out).write_text("%PDF-1.5\n")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    proj = _build_fake_project(tmp_path)
    iface = types.SimpleNamespace(board=None)

    result = handle_snapshot_project(iface, {"projectPath": str(proj)})
    assert result["success"], result

    assert result["pdf"] is not None
    assert result["pdfNote"] is None
    pdf = Path(result["pdf"])
    assert pdf.is_file() and pdf.suffix == ".pdf"
    # The PDF lives inside the snapshot directory (a self-contained checkpoint).
    assert pdf.parent == Path(result["snapshotPath"])


@pytest.mark.unit
def test_snapshot_pdf_degrades_when_cli_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(kcli, "find_kicad_cli", lambda: None)

    proj = _build_fake_project(tmp_path)
    iface = types.SimpleNamespace(board=None)

    result = handle_snapshot_project(iface, {"projectPath": str(proj)})
    assert result["success"], result
    # Snapshot copy still succeeds; PDF degrades truthfully.
    assert result["pdf"] is None
    assert result["pdfNote"] and "kicad-cli" in result["pdfNote"]


@pytest.mark.unit
def test_snapshot_pdf_degrades_when_no_board_saved(tmp_path, monkeypatch):
    # cli is available, but the project has no .kicad_pcb to render.
    monkeypatch.setattr(kcli, "find_kicad_cli", lambda: "kicad-cli")
    monkeypatch.setattr(kcli, "c_locale_env", lambda *a, **k: {})

    proj = tmp_path / "noboard"
    proj.mkdir()
    (proj / "noboard.kicad_pro").write_text("{}")
    (proj / "noboard.kicad_sch").write_text("(kicad_sch)")
    iface = types.SimpleNamespace(board=None)

    result = handle_snapshot_project(iface, {"projectPath": str(proj)})
    assert result["success"], result
    assert result["pdf"] is None
    assert result["pdfNote"] and "kicad_pcb" in result["pdfNote"]
