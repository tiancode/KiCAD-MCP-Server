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


@pytest.fixture(autouse=True)
def _isolate_global_sym_lib_table(monkeypatch):
    """Keep these tests hermetic regardless of the host's KiCad install.

    ``DynamicSymbolLoader.find_library_file`` consults the user-global
    sym-lib-table (``~/.config/kicad`` / ``~/Library/Preferences/kicad`` /
    ``%APPDATA%/kicad``) *before* the bundled-library lookup that each test
    monkeypatches via ``find_kicad_symbol_libraries``.  On any machine with
    KiCad installed (developer laptop or CI runner) that global table
    resolves ``Device:R`` from the real system libraries — so a test that
    points the loader at an empty dir still "finds" the symbol and the
    assertions about ``missing`` / ``refreshed`` flip.  Stub the global-table
    candidate list to empty so resolution only ever uses each test's
    controlled ``find_kicad_symbol_libraries``.
    """
    monkeypatch.setattr(
        "commands.dynamic_symbol_loader.DynamicSymbolLoader._global_sym_lib_table_paths",
        lambda self: [],
    )


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


# A COMPACTED schematic: the whole tree on one line, exactly the shape
# sexpdata.dumps() emits after add_wire / add_label.  The embedded
# Device:R copy is stale ("OLD STALE COPY") so a refresh will replace it.
_SCHEMATIC_COMPACTED_STALE_R = (
    "(kicad_sch (version 20231120) (generator eeschema) (lib_symbols "
    '(symbol "Device:R" (property "Reference" "R" (at 0 0 0)) '
    '(property "Value" "R" (at 0 0 0)) '
    '(property "Description" "Resistor (OLD STALE COPY)" (at 0 0 0)) '
    '(symbol "Device:R_0_1" (rectangle (start -1.016 -2.54) (end 1.016 2.54) '
    "(stroke (width 0.254) (type default)) (fill (type none)))) "
    '(symbol "Device:R_1_1" '
    '(pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1")) '
    '(pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))))) '
    '(symbol (lib_id "Device:R") (at 100 80 0) (unit 1)))'
)


def _paren_depth(text):
    """String-aware net paren depth (0 == balanced)."""
    depth = 0
    in_str = False
    esc = False
    for ch in text:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    return depth


def test_refresh_on_compacted_file_does_not_explode(monkeypatch, tmp_path):
    """Regression: a schematic compacted to a single line (what
    add_wire/add_label leave behind via sexpdata.dumps) used to explode
    on refresh — the per-line indent was computed as the WHOLE file
    prefix (because rfind('\\n') returned -1), so the entire head,
    including '(kicad_sch', was prepended to every line of every
    refreshed symbol, ballooning the file to hundreds of nested roots.

    The refreshed file must stay a single, paren-balanced root.
    """
    lib_dir = tmp_path / "symbols"
    lib_dir.mkdir()
    (lib_dir / "Device.kicad_sym").write_text(_LIB_FRESH, encoding="utf-8")

    sch_path = tmp_path / "compact.kicad_sch"
    sch_path.write_text(_SCHEMATIC_COMPACTED_STALE_R, encoding="utf-8")
    before_len = len(_SCHEMATIC_COMPACTED_STALE_R)

    loader = _make_loader(tmp_path, lib_dir, monkeypatch)
    out = loader.refresh_embedded_lib_symbols(sch_path)

    assert out["success"] is True
    assert out["refreshed"] == ["Device:R"]

    rewritten = sch_path.read_text(encoding="utf-8")
    # The headline regression assertions:
    assert rewritten.count("(kicad_sch") == 1, "file exploded into multiple roots"
    assert _paren_depth(rewritten) == 0, "refresh produced unbalanced parens"
    # A sane single-symbol refresh grows by at most a few KB, never the
    # ~1.3 MB / 700-root blow-up the bug produced.
    assert len(rewritten) < before_len + 5000, "file grew implausibly large"
    # And it actually did the refresh.
    assert "Resistor (UPDATED)" in rewritten
    assert "OLD STALE COPY" not in rewritten


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


# ---------------------------------------------------------------------------
# run_erc auto-refresh: pre-call to refresh_embedded_lib_symbols
# ---------------------------------------------------------------------------
def test_run_erc_auto_refreshes_lib_symbols_by_default(monkeypatch, tmp_path):
    """The user reported every MCP-placed component triggers
    lib_symbol_mismatch.  ERC now runs ``refresh_embedded_lib_symbols``
    before kicad-cli by default so the embedded snapshot is byte-aligned
    with the on-disk .kicad_sym; the refresh result is surfaced under
    ``response.lib_symbols_refresh``."""
    import json as _json
    from unittest.mock import MagicMock

    from commands import dynamic_symbol_loader
    from handlers.schematic_io import handle_run_erc
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.design_rule_commands = MagicMock()
    iface.design_rule_commands._find_kicad_cli = MagicMock(return_value="/fake/kicad-cli")

    sch = tmp_path / "demo.kicad_sch"
    sch.write_text("(kicad_sch)\n", encoding="utf-8")

    # Stub the refresh to a known payload so the test doesn't depend on
    # a real library file.
    refresh_called: dict = {}

    def _fake_refresh(self, schematic_path):
        refresh_called["path"] = str(schematic_path)
        return {
            "success": True,
            "refreshed": ["Device:R", "Device:C"],
            "unchanged": [],
            "missing": [],
            "message": "Refreshed 2 symbol(s)",
        }

    monkeypatch.setattr(
        dynamic_symbol_loader.DynamicSymbolLoader,
        "refresh_embedded_lib_symbols",
        _fake_refresh,
    )

    erc = {"sheets": [{"violations": []}]}
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kw: (
            Path(cmd[cmd.index("--output") + 1]).write_text(_json.dumps(erc), encoding="utf-8"),
            MagicMock(returncode=0, stderr="", stdout=""),
        )[1],
    )

    out = handle_run_erc(iface, {"schematicPath": str(sch)})

    assert out["success"] is True
    # Pre-refresh fired with the schematic path.
    assert refresh_called["path"] == str(sch)
    # Refresh result piggybacks on the ERC response.
    assert out["lib_symbols_refresh"]["refreshed"] == ["Device:R", "Device:C"]


def test_run_erc_opt_out_of_auto_refresh(monkeypatch, tmp_path):
    """Pass ``autoRefreshLibSymbols: false`` to skip the pre-refresh —
    useful when the user wants to see drift warnings."""
    import json as _json
    from unittest.mock import MagicMock

    from commands import dynamic_symbol_loader
    from handlers.schematic_io import handle_run_erc
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.design_rule_commands = MagicMock()
    iface.design_rule_commands._find_kicad_cli = MagicMock(return_value="/fake/kicad-cli")

    sch = tmp_path / "demo.kicad_sch"
    sch.write_text("(kicad_sch)\n", encoding="utf-8")

    monkeypatch.setattr(
        dynamic_symbol_loader.DynamicSymbolLoader,
        "refresh_embedded_lib_symbols",
        lambda self, p: pytest.fail("refresh must NOT be called when autoRefreshLibSymbols=False"),
    )

    erc = {"sheets": [{"violations": []}]}
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kw: (
            Path(cmd[cmd.index("--output") + 1]).write_text(_json.dumps(erc), encoding="utf-8"),
            MagicMock(returncode=0, stderr="", stdout=""),
        )[1],
    )

    out = handle_run_erc(iface, {"schematicPath": str(sch), "autoRefreshLibSymbols": False})

    assert out["success"] is True
    assert "lib_symbols_refresh" not in out


# ---------------------------------------------------------------------------
# A13 — pre-refresh must NOT revert set_symbol_pin_types' embedded pin-type edit
# ---------------------------------------------------------------------------
# On-disk .kicad_sym: pins blanket ``unspecified`` (the easyeda import shape).
# Sub-symbol uses the SHORT name exactly as extract_symbol_from_library emits.
_LIB_UNSPEC = """\
(kicad_symbol_lib
  (version 20231120)
  (generator kicad_symbol_editor)
  (symbol "RDA5807M"
    (property "Reference" "U" (at 0 0 0))
    (property "Value" "RDA5807M" (at 0 0 0))
    (property "Description" "FM radio" (at 0 0 0))
    (symbol "RDA5807M_1_1"
      (pin unspecified line (at -10 5 0) (length 5)
        (name "VDD" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin unspecified line (at -10 0 0) (length 5)
        (name "GND" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27)))))
    )
  )
)
"""

# Embedded copy mirrors what inject_symbol_into_schematic writes: top-level name
# is library-prefixed, sub-symbol keeps the short name.
_SCH_EMBEDDED_UNSPEC = """\
(kicad_sch
  (version 20231120)
  (generator eeschema)
  (lib_symbols
    (symbol "Device:RDA5807M"
      (property "Reference" "U" (at 0 0 0))
      (property "Value" "RDA5807M" (at 0 0 0))
      (property "Description" "FM radio" (at 0 0 0))
      (symbol "RDA5807M_1_1"
        (pin unspecified line (at -10 5 0) (length 5)
          (name "VDD" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin unspecified line (at -10 0 0) (length 5)
          (name "GND" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "Device:RDA5807M") (at 100 80 0) (unit 1)
    (property "Reference" "U1" (at 100 70 0))
  )
)
"""


def _embedded_pin_types(sch_path, lib_id):
    import commands.easyeda_import as ee

    content = sch_path.read_text(encoding="utf-8")
    ls_start = content.find("(lib_symbols")
    ls_end = ee._match_paren(content, ls_start)
    span = ee._symbol_span(content[ls_start:ls_end], lib_id)
    block = content[ls_start:ls_end][span[0] : span[1]]
    out = {}
    i = 0
    while True:
        p = block.find("(pin ", i)
        if p == -1:
            break
        end = ee._match_paren(block, p)
        pb = block[p:end]
        hdr = ee._PIN_HEADER_RE.match(pb)
        nm = ee._PIN_NAME_RE.search(pb)
        if hdr and nm:
            out[nm.group(1)] = hdr.group(1)
        i = end
    return out


def test_refresh_preserves_pin_type_override(monkeypatch, tmp_path):
    """A13: set_symbol_pin_types (schematic mode) retypes pins in the embedded
    snapshot while the on-disk .kicad_sym still says ``unspecified``.  run_erc's
    pre-refresh must NOT revert that deliberate edit — the marked pins stay
    output/power_in instead of collapsing back to unspecified."""
    import commands.symbol_pin_types as spt

    lib_dir = tmp_path / "symbols"
    lib_dir.mkdir()
    (lib_dir / "Device.kicad_sym").write_text(_LIB_UNSPEC, encoding="utf-8")
    sch_path = tmp_path / "demo.kicad_sch"
    sch_path.write_text(_SCH_EMBEDDED_UNSPEC, encoding="utf-8")

    spt.apply_to_schematic(
        sch_path, "Device:RDA5807M", spt.normalize_mapping({"1": "output", "2": "power_in"})
    )
    assert _embedded_pin_types(sch_path, "Device:RDA5807M") == {
        "VDD": "output",
        "GND": "power_in",
    }

    loader = _make_loader(tmp_path, lib_dir, monkeypatch)
    out = loader.refresh_embedded_lib_symbols(sch_path)

    assert out["success"] is True
    # The override survives the pre-refresh (no revert to unspecified).
    assert _embedded_pin_types(sch_path, "Device:RDA5807M") == {
        "VDD": "output",
        "GND": "power_in",
    }
    # fresh + override == embedded ⇒ no spurious rewrite reported.
    assert out["refreshed"] == []
    assert out["unchanged"] == ["Device:RDA5807M"]


def test_refresh_merges_override_into_genuine_library_drift(monkeypatch, tmp_path):
    """The overridden pin types are preserved WHILE genuine library drift (a
    changed Description on disk) still flows into the embedded copy."""
    import commands.symbol_pin_types as spt

    lib_dir = tmp_path / "symbols"
    lib_dir.mkdir()
    (lib_dir / "Device.kicad_sym").write_text(_LIB_UNSPEC, encoding="utf-8")
    sch_path = tmp_path / "demo.kicad_sch"
    sch_path.write_text(_SCH_EMBEDDED_UNSPEC, encoding="utf-8")

    spt.apply_to_schematic(sch_path, "Device:RDA5807M", spt.normalize_mapping({"1": "output"}))

    # Library genuinely drifts on disk.
    (lib_dir / "Device.kicad_sym").write_text(
        _LIB_UNSPEC.replace("FM radio", "FM radio receiver (rev B)"), encoding="utf-8"
    )

    loader = _make_loader(tmp_path, lib_dir, monkeypatch)
    out = loader.refresh_embedded_lib_symbols(sch_path)

    assert out["success"] is True
    assert out["refreshed"] == ["Device:RDA5807M"]
    content = sch_path.read_text(encoding="utf-8")
    # Drift flowed in.
    assert "FM radio receiver (rev B)" in content
    # Override preserved; the un-overridden pin took the (still unspecified) disk value.
    types = _embedded_pin_types(sch_path, "Device:RDA5807M")
    assert types["VDD"] == "output"
    assert types["GND"] == "unspecified"


def test_run_erc_continues_when_pre_refresh_fails(monkeypatch, tmp_path):
    """Pre-refresh is best-effort — a failure (corrupted schematic,
    missing library, etc.) must NOT block ERC.  The failure status is
    surfaced in lib_symbols_refresh for visibility."""
    import json as _json
    from unittest.mock import MagicMock

    from commands import dynamic_symbol_loader
    from handlers.schematic_io import handle_run_erc
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.design_rule_commands = MagicMock()
    iface.design_rule_commands._find_kicad_cli = MagicMock(return_value="/fake/kicad-cli")

    sch = tmp_path / "demo.kicad_sch"
    sch.write_text("(kicad_sch)\n", encoding="utf-8")

    def _crash_refresh(self, schematic_path):
        raise RuntimeError("library file unreadable")

    monkeypatch.setattr(
        dynamic_symbol_loader.DynamicSymbolLoader,
        "refresh_embedded_lib_symbols",
        _crash_refresh,
    )

    erc = {"sheets": [{"violations": []}]}
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kw: (
            Path(cmd[cmd.index("--output") + 1]).write_text(_json.dumps(erc), encoding="utf-8"),
            MagicMock(returncode=0, stderr="", stdout=""),
        )[1],
    )

    out = handle_run_erc(iface, {"schematicPath": str(sch)})

    # ERC still ran successfully.
    assert out["success"] is True
    # Failure surfaced for visibility.
    assert out["lib_symbols_refresh"]["success"] is False
    assert "library file unreadable" in out["lib_symbols_refresh"]["message"]
