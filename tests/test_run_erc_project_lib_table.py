"""Regression tests for run_erc honouring the project-local sym-lib-table.

kicad-cli ``sch erc`` only loads the *global* sym-lib-table; it does not
merge the project-local one the GUI uses. A schematic that places symbols
from a project-registered custom library therefore got a spurious
"symbol library 'X' is not in the current configuration" warning per
instance. ``handle_run_erc`` now writes a throwaway KiCad config home whose
global table = real global table + the project's libraries (URIs resolved,
nicknames deduped) and points ``KICAD_CONFIG_HOME`` at it for the ERC run.

These tests exercise the merge helper hermetically (monkeypatching the
global-table lookup) plus the handler wiring (env + response + cleanup).
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


_GLOBAL_TABLE = (
    "(sym_lib_table\n"
    "  (version 7)\n"
    '  (lib (name "KiCad")(type "KiCad")(uri "/usr/share/kicad/symbols")(options ""))\n'
    ")\n"
)


def _make_project(tmp_path: Path, lib_line: str) -> Path:
    """Create a project dir with a sym-lib-table and the referenced .kicad_sym."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "mylib.kicad_sym").write_text("(kicad_symbol_lib)\n", encoding="utf-8")
    (proj / "sym-lib-table").write_text(
        f"(sym_lib_table\n  (version 7)\n{lib_line}\n)\n", encoding="utf-8"
    )
    return proj


def _patch_global_table(monkeypatch, tmp_path: Path, version: str = "10.0") -> Path:
    """Point SymbolLibraryManager at a temp global table in a versioned config
    dir (kicad-cli only reads tables under a ``<major>.<minor>`` dir) and return
    its path. Also drops a kicad_common.json alongside it so tests can assert
    the whole config dir is copied (not just the table)."""
    from commands.library_symbol import SymbolLibraryManager

    gdir = tmp_path / version
    gdir.mkdir()
    gtable = gdir / "sym-lib-table"
    gtable.write_text(_GLOBAL_TABLE, encoding="utf-8")
    (gdir / "kicad_common.json").write_text('{"environment": {"vars": {}}}\n', encoding="utf-8")
    monkeypatch.setattr(
        SymbolLibraryManager,
        "_get_global_sym_lib_table",
        lambda self: gtable,
    )
    return gtable


def test_merge_adds_project_library(monkeypatch, tmp_path):
    from handlers.schematic_io import _build_project_lib_config_home

    _patch_global_table(monkeypatch, tmp_path)
    proj = _make_project(
        tmp_path,
        '  (lib (name "mylib")(type "KiCad")(uri "${KIPRJMOD}/mylib.kicad_sym")(options ""))',
    )

    result = _build_project_lib_config_home(proj)
    assert result is not None
    config_home, nicks = result
    try:
        assert nicks == ["mylib"]
        # version dir mirrors the real global table's parent name
        assert os.listdir(config_home) == ["10.0"]
        ver_dir = Path(config_home) / "10.0"
        merged = (ver_dir / "sym-lib-table").read_text(encoding="utf-8")
        # global lib preserved, project lib added with KIPRJMOD resolved to abs
        assert '(name "KiCad")' in merged
        assert '(name "mylib")' in merged
        assert "${KIPRJMOD}" not in merged
        assert str(proj / "mylib.kicad_sym") in merged
        # whole config dir copied — kicad_common.json (custom env vars) must
        # travel with it, else other global-table libs could fail to resolve.
        assert (ver_dir / "kicad_common.json").exists()
    finally:
        import shutil

        shutil.rmtree(config_home, ignore_errors=True)


def test_non_versioned_global_dir_returns_none(monkeypatch, tmp_path):
    """If the global table isn't under a <major>.<minor> dir, skip the merge:
    kicad-cli appends the version dir, so writing elsewhere would hide the
    table entirely (worse than the original false positives)."""
    from commands.library_symbol import SymbolLibraryManager
    from handlers.schematic_io import _build_project_lib_config_home

    gdir = tmp_path / "kicad"  # flat, non-versioned layout
    gdir.mkdir()
    (gdir / "sym-lib-table").write_text(_GLOBAL_TABLE, encoding="utf-8")
    monkeypatch.setattr(
        SymbolLibraryManager, "_get_global_sym_lib_table", lambda self: gdir / "sym-lib-table"
    )
    proj = _make_project(
        tmp_path,
        '  (lib (name "mylib")(type "KiCad")(uri "${KIPRJMOD}/mylib.kicad_sym")(options ""))',
    )
    assert _build_project_lib_config_home(proj) is None


def test_no_project_table_returns_none(monkeypatch, tmp_path):
    """A project without a sym-lib-table needs no merge — run ERC unchanged."""
    from handlers.schematic_io import _build_project_lib_config_home

    bare = tmp_path / "bare"
    bare.mkdir()
    assert _build_project_lib_config_home(bare) is None


def test_duplicate_nickname_not_merged(monkeypatch, tmp_path):
    """If the project nickname already exists in the global table, skip it so
    the merged table can't contain a duplicate (a hard error in KiCad)."""
    from handlers.schematic_io import _build_project_lib_config_home

    _patch_global_table(monkeypatch, tmp_path)
    # project re-declares the global "KiCad" nickname → nothing new to add
    proj = _make_project(
        tmp_path,
        '  (lib (name "KiCad")(type "KiCad")(uri "${KIPRJMOD}/mylib.kicad_sym")(options ""))',
    )
    assert _build_project_lib_config_home(proj) is None


def test_missing_global_table_returns_none(monkeypatch, tmp_path):
    """Without a discoverable global table we must NOT substitute a minimal
    one (it would hide kicad-cli's own global table); run ERC unchanged."""
    from commands.library_symbol import SymbolLibraryManager
    from handlers.schematic_io import _build_project_lib_config_home

    monkeypatch.setattr(SymbolLibraryManager, "_get_global_sym_lib_table", lambda self: None)
    proj = _make_project(
        tmp_path,
        '  (lib (name "mylib")(type "KiCad")(uri "${KIPRJMOD}/mylib.kicad_sym")(options ""))',
    )
    assert _build_project_lib_config_home(proj) is None


def test_handler_sets_config_home_and_reports_merge(monkeypatch, tmp_path):
    """End-to-end: handle_run_erc points KICAD_CONFIG_HOME at the merged config,
    surfaces project_lib_table, and cleans the temp dir up afterwards."""
    from handlers.schematic_io import handle_run_erc
    from kicad_interface import KiCADInterface

    _patch_global_table(monkeypatch, tmp_path)
    proj = _make_project(
        tmp_path,
        '  (lib (name "mylib")(type "KiCad")(uri "${KIPRJMOD}/mylib.kicad_sym")(options ""))',
    )
    sch = proj / "demo.kicad_sch"
    sch.write_text("(kicad_sch)\n", encoding="utf-8")

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.design_rule_commands = MagicMock()
    iface.design_rule_commands._find_kicad_cli = MagicMock(return_value="/fake/kicad-cli")

    captured = {}

    def _fake_run(cmd, **kw):
        captured["env"] = kw.get("env")
        out_path = cmd[cmd.index("--output") + 1]
        Path(out_path).write_text(json.dumps({"sheets": []}), encoding="utf-8")
        return MagicMock(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("subprocess.run", _fake_run)

    out = handle_run_erc(iface, {"schematicPath": str(sch), "autoRefreshLibSymbols": False})

    assert out["success"] is True
    # handler passed a merged config home to kicad-cli
    env = captured["env"]
    assert env is not None and "KICAD_CONFIG_HOME" in env
    cfg_home = env["KICAD_CONFIG_HOME"]
    assert "kicad-mcp-erc-cfg-" in cfg_home
    # response advertises the merge
    assert out["project_lib_table"]["merged"] is True
    assert out["project_lib_table"]["libraries"] == ["mylib"]
    # temp config home cleaned up after the run
    assert not os.path.isdir(cfg_home)


def test_export_netlist_merges_project_lib(monkeypatch, tmp_path):
    """export_netlist runs kicad-cli with the merged config so the netlist's
    <libraries> block includes project-scoped libs, then cleans the temp dir."""
    from handlers.schematic_io import handle_export_netlist
    from kicad_interface import KiCADInterface

    _patch_global_table(monkeypatch, tmp_path)
    proj = _make_project(
        tmp_path,
        '  (lib (name "mylib")(type "KiCad")(uri "${KIPRJMOD}/mylib.kicad_sym")(options ""))',
    )
    sch = proj / "demo.kicad_sch"
    sch.write_text("(kicad_sch)\n", encoding="utf-8")
    out_file = tmp_path / "out.net"

    iface = KiCADInterface.__new__(KiCADInterface)
    monkeypatch.setattr(
        KiCADInterface, "_find_kicad_cli_static", staticmethod(lambda: "/fake/kicad-cli")
    )

    captured = {}

    def _fake_run(cmd, **kw):
        captured["env"] = kw.get("env")
        out_path = cmd[cmd.index("--output") + 1]
        Path(out_path).write_text("(export)\n", encoding="utf-8")
        return MagicMock(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("subprocess.run", _fake_run)

    out = handle_export_netlist(iface, {"schematicPath": str(sch), "outputPath": str(out_file)})

    assert out["success"] is True
    env = captured["env"]
    assert env is not None and "KICAD_CONFIG_HOME" in env
    cfg_home = env["KICAD_CONFIG_HOME"]
    assert out["mergedProjectLibraries"] == ["mylib"]
    # temp config home cleaned up after the run
    assert not os.path.isdir(cfg_home)
