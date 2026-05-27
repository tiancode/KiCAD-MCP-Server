"""Regression tests for refresh_schematic_lib_symbols.

User report: KiCad library was upgraded but the schematic's embedded
``lib_symbols`` snapshot is stale — kicad-cli ERC fires
``lib_symbol_mismatch`` on every affected symbol.  This tool walks the
schematic, re-extracts each Library:Name from the current ``.kicad_sym``
on disk, and rewrites the embedded block.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


# A minimal .kicad_sym containing one symbol with one easily-identifiable
# property (Description) — the test mutates this to simulate a library upgrade.
_LIB_FRESH = """\
(kicad_symbol_lib
  (version 20231120)
  (generator kicad_symbol_editor)
  (symbol "R"
    (property "Reference" "R" (at 0 0 0))
    (property "Value" "R" (at 0 0 0))
    (property "Description" "Resistor (UPDATED)" (at 0 0 0))
    (symbol "R_0_1"
      (rectangle (start -1.016 -2.54) (end 1.016 2.54)
        (stroke (width 0.254) (type default))
        (fill (type none)))
    )
    (symbol "R_1_1"
      (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
      (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))
    )
  )
)
"""


_SCHEMATIC_WITH_STALE_R = """\
(kicad_sch
  (version 20231120)
  (generator eeschema)
  (lib_symbols
    (symbol "Device:R"
      (property "Reference" "R" (at 0 0 0))
      (property "Value" "R" (at 0 0 0))
      (property "Description" "Resistor (OLD STALE COPY)" (at 0 0 0))
      (symbol "Device:R_0_1"
        (rectangle (start -1.016 -2.54) (end 1.016 2.54)
          (stroke (width 0.254) (type default))
          (fill (type none)))
      )
      (symbol "Device:R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))
      )
    )
  )
)
"""


def _setup_project(tmp_path):
    """Build a fake project tree with a Device library + a stale schematic.

    Returns (schematic_path, library_path).  The loader picks the
    library path via ``find_kicad_symbol_libraries`` — to keep the test
    hermetic we monkeypatch that method to point at our tmp dir.
    """
    lib_dir = tmp_path / "symbols"
    lib_dir.mkdir()
    lib_path = lib_dir / "Device.kicad_sym"
    lib_path.write_text(_LIB_FRESH, encoding="utf-8")

    sch_path = tmp_path / "demo.kicad_sch"
    sch_path.write_text(_SCHEMATIC_WITH_STALE_R, encoding="utf-8")
    return sch_path, lib_dir


def _make_loader(tmp_path, lib_dir, monkeypatch):
    """Build a DynamicSymbolLoader that finds our fixture library."""
    from commands.dynamic_symbol_loader import DynamicSymbolLoader

    loader = DynamicSymbolLoader(project_path=tmp_path)
    monkeypatch.setattr(
        loader,
        "find_kicad_symbol_libraries",
        lambda: [lib_dir],
    )
    return loader


def test_refresh_replaces_stale_embedded_copy(monkeypatch, tmp_path):
    """The stale Description in the embedded snapshot is replaced by the
    one from the current Device.kicad_sym."""
    sch_path, lib_dir = _setup_project(tmp_path)
    loader = _make_loader(tmp_path, lib_dir, monkeypatch)

    out = loader.refresh_embedded_lib_symbols(sch_path)

    assert out["success"] is True
    assert out["refreshed"] == ["Device:R"]
    assert out["unchanged"] == []
    assert out["missing"] == []
    rewritten = sch_path.read_text(encoding="utf-8")
    assert "Resistor (UPDATED)" in rewritten
    assert "Resistor (OLD STALE COPY)" not in rewritten


def test_refresh_skips_unchanged_entries(monkeypatch, tmp_path):
    """When the embedded snapshot already matches disk, the entry is
    reported as ``unchanged`` and the file isn't rewritten."""
    sch_path, lib_dir = _setup_project(tmp_path)
    # Make the embedded copy match disk exactly: refresh once, then
    # refresh again — the second call must report ``unchanged``.
    loader1 = _make_loader(tmp_path, lib_dir, monkeypatch)
    loader1.refresh_embedded_lib_symbols(sch_path)
    mtime_after_first = sch_path.stat().st_mtime_ns

    # Second loader, same fixture — disk and schematic now agree.
    loader2 = _make_loader(tmp_path, lib_dir, monkeypatch)
    out = loader2.refresh_embedded_lib_symbols(sch_path)

    assert out["success"] is True
    assert out["refreshed"] == []
    assert out["unchanged"] == ["Device:R"]
    # No-op refresh must not touch the file.
    assert sch_path.stat().st_mtime_ns == mtime_after_first


def test_refresh_reports_missing_libraries(monkeypatch, tmp_path):
    """A symbol whose library can't be located is listed in ``missing``;
    the rest of the schematic is untouched."""
    sch_path, lib_dir = _setup_project(tmp_path)
    # Point the loader at an empty directory so Device.kicad_sym isn't
    # findable; the refresh should report ``missing`` and bail
    # gracefully.
    empty = tmp_path / "empty_symbols"
    empty.mkdir()
    from commands.dynamic_symbol_loader import DynamicSymbolLoader

    loader = DynamicSymbolLoader(project_path=tmp_path)
    monkeypatch.setattr(loader, "find_kicad_symbol_libraries", lambda: [empty])

    out = loader.refresh_embedded_lib_symbols(sch_path)

    assert out["success"] is True
    assert out["refreshed"] == []
    assert out["missing"] == ["Device:R"]
    # File should NOT have been rewritten — the stale copy survives.
    assert "Resistor (OLD STALE COPY)" in sch_path.read_text(encoding="utf-8")


def test_refresh_handles_schematic_without_lib_symbols(tmp_path):
    """A schematic without any ``(lib_symbols ...)`` block is a no-op."""
    sch_path = tmp_path / "empty.kicad_sch"
    sch_path.write_text("(kicad_sch (version 20231120))\n", encoding="utf-8")
    from commands.dynamic_symbol_loader import DynamicSymbolLoader

    loader = DynamicSymbolLoader(project_path=tmp_path)
    out = loader.refresh_embedded_lib_symbols(sch_path)

    assert out["success"] is True
    assert out["refreshed"] == []
    assert out["unchanged"] == []
    assert out["missing"] == []
    assert "No lib_symbols" in out["message"]


# ---------------------------------------------------------------------------
# Handler wraps loader + finds the project root
# ---------------------------------------------------------------------------
def test_handler_returns_loader_result(monkeypatch, tmp_path):
    from handlers.schematic_component import handle_refresh_schematic_lib_symbols

    sch_path, lib_dir = _setup_project(tmp_path)
    (tmp_path / "demo.kicad_pro").write_text("(kicad_project)\n", encoding="utf-8")

    # Monkeypatch the loader's library-lookup so the handler resolves to
    # our fixture without needing real KiCad installed.
    import commands.dynamic_symbol_loader as dsl

    real_init = dsl.DynamicSymbolLoader.__init__

    def _patched_init(self, project_path=None):
        real_init(self, project_path=project_path)
        self.find_kicad_symbol_libraries = lambda: [lib_dir]

    monkeypatch.setattr(dsl.DynamicSymbolLoader, "__init__", _patched_init)

    out = handle_refresh_schematic_lib_symbols(iface=None, params={"schematicPath": str(sch_path)})

    assert out["success"] is True
    assert "Device:R" in out["refreshed"]


def test_handler_requires_schematic_path():
    from handlers.schematic_component import handle_refresh_schematic_lib_symbols

    out = handle_refresh_schematic_lib_symbols(iface=None, params={})

    assert out["success"] is False
    assert "schematicPath" in out["message"]


def test_handler_refuses_missing_file():
    from handlers.schematic_component import handle_refresh_schematic_lib_symbols

    out = handle_refresh_schematic_lib_symbols(
        iface=None, params={"schematicPath": "/nonexistent/file.kicad_sch"}
    )

    assert out["success"] is False
    assert "not found" in out["message"].lower()


# ---------------------------------------------------------------------------
# ERC surfaces refresh_lib_symbols recommendation on lib_symbol_mismatch
# ---------------------------------------------------------------------------
def test_erc_recommends_refresh_when_lib_symbol_mismatch_present(monkeypatch, tmp_path):
    import json as _json
    from unittest.mock import MagicMock

    from handlers.schematic_io import handle_run_erc
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.design_rule_commands = MagicMock()
    iface.design_rule_commands._find_kicad_cli = MagicMock(return_value="/fake/kicad-cli")

    sch = tmp_path / "demo.kicad_sch"
    sch.write_text("(kicad_sch)\n", encoding="utf-8")

    erc = {
        "sheets": [
            {
                "violations": [
                    {
                        "description": "Symbol 'R' doesn't match copy in library 'Device'",
                        "severity": "warning",
                        "type": "lib_symbol_mismatch",
                        "items": [{"pos": {"x": 1.0, "y": 1.0}}],
                    },
                    {
                        "description": "Symbol 'C' doesn't match copy in library 'Device'",
                        "severity": "warning",
                        "type": "lib_symbol_mismatch",
                        "items": [{"pos": {"x": 1.0, "y": 1.0}}],
                    },
                ]
            }
        ],
    }

    def _fake_subprocess_run(cmd, **kw):
        out_path = cmd[cmd.index("--output") + 1]
        Path(out_path).write_text(_json.dumps(erc), encoding="utf-8")
        return MagicMock(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("subprocess.run", _fake_subprocess_run)
    out = handle_run_erc(iface, {"schematicPath": str(sch)})

    assert out["success"] is True
    recs = out["summary"]["recommendations"]
    refresh_rec = next((r for r in recs if r["kind"] == "refresh_lib_symbols"), None)
    assert refresh_rec is not None
    assert refresh_rec["count"] == 2
    assert "refresh_schematic_lib_symbols" in refresh_rec["action"]
