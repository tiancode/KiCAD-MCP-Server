"""Regression: snapshot_project must not copy transient artifacts.

Phase C E2E found snapshots bloated with KiCad lock files (~*.lck), the MCP's
own .mcp-backups/ and .history/ dirs, etc.  The snapshot must exclude those
while keeping the real design files.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from handlers.project import handle_snapshot_project


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
def test_snapshot_excludes_transient_artifacts_keeps_design(tmp_path):
    proj = _build_fake_project(tmp_path)
    iface = types.SimpleNamespace(board=None)

    result = handle_snapshot_project(iface, {"projectPath": str(proj)})
    assert result["success"], result
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
