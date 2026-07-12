"""
Regression tests for the create_project / create_schematic overwrite guard.

create_project (commands/project.py) and handle_create_schematic
(handlers/schematic_io.py) used to write their target files unconditionally,
silently clobbering an existing project/sheet on a name collision. They now
refuse unless overwrite=true. pcbnew / skip are stubbed by tests/conftest.py.
"""

import json
import os

import pytest
from commands.project import ProjectCommands


@pytest.mark.unit
class TestCreateProjectOverwriteGuard:
    def test_refuses_when_project_file_exists(self, tmp_path):
        existing = tmp_path / "Board.kicad_pro"
        existing.write_text("{}", encoding="utf-8")

        result = ProjectCommands().create_project({"name": "Board", "path": str(tmp_path)})

        assert result["success"] is False
        assert result["errorCode"] == "PROJECT_EXISTS"
        assert str(existing) in result["existingFiles"]
        # The pre-existing file must be left untouched.
        assert existing.read_text(encoding="utf-8") == "{}"

    def test_refuses_when_only_board_or_schematic_exists(self, tmp_path):
        # A stray sibling .kicad_pcb is enough to block (it would be overwritten).
        (tmp_path / "Board.kicad_pcb").write_text("stale", encoding="utf-8")

        result = ProjectCommands().create_project({"name": "Board", "path": str(tmp_path)})

        assert result["success"] is False
        assert result["errorCode"] == "PROJECT_EXISTS"

    def test_creates_fresh_project(self, tmp_path):
        result = ProjectCommands().create_project({"name": "Fresh", "path": str(tmp_path)})

        assert result["success"] is True, result
        assert os.path.exists(result["project"]["path"])

    def test_overwrite_true_bypasses_guard(self, tmp_path):
        (tmp_path / "Board.kicad_pro").write_text("{}", encoding="utf-8")

        result = ProjectCommands().create_project(
            {"name": "Board", "path": str(tmp_path), "overwrite": True}
        )

        assert result["success"] is True, result


@pytest.mark.unit
class TestCreateProjectFileContents:
    """B6/B7: create_project must write a faithful minimal .kicad_pro and a
    clean empty schematic (no preloaded dead lib_symbols)."""

    def test_kicad_pro_is_a_real_minimal_project(self, tmp_path):
        # B7: no more ~112-byte stub — a parseable KiCad 10 project with a
        # Default net class and > 1 KB of real content.
        res = ProjectCommands().create_project({"name": "Demo", "path": str(tmp_path)})
        assert res["success"] is True, res

        pro = tmp_path / "Demo.kicad_pro"
        assert pro.exists()
        assert pro.stat().st_size > 1024, "project file should be a real document, not a stub"

        data = json.loads(pro.read_text(encoding="utf-8"))
        classes = {c["name"]: c for c in data["net_settings"]["classes"]}
        assert "Default" in classes
        assert "clearance" in classes["Default"]
        assert data["meta"]["filename"] == "Demo.kicad_pro"

    def test_schematic_has_no_preloaded_lib_symbols(self, tmp_path):
        # B6: the dynamic symbol loader injects lib_symbols on demand, so a
        # fresh schematic must start with an empty lib_symbols block and zero
        # placed instances — no dead LM358/Crystal/… defs.
        res = ProjectCommands().create_project({"name": "Demo", "path": str(tmp_path)})
        assert res["success"] is True, res

        sch = (tmp_path / "Demo.kicad_sch").read_text(encoding="utf-8")
        assert sch.count('(symbol "') == 0, "no lib_symbol definitions should be preloaded"
        assert sch.count("(lib_id") == 0, "no components should be placed"
        assert "(lib_symbols\n  )" in sch


@pytest.mark.unit
class TestCreateSchematicOverwriteGuard:
    def test_refuses_when_schematic_exists(self, tmp_path):
        from handlers.schematic_io import handle_create_schematic

        existing = tmp_path / "Sheet.kicad_sch"
        existing.write_text("(kicad_sch)", encoding="utf-8")

        result = handle_create_schematic(None, {"name": "Sheet", "path": str(tmp_path)})

        assert result["success"] is False
        assert result["errorCode"] == "SCHEMATIC_EXISTS"
        # Untouched.
        assert existing.read_text(encoding="utf-8") == "(kicad_sch)"
