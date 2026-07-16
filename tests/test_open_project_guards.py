"""Regression tests for E2E round 7 project-lifecycle findings.

C1  (major): open_project on a nonexistent / corrupt / wrong-extension file
             returned a misleading INTERNAL_ERROR ("loaded the board … restart
             the MCP server").  It must instead classify the input cleanly:
             FILE_NOT_FOUND / UNSUPPORTED_FILE / PARSE_ERROR, never claim the
             board loaded, and never advise restarting the server.
C2  (major): a FAILED open_project clobbered the currently-loaded board.  Open
             is transactional now — a failed open leaves the prior board intact.
C9  (minor): create_project silently turned an empty `name` into "New_Project";
             an explicitly-empty name must error INVALID_NAME.
C11 (minor): project.path meant different files across tools.  It is now always
             the .kicad_pro, with the .kicad_pcb reported separately as boardPath.

pcbnew is stubbed by tests/conftest.py as a MagicMock, so pcbnew.LoadBoard
returns a live-dispatch MagicMock (a "healthy" board) unless a test patches it.
"""

import os
from unittest.mock import patch

import pytest
from commands import project as project_mod
from commands.project import ProjectCommands


class _FakeTitleBlock:
    def GetTitle(self):
        return "Demo Title"

    def GetDate(self):
        return "2026-07-16"

    def GetRevision(self):
        return "A"

    def GetCompany(self):
        return "ACME"

    def GetComment(self, i):
        return f"comment{i}"


class _FakeBoard:
    """A loaded board that passes the SWIG-dehydration health probe."""

    def __init__(self, fn: str):
        self._fn = fn

    def GetFileName(self) -> str:
        return self._fn

    def SetFileName(self, fn: str) -> None:
        self._fn = fn

    def GetTitleBlock(self) -> _FakeTitleBlock:
        return _FakeTitleBlock()

    # Health-probe methods (commands.project._BOARD_HEALTH_METHODS).
    def GetDesignSettings(self):
        return object()

    def GetBoardEdgesBoundingBox(self):
        return object()


# ---------------------------------------------------------------------------
# C1: open_project input classification
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOpenProjectInputGuards:
    def _assert_not_misleading(self, result):
        blob = " ".join(str(v) for v in result.values()).lower()
        assert "restart" not in blob, result
        assert "loaded the board" not in blob, result

    def test_nonexistent_pro_is_file_not_found(self, tmp_path):
        missing = tmp_path / "nope" / "nothere.kicad_pro"
        result = ProjectCommands().open_project({"path": str(missing)})
        assert result["success"] is False
        assert result["errorCode"] == "FILE_NOT_FOUND"
        self._assert_not_misleading(result)

    def test_pro_exists_but_board_sibling_missing_is_file_not_found(self, tmp_path):
        # The .kicad_pro is present (e.g. corrupt project) but no .kicad_pcb.
        pro = tmp_path / "mini.kicad_pro"
        pro.write_text("{}", encoding="utf-8")
        result = ProjectCommands().open_project({"path": str(pro)})
        assert result["success"] is False
        assert result["errorCode"] == "FILE_NOT_FOUND"
        # Names the board file we actually needed.
        assert "mini.kicad_pcb" in result["message"]
        self._assert_not_misleading(result)

    def test_wrong_extension_is_unsupported_file(self, tmp_path):
        junk = tmp_path / "notes.txt"
        junk.write_text("hello", encoding="utf-8")
        result = ProjectCommands().open_project({"path": str(junk)})
        assert result["success"] is False
        assert result["errorCode"] == "UNSUPPORTED_FILE"
        self._assert_not_misleading(result)

    def test_corrupt_board_load_raises_is_parse_error(self, tmp_path):
        board = tmp_path / "bad.kicad_pcb"
        board.write_text("garbage-not-a-board", encoding="utf-8")
        with patch.object(
            project_mod.pcbnew, "LoadBoard", side_effect=RuntimeError("IO_ERROR: bad sexpr")
        ):
            result = ProjectCommands().open_project({"path": str(board)})
        assert result["success"] is False
        assert result["errorCode"] == "PARSE_ERROR"
        self._assert_not_misleading(result)

    def test_dehydrated_board_proxy_is_parse_error(self, tmp_path):
        board = tmp_path / "dehydrated.kicad_pcb"
        board.write_text("(kicad_pcb)\n", encoding="utf-8")
        # A raw object lacking the SWIG dispatch methods == dehydrated proxy.
        with patch.object(project_mod.pcbnew, "LoadBoard", return_value=object()):
            result = ProjectCommands().open_project({"path": str(board)})
        assert result["success"] is False
        assert result["errorCode"] == "PARSE_ERROR"
        self._assert_not_misleading(result)

    def test_valid_board_opens(self, tmp_path):
        pro = tmp_path / "ok.kicad_pro"
        pro.write_text("{}", encoding="utf-8")
        (tmp_path / "ok.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
        result = ProjectCommands().open_project({"path": str(pro)})
        assert result["success"] is True, result


# ---------------------------------------------------------------------------
# C2: a failed open must NOT discard the currently-loaded board
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOpenProjectTransactional:
    def test_failed_open_preserves_loaded_board(self, tmp_path):
        pc = ProjectCommands()
        loaded = _FakeBoard(str(tmp_path / "mini.kicad_pcb"))
        pc.board = loaded

        # A failed open on a nonexistent path.
        result = pc.open_project({"path": str(tmp_path / "gone" / "nothere.kicad_pro")})
        assert result["success"] is False
        assert result["errorCode"] == "FILE_NOT_FOUND"

        # The previously-loaded board is untouched (identity preserved).
        assert pc.board is loaded

        # get_project_info still reports the original project.
        info = pc.get_project_info({})
        assert info["success"] is True
        assert info["project"]["boardPath"] == str(tmp_path / "mini.kicad_pcb")

    def test_corrupt_open_preserves_loaded_board(self, tmp_path):
        pc = ProjectCommands()
        loaded = _FakeBoard(str(tmp_path / "mini.kicad_pcb"))
        pc.board = loaded

        board = tmp_path / "bad.kicad_pcb"
        board.write_text("garbage", encoding="utf-8")
        with patch.object(project_mod.pcbnew, "LoadBoard", side_effect=RuntimeError("bad board")):
            result = pc.open_project({"path": str(board)})

        assert result["success"] is False
        assert result["errorCode"] == "PARSE_ERROR"
        # Board preserved despite the failed open.
        assert pc.board is loaded


# ---------------------------------------------------------------------------
# C9: create_project rejects an explicitly-empty name
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateProjectEmptyName:
    def test_empty_name_is_invalid_name(self, tmp_path):
        target = tmp_path / "emptyname"
        result = ProjectCommands().create_project({"path": str(target), "name": ""})
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_NAME"
        # Nothing named "New_Project" was written.
        assert not (target / "New_Project.kicad_pro").exists()

    def test_whitespace_name_is_invalid_name(self, tmp_path):
        result = ProjectCommands().create_project({"path": str(tmp_path), "name": "   "})
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_NAME"

    def test_legacy_projectname_empty_is_invalid_name(self, tmp_path):
        result = ProjectCommands().create_project({"path": str(tmp_path), "projectName": ""})
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_NAME"

    def test_absent_name_still_defaults(self, tmp_path):
        # An OMITTED name (no key at all) keeps the historical default so
        # existing callers aren't broken — only an explicit empty is rejected.
        result = ProjectCommands().create_project({"path": str(tmp_path)})
        assert result["success"] is True, result
        assert result["project"]["name"] == "New_Project"

    def test_empty_name_with_pro_path_derives_name(self, tmp_path):
        # A .kicad_pro path still supplies the name even if `name` is empty.
        result = ProjectCommands().create_project(
            {"path": str(tmp_path / "Widget.kicad_pro"), "name": ""}
        )
        assert result["success"] is True, result
        assert result["project"]["name"] == "Widget"


# ---------------------------------------------------------------------------
# C11: project.path is always the .kicad_pro; boardPath is the .kicad_pcb
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProjectPathConsistency:
    def test_create_open_get_save_all_agree(self, tmp_path):
        # create_project
        created = ProjectCommands().create_project({"path": str(tmp_path), "name": "consistent"})
        assert created["success"] is True, created
        pro = str(tmp_path / "consistent.kicad_pro")
        pcb = str(tmp_path / "consistent.kicad_pcb")
        assert created["project"]["path"] == pro
        assert created["project"]["boardPath"] == pcb

        # open_project (via .kicad_pcb directly → still reports .kicad_pro path)
        (tmp_path / "consistent.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
        opened = ProjectCommands().open_project({"path": pcb})
        assert opened["success"] is True, opened
        assert opened["project"]["path"] == pro
        assert opened["project"]["boardPath"] == pcb

        # get_project_info
        pc = ProjectCommands()
        pc.board = _FakeBoard(pcb)
        info = pc.get_project_info({})
        assert info["success"] is True, info
        assert info["project"]["path"] == pro
        assert info["project"]["boardPath"] == pcb

        # save_project
        saved = pc.save_project({})
        assert saved["success"] is True, saved
        assert saved["project"]["path"] == pro
        assert saved["project"]["boardPath"] == pcb
        assert saved["savedPath"] == pcb
