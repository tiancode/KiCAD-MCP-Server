"""
Unit tests for create_schematic path parameter bug fix.

Verifies that create_schematic respects the `path` argument and writes
the schematic file to the correct directory instead of the process cwd.
"""

import importlib.util
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

# pcbnew and skip are only available inside KiCAD — stub them so the
# schematic module can be imported in a plain Python environment.
sys.modules.setdefault("pcbnew", MagicMock())
sys.modules.setdefault("skip", MagicMock())

# Import the module directly (bypasses python/commands/__init__.py which
# would otherwise pull in board/component commands that also need pcbnew).
_spec = importlib.util.spec_from_file_location(
    "schematic_module",
    os.path.join(os.path.dirname(__file__), "..", "python", "commands", "schematic.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
SchematicManager = _mod.SchematicManager

_OPEN_MOCK = MagicMock(
    return_value=MagicMock(
        __enter__=MagicMock(
            return_value=MagicMock(
                read=MagicMock(return_value="(uuid 00000000-0000-0000-0000-000000000000)")
            )
        ),
        __exit__=MagicMock(return_value=False),
    )
)


def test_create_schematic_uses_path_argument():
    """
    create_schematic should write the .kicad_sch file inside `path`
    when that argument is provided, not in the process working directory.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        with (
            patch.object(_mod, "Schematic") as mock_sch_cls,
            patch("shutil.copy"),
            patch("os.path.exists", return_value=True),
            patch("builtins.open", _OPEN_MOCK),
        ):
            mock_sch_cls.return_value = MagicMock()

            SchematicManager.create_schematic("myschematic", path=tmpdir)

            used_path = mock_sch_cls.call_args[0][0]
            assert used_path.startswith(
                tmpdir
            ), f"Expected path inside {tmpdir!r}, got {used_path!r}"
            assert used_path.endswith("myschematic.kicad_sch")


def test_create_schematic_without_path_uses_relative():
    """
    When no path is given, behaviour is unchanged — file goes to cwd-relative name.
    """
    with (
        patch.object(_mod, "Schematic") as mock_sch_cls,
        patch("shutil.copy"),
        patch("os.path.exists", return_value=True),
        patch("builtins.open", _OPEN_MOCK),
    ):
        mock_sch_cls.return_value = MagicMock()

        SchematicManager.create_schematic("myschematic")

        used_path = mock_sch_cls.call_args[0][0]
        assert used_path == "myschematic.kicad_sch"


def test_create_schematic_accepts_full_sch_filename():
    """
    If name already ends with .kicad_sch, it should not double the suffix.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        with (
            patch.object(_mod, "Schematic") as mock_sch_cls,
            patch("shutil.copy"),
            patch("os.path.exists", return_value=True),
            patch("builtins.open", _OPEN_MOCK),
        ):
            mock_sch_cls.return_value = MagicMock()

            SchematicManager.create_schematic("myschematic.kicad_sch", path=tmpdir)

            used_path = mock_sch_cls.call_args[0][0]
            assert used_path.endswith("myschematic.kicad_sch")
            assert "myschematic.kicad_sch.kicad_sch" not in used_path


# ---------------------------------------------------------------------------
# A7 (handler layer) — create_schematic must accept a full .kicad_sch FILE path
# in `path` instead of treating it as a directory and double-appending
# <name>.kicad_sch (".../scratch.kicad_sch/scratch.kicad_sch" -> FILE_NOT_FOUND).
# These exercise handle_create_schematic end-to-end (real file writes).
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))


def _handler():
    from handlers.schematic_io import handle_create_schematic

    return handle_create_schematic


def test_handler_full_file_path_in_path_not_double_appended():
    with tempfile.TemporaryDirectory() as tmpdir:
        target = os.path.join(tmpdir, "scratch.kicad_sch")
        res = _handler()(None, {"name": "scratch", "path": target})
        assert res["success"] is True
        assert res["file_path"] == target
        assert os.path.isfile(target)
        # No doubled ".../scratch.kicad_sch/scratch.kicad_sch" directory.
        assert not os.path.exists(os.path.join(target, "scratch.kicad_sch"))


def test_handler_full_file_path_without_name():
    with tempfile.TemporaryDirectory() as tmpdir:
        target = os.path.join(tmpdir, "board.kicad_sch")
        res = _handler()(None, {"path": target})
        assert res["success"] is True
        assert res["file_path"] == target
        assert os.path.isfile(target)


def test_handler_name_basename_conflict_refused():
    with tempfile.TemporaryDirectory() as tmpdir:
        target = os.path.join(tmpdir, "alpha.kicad_sch")
        res = _handler()(None, {"name": "beta", "path": target})
        assert res["success"] is False
        assert res["errorCode"] == "SCHEMATIC_NAME_CONFLICT"
        assert not os.path.exists(target)


def test_handler_name_agreeing_with_basename_accepted():
    with tempfile.TemporaryDirectory() as tmpdir:
        target = os.path.join(tmpdir, "same.kicad_sch")
        res = _handler()(None, {"name": "same", "path": target})
        assert res["success"] is True
        assert res["file_path"] == target
        assert os.path.isfile(target)


def test_handler_directory_path_still_works():
    with tempfile.TemporaryDirectory() as tmpdir:
        res = _handler()(None, {"name": "sheet1", "path": tmpdir})
        assert res["success"] is True
        assert res["file_path"] == os.path.join(tmpdir, "sheet1.kicad_sch")
        assert os.path.isfile(os.path.join(tmpdir, "sheet1.kicad_sch"))
