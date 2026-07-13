"""
Regression tests for E2E round 6 project-lifecycle findings.

S1  (major): create_project mangled a `path` that ends in `.kicad_pro`,
             left a stray directory, and then permanently blocked retries
             with a spurious PROJECT_EXISTS.
S15 (papercut): open_project only accepted `filename`; it must also accept
             `path` (a file or a directory containing one .kicad_pro).
Extra: save_project must state WHICH project path it saved, and honour an
             explicit save-as target passed as `path`.

pcbnew / skip are stubbed by tests/conftest.py; pcbnew.SaveBoard is a no-op
MagicMock, so a successful create writes a real .kicad_pro and .kicad_sch
(via plain open()) but no .kicad_pcb.
"""

import os
from unittest.mock import patch

import pytest
from commands import project as project_mod
from commands.project import ProjectCommands


class _FakeBoard:
    """Minimal stand-in for a pcbnew BOARD for save_project tests."""

    def __init__(self, fn: str):
        self._fn = fn

    def GetFileName(self) -> str:
        return self._fn

    def SetFileName(self, fn: str) -> None:
        self._fn = fn


# ---------------------------------------------------------------------------
# S1a: create_project with a path that ends in .kicad_pro
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateProjectPathEndsInKicadPro:
    def test_full_pro_path_is_split_not_doubled(self, tmp_path):
        """The exact bad call from the report now succeeds and produces the
        correct sibling paths — no doubled sub-path, no stray directory."""
        proj_dir = tmp_path / "gd32_radio"
        proj_dir.mkdir()
        bad_path = str(proj_dir / "gd32_radio.kicad_pro")

        result = ProjectCommands().create_project({"path": bad_path, "name": "gd32_radio"})

        assert result["success"] is True, result
        assert result["project"]["path"] == str(proj_dir / "gd32_radio.kicad_pro")
        assert result["project"]["boardPath"] == str(proj_dir / "gd32_radio.kicad_pcb")
        assert result["project"]["schematicPath"] == str(proj_dir / "gd32_radio.kicad_sch")
        # The real .kicad_pro must be a FILE, and there must be no stray dir.
        assert (proj_dir / "gd32_radio.kicad_pro").is_file()
        assert not (proj_dir / "gd32_radio.kicad_pro").is_dir()
        assert not (proj_dir / "gd32_radio.kicad_sch").is_dir()
        # No doubled ".kicad_pro/.kicad_pro" anywhere under the project dir.
        for dirpath, dirnames, _ in os.walk(proj_dir):
            assert "gd32_radio.kicad_pro" not in dirnames

    def test_derives_name_from_pro_path_when_name_omitted(self, tmp_path):
        pro_path = str(tmp_path / "Widget.kicad_pro")
        result = ProjectCommands().create_project({"path": pro_path})
        assert result["success"] is True, result
        assert result["project"]["name"] == "Widget"
        assert result["project"]["path"] == str(tmp_path / "Widget.kicad_pro")

    def test_conflicting_name_and_pro_path_errors(self, tmp_path):
        pro_path = str(tmp_path / "Alpha.kicad_pro")
        result = ProjectCommands().create_project({"path": pro_path, "name": "Beta"})
        assert result["success"] is False
        assert result["errorCode"] == "PROJECT_NAME_CONFLICT"
        # Nothing should have been written for the conflicting call.
        assert not (tmp_path / "Alpha.kicad_pro").exists()
        assert not (tmp_path / "Beta.kicad_pro").exists()

    def test_matching_name_with_extension_is_accepted(self, tmp_path):
        pro_path = str(tmp_path / "Gamma.kicad_pro")
        result = ProjectCommands().create_project(
            {"path": pro_path, "name": "Gamma.kicad_pro"}
        )
        assert result["success"] is True, result
        assert result["project"]["name"] == "Gamma"


# ---------------------------------------------------------------------------
# S1c: stray directory handling / retry unblocking
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateProjectStrayDirectory:
    def test_empty_stray_dir_is_reused_not_blocked(self, tmp_path):
        """An empty directory literally named <name>.kicad_pro (leftover from an
        earlier malformed call) must NOT trigger PROJECT_EXISTS — it is reused."""
        stray = tmp_path / "gd32_radio.kicad_pro"
        stray.mkdir()

        result = ProjectCommands().create_project(
            {"name": "gd32_radio", "path": str(tmp_path)}
        )

        assert result["success"] is True, result
        assert result.get("errorCode") != "PROJECT_EXISTS"
        # The stray dir was replaced by the real project FILE.
        assert stray.is_file()
        assert not stray.is_dir()

    def test_nonempty_stray_dir_is_a_distinct_error(self, tmp_path):
        stray = tmp_path / "gd32_radio.kicad_pro"
        stray.mkdir()
        (stray / "junk.txt").write_text("x", encoding="utf-8")

        result = ProjectCommands().create_project(
            {"name": "gd32_radio", "path": str(tmp_path)}
        )

        assert result["success"] is False
        assert result["errorCode"] == "PATH_IS_DIRECTORY"
        # The distinct error must NOT masquerade as PROJECT_EXISTS.
        assert result["errorCode"] != "PROJECT_EXISTS"
        # The directory and its contents are left untouched.
        assert stray.is_dir()
        assert (stray / "junk.txt").read_text(encoding="utf-8") == "x"

    def test_project_exists_only_fires_for_a_real_file(self, tmp_path):
        # A real .kicad_pro FILE blocks (PROJECT_EXISTS)...
        (tmp_path / "Board.kicad_pro").write_text("{}", encoding="utf-8")
        blocked = ProjectCommands().create_project({"name": "Board", "path": str(tmp_path)})
        assert blocked["errorCode"] == "PROJECT_EXISTS"

        # ...but a stray empty directory of that name does not.
        stray = tmp_path / "Other.kicad_pro"
        stray.mkdir()
        ok = ProjectCommands().create_project({"name": "Other", "path": str(tmp_path)})
        assert ok["success"] is True, ok


# ---------------------------------------------------------------------------
# S1b: partial artifacts are cleaned up on failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateProjectFailureCleanup:
    def test_directory_created_by_this_call_is_removed_on_failure(self, tmp_path):
        """If a write fails, a directory this call created must not linger and
        block the next (correct) attempt."""
        new_dir = tmp_path / "fresh_sub"
        assert not new_dir.exists()

        with patch.object(
            project_mod.pcbnew, "SaveBoard", side_effect=RuntimeError("boom")
        ):
            result = ProjectCommands().create_project(
                {"name": "Proj", "path": str(new_dir)}
            )

        assert result["success"] is False
        # The directory we created must have been cleaned up.
        assert not new_dir.exists(), "stray directory left behind after failure"

    def test_written_files_are_removed_on_failure(self, tmp_path):
        """A failure during the project-file write must roll back the schematic
        file this call already wrote."""
        new_dir = tmp_path / "fresh_sub2"

        with patch.object(project_mod.json, "dump", side_effect=RuntimeError("boom")):
            result = ProjectCommands().create_project(
                {"name": "Proj", "path": str(new_dir)}
            )

        assert result["success"] is False
        # Neither the schematic we wrote nor the created dir should remain.
        assert not (new_dir / "Proj.kicad_sch").exists()
        assert not new_dir.exists()


# ---------------------------------------------------------------------------
# S15: open_project accepts `path` (file or directory) as well as `filename`
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOpenProjectPathParam:
    def test_open_with_path_to_pro_file(self, tmp_path):
        pro = tmp_path / "Demo.kicad_pro"
        pro.write_text("{}", encoding="utf-8")

        result = ProjectCommands().open_project({"path": str(pro)})

        assert result["success"] is True, result
        assert result["project"]["path"] == str(pro)
        assert result["project"]["boardPath"] == str(tmp_path / "Demo.kicad_pcb")

    def test_open_with_path_to_directory_single_project(self, tmp_path):
        pro = tmp_path / "Demo.kicad_pro"
        pro.write_text("{}", encoding="utf-8")

        result = ProjectCommands().open_project({"path": str(tmp_path)})

        assert result["success"] is True, result
        assert result["project"]["path"] == str(pro)
        assert result["project"]["boardPath"] == str(tmp_path / "Demo.kicad_pcb")

    def test_open_directory_with_no_project_errors(self, tmp_path):
        result = ProjectCommands().open_project({"path": str(tmp_path)})
        assert result["success"] is False
        assert result["errorCode"] == "NO_PROJECT_IN_DIR"

    def test_open_directory_ambiguous_errors(self, tmp_path):
        (tmp_path / "A.kicad_pro").write_text("{}", encoding="utf-8")
        (tmp_path / "B.kicad_pro").write_text("{}", encoding="utf-8")

        result = ProjectCommands().open_project({"path": str(tmp_path)})

        assert result["success"] is False
        assert result["errorCode"] == "AMBIGUOUS_PROJECT"
        assert len(result["candidates"]) == 2

    def test_filename_still_works(self, tmp_path):
        pro = tmp_path / "Legacy.kicad_pro"
        pro.write_text("{}", encoding="utf-8")

        result = ProjectCommands().open_project({"filename": str(pro)})

        assert result["success"] is True, result
        assert result["project"]["boardPath"] == str(tmp_path / "Legacy.kicad_pcb")

    def test_missing_path_and_filename_errors(self):
        result = ProjectCommands().open_project({})
        assert result["success"] is False
        assert result["errorCode"] == "MISSING_PATH"


# ---------------------------------------------------------------------------
# Extra: save_project names the saved project + honours explicit path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSaveProjectNamesTarget:
    def test_response_states_which_project_was_saved(self, tmp_path):
        pc = ProjectCommands()
        pc.board = _FakeBoard(str(tmp_path / "hier.kicad_pcb"))

        result = pc.save_project({})

        assert result["success"] is True, result
        assert result["savedPath"] == str(tmp_path / "hier.kicad_pcb")
        assert result["project"]["path"] == str(tmp_path / "hier.kicad_pcb")
        assert str(tmp_path / "hier.kicad_pcb") in result["message"]

    def test_explicit_path_is_a_save_as_target(self, tmp_path):
        pc = ProjectCommands()
        pc.board = _FakeBoard(str(tmp_path / "orig.kicad_pcb"))

        # A .kicad_pro save-as target maps to its sibling .kicad_pcb.
        target = tmp_path / "elsewhere" / "renamed.kicad_pro"
        result = pc.save_project({"path": str(target)})

        assert result["success"] is True, result
        expected = str(tmp_path / "elsewhere" / "renamed.kicad_pcb")
        assert result["savedPath"] == expected
        assert pc.board.GetFileName() == expected

    def test_save_without_board_fails(self):
        result = ProjectCommands().save_project({})
        assert result["success"] is False
