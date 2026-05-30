"""Tests for the LCSC/JLCPCB → KiCAD symbol import (``import_jlcpcb_symbol``).

The network/tool call (``python -m easyeda2kicad``) is mocked via the
injectable ``easyeda_import._run`` seam, and the shared-cache / lib-table
paths are redirected into ``tmp_path`` — so these run offline and never
touch the user's real ~/.kicad-mcp or KiCad config.

A real end-to-end fetch of C7593 (NE555) is provided as an opt-in
integration test, skipped unless RUN_EASYEDA_NET=1.
"""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))

import commands.easyeda_import as ee  # noqa: E402

# A minimal KiCad v6 .kicad_sym with one top-level symbol, mirroring what
# easyeda2kicad emits (LCSC Part + Footprint properties, one nested sub-unit).
_FIXTURE_LIB = """\
(kicad_symbol_lib
  (version 20211014)
  (generator https://github.com/uPesy/easyeda2kicad.py)
  (symbol "NE555DR"
    (property "Reference" "U" (id 0) (at 0 0 0))
    (property "Value" "NE555DR" (id 1) (at 0 0 0))
    (property "Footprint" "easyeda:SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL" (id 2) (at 0 0 0))
    (property "Datasheet" "https://example.com/ne555.pdf" (id 3) (at 0 0 0))
    (property "Manufacturer" "Texas Instruments" (id 4) (at 0 0 0))
    (property "MPN" "NE555DR" (id 5) (at 0 0 0))
    (property "LCSC Part" "C7593" (id 6) (at 0 0 0))
    (symbol "NE555DR_0_1")
  )
)
"""


def _make_runner(
    sym_path,
    pretty_dir,
    *,
    content=_FIXTURE_LIB,
    write=True,
    rc=0,
    stderr="",
    stdout="",
    record=None,
):
    def _runner(cmd, timeout):
        if record is not None:
            record.append(cmd)
        if write:
            sym_path.parent.mkdir(parents=True, exist_ok=True)
            sym_path.write_text(content, encoding="utf-8")
            pretty_dir.mkdir(parents=True, exist_ok=True)
            (pretty_dir / "SOIC-8.kicad_mod").write_text("(footprint)", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr=stderr)

    return _runner


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Redirect cache + config paths into tmp_path."""
    cache = tmp_path / "cache"
    sym = cache / "easyeda.kicad_sym"
    pretty = cache / "easyeda.pretty"
    cfg = tmp_path / "config" / "kicad" / "10.0"

    monkeypatch.setattr(ee, "_CACHE_DIR", cache)
    monkeypatch.setattr(ee, "SYMBOL_LIB_PATH", sym)
    monkeypatch.setattr(ee, "FOOTPRINT_LIB_DIR", pretty)

    def _cfg_dir():
        cfg.mkdir(parents=True, exist_ok=True)
        return cfg

    monkeypatch.setattr(ee, "_resolve_global_config_dir", _cfg_dir)
    return SimpleNamespace(cache=cache, sym=sym, pretty=pretty, cfg=cfg)


# ---------------------------------------------------------------------------
# _normalize_lcsc
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    "given,expected",
    [("C7593", "C7593"), ("7593", "C7593"), (" c7593 ", "C7593"), ("C25804", "C25804")],
)
def test_normalize_lcsc_ok(given, expected):
    assert ee._normalize_lcsc(given) == expected


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "  ", "ABC", "C", "C12A", "12-34"])
def test_normalize_lcsc_rejects(bad):
    with pytest.raises(ValueError):
        ee._normalize_lcsc(bad)


# ---------------------------------------------------------------------------
# _parse_symbols
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_parse_symbols(tmp_path):
    lib = tmp_path / "easyeda.kicad_sym"
    lib.write_text(_FIXTURE_LIB, encoding="utf-8")
    syms = ee._parse_symbols(lib)
    assert [s["name"] for s in syms] == ["NE555DR"]  # sub-unit not top-level
    props = syms[0]["properties"]
    assert props["LCSC Part"] == "C7593"
    assert props["Footprint"] == "easyeda:SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL"


@pytest.mark.unit
def test_parse_symbols_missing_file(tmp_path):
    assert ee._parse_symbols(tmp_path / "nope.kicad_sym") == []


# ---------------------------------------------------------------------------
# _ensure_table_entry
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_ensure_table_entry_creates_and_dedups(tmp_path):
    table = tmp_path / "sym-lib-table"
    added = ee._ensure_table_entry(table, "sym_lib_table", "easyeda", "/x/easyeda.kicad_sym", "d")
    assert added is True
    content = table.read_text()
    assert '(name "easyeda")' in content and "/x/easyeda.kicad_sym" in content
    assert content.strip().startswith("(sym_lib_table")

    # Idempotent: second call adds nothing.
    again = ee._ensure_table_entry(table, "sym_lib_table", "easyeda", "/x/easyeda.kicad_sym", "d")
    assert again is False
    assert table.read_text().count('(name "easyeda")') == 1


# ---------------------------------------------------------------------------
# import_lcsc_part
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_import_new_part(env, monkeypatch):
    record = []
    monkeypatch.setattr(ee, "_run", _make_runner(env.sym, env.pretty, record=record))

    res = ee.import_lcsc_part("C7593")

    assert res["success"] is True
    assert res["symbol"] == "NE555DR"
    assert res["lib_id"] == "easyeda:NE555DR"
    assert res["library"] == "easyeda"
    assert res["footprint"] == "easyeda:SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL"
    assert res["fetched"] is True and res["already_cached"] is False
    assert len(record) == 1  # the tool was invoked once

    # Both lib-tables were created with the easyeda nickname.
    sym_table = (env.cfg / "sym-lib-table").read_text()
    fp_table = (env.cfg / "fp-lib-table").read_text()
    assert '(name "easyeda")' in sym_table and str(env.sym) in sym_table
    assert '(name "easyeda")' in fp_table and str(env.pretty) in fp_table
    assert res["registered"]["sym_added"] is True
    assert res["registered"]["fp_added"] is True


@pytest.mark.unit
def test_import_already_cached_skips_fetch(env, monkeypatch):
    env.sym.parent.mkdir(parents=True, exist_ok=True)
    env.sym.write_text(_FIXTURE_LIB, encoding="utf-8")
    env.pretty.mkdir(parents=True, exist_ok=True)

    record = []
    monkeypatch.setattr(ee, "_run", _make_runner(env.sym, env.pretty, record=record))

    res = ee.import_lcsc_part("C7593")  # no overwrite

    assert res["success"] is True
    assert res["symbol"] == "NE555DR"
    assert res["fetched"] is False and res["already_cached"] is True
    assert record == []  # network/tool was NOT called


@pytest.mark.unit
def test_import_overwrite_refetches(env, monkeypatch):
    env.sym.parent.mkdir(parents=True, exist_ok=True)
    env.sym.write_text(_FIXTURE_LIB, encoding="utf-8")
    env.pretty.mkdir(parents=True, exist_ok=True)

    record = []
    monkeypatch.setattr(ee, "_run", _make_runner(env.sym, env.pretty, record=record))

    res = ee.import_lcsc_part("C7593", overwrite=True)

    assert res["fetched"] is True
    assert len(record) == 1
    assert "--overwrite" in record[0]


@pytest.mark.unit
def test_import_not_installed_message(env, monkeypatch):
    # Tool absent: returns non-zero, writes nothing.
    monkeypatch.setattr(
        ee,
        "_run",
        _make_runner(
            env.sym, env.pretty, write=False, rc=1, stderr="No module named easyeda2kicad"
        ),
    )
    with pytest.raises(ee.EasyEdaImportError) as exc:
        ee.import_lcsc_part("C7593")
    assert "pip install easyeda2kicad" in str(exc.value)


@pytest.mark.unit
def test_import_no_symbol_produced(env, monkeypatch):
    # Tool ran but produced nothing for this id.
    monkeypatch.setattr(
        ee, "_run", _make_runner(env.sym, env.pretty, write=False, rc=0, stdout="nothing")
    )
    with pytest.raises(ee.EasyEdaImportError) as exc:
        ee.import_lcsc_part("C7593")
    assert "did not produce a symbol" in str(exc.value)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_handler_missing_lcsc():
    from handlers.jlcpcb import handle_import_jlcpcb_symbol

    res = handle_import_jlcpcb_symbol(None, {})
    assert res["success"] is False
    assert "lcsc_number" in res["message"].lower()


@pytest.mark.unit
def test_handler_end_to_end(env, monkeypatch):
    from handlers.jlcpcb import handle_import_jlcpcb_symbol

    monkeypatch.setattr(ee, "_run", _make_runner(env.sym, env.pretty))
    res = handle_import_jlcpcb_symbol(None, {"lcsc_number": "C7593"})
    assert res["success"] is True
    assert res["symbol"] == "NE555DR"


@pytest.mark.unit
def test_handler_surfaces_not_installed(env, monkeypatch):
    from handlers.jlcpcb import handle_import_jlcpcb_symbol

    monkeypatch.setattr(
        ee,
        "_run",
        _make_runner(
            env.sym, env.pretty, write=False, rc=1, stderr="No module named easyeda2kicad"
        ),
    )
    res = handle_import_jlcpcb_symbol(None, {"lcsc_number": "C7593"})
    assert res["success"] is False
    assert "pip install easyeda2kicad" in res["message"]


# ---------------------------------------------------------------------------
# import_lcsc_parts (batch)
# ---------------------------------------------------------------------------
def _make_appending_runner(sym_path, pretty_dir, *, record=None):
    """Fake easyeda2kicad that appends a per-id symbol (LCSC-aware), like the
    real tool: each call adds (symbol "SYM_<id>") with a matching LCSC Part."""

    def _runner(cmd, timeout):
        if record is not None:
            record.append(cmd)
        lcsc = cmd[cmd.index("--lcsc_id") + 1]
        block = (
            f'  (symbol "SYM_{lcsc}"\n'
            f'    (property "Value" "SYM_{lcsc}" (id 1) (at 0 0 0))\n'
            f'    (property "Footprint" "easyeda:FP_{lcsc}" (id 2) (at 0 0 0))\n'
            f'    (property "LCSC Part" "{lcsc}" (id 6) (at 0 0 0))\n'
            f"  )\n"
        )
        sym_path.parent.mkdir(parents=True, exist_ok=True)
        if sym_path.exists():
            content = sym_path.read_text(encoding="utf-8")
        else:
            content = "(kicad_symbol_lib\n  (version 20211014)\n  (generator test)\n)\n"
        idx = content.rfind(")")
        sym_path.write_text(content[:idx] + block + content[idx:], encoding="utf-8")
        pretty_dir.mkdir(parents=True, exist_ok=True)
        (pretty_dir / f"FP_{lcsc}.kicad_mod").write_text("(footprint)", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return _runner


@pytest.mark.unit
def test_batch_all_new(env, monkeypatch):
    record = []
    monkeypatch.setattr(ee, "_run", _make_appending_runner(env.sym, env.pretty, record=record))

    res = ee.import_lcsc_parts(["C100", "C200"])

    assert res["success"] is True and res["all_succeeded"] is True
    assert (res["imported"], res["cached"], res["failed"]) == (2, 0, 0)
    assert [r["symbol"] for r in res["results"]] == ["SYM_C100", "SYM_C200"]
    # Both parts accumulated into the one shared library.
    assert {s["name"] for s in ee._parse_symbols(env.sym)} == {"SYM_C100", "SYM_C200"}
    assert len(record) == 2


@pytest.mark.unit
def test_batch_dedups_input(env, monkeypatch):
    record = []
    monkeypatch.setattr(ee, "_run", _make_appending_runner(env.sym, env.pretty, record=record))

    res = ee.import_lcsc_parts(["C100", "c100", " C100 "])

    assert res["requested"] == 1
    assert res["imported"] == 1
    assert len(record) == 1  # the tool ran once despite three spellings


@pytest.mark.unit
def test_batch_mixed_cached_and_invalid(env, monkeypatch):
    # Pre-cache C100 so it is skipped without a fetch.
    env.sym.parent.mkdir(parents=True, exist_ok=True)
    env.sym.write_text(
        "(kicad_symbol_lib\n  (version 20211014)\n  (generator t)\n"
        '  (symbol "SYM_C100" (property "LCSC Part" "C100" (id 6) (at 0 0 0)))\n)\n',
        encoding="utf-8",
    )
    env.pretty.mkdir(parents=True, exist_ok=True)

    record = []
    monkeypatch.setattr(ee, "_run", _make_appending_runner(env.sym, env.pretty, record=record))

    res = ee.import_lcsc_parts(["C100", "C200", "not-an-id"])

    assert (res["imported"], res["cached"], res["failed"]) == (1, 1, 1)
    assert res["success"] is True  # at least one obtained
    assert res["all_succeeded"] is False
    assert [f["lcsc"] for f in res["failures"]] == ["not-an-id"]
    statuses = {r["lcsc"]: r["status"] for r in res["results"]}
    assert statuses == {"C100": "cached", "C200": "imported", "not-an-id": "failed"}
    assert len(record) == 1  # only C200 hit the tool (C100 cached, bad id rejected pre-run)


@pytest.mark.unit
def test_batch_all_invalid_is_failure(env, monkeypatch):
    record = []
    monkeypatch.setattr(ee, "_run", _make_appending_runner(env.sym, env.pretty, record=record))

    res = ee.import_lcsc_parts(["bad", "C"])

    assert res["success"] is False  # nothing obtained
    assert (res["imported"], res["cached"], res["failed"]) == (0, 0, 2)
    assert record == []  # malformed ids never reach the tool


@pytest.mark.unit
def test_batch_handler_requires_list():
    from handlers.jlcpcb import handle_import_jlcpcb_symbols

    res = handle_import_jlcpcb_symbols(None, {})
    assert res["success"] is False
    assert "non-empty list" in res["message"].lower()


@pytest.mark.unit
def test_batch_handler_end_to_end(env, monkeypatch):
    from handlers.jlcpcb import handle_import_jlcpcb_symbols

    monkeypatch.setattr(ee, "_run", _make_appending_runner(env.sym, env.pretty))
    res = handle_import_jlcpcb_symbols(None, {"lcsc_numbers": ["C100", "C200"]})
    assert res["success"] is True
    assert res["imported"] == 2
    assert {r["symbol"] for r in res["results"]} == {"SYM_C100", "SYM_C200"}


@pytest.mark.unit
def test_batch_handler_accepts_single_string(env, monkeypatch):
    from handlers.jlcpcb import handle_import_jlcpcb_symbols

    monkeypatch.setattr(ee, "_run", _make_appending_runner(env.sym, env.pretty))
    res = handle_import_jlcpcb_symbols(None, {"lcsc_numbers": "C100"})
    assert res["success"] is True
    assert res["requested"] == 1


# ---------------------------------------------------------------------------
# Opt-in real network fetch (skipped by default)
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_EASYEDA_NET") != "1",
    reason="set RUN_EASYEDA_NET=1 to hit the live EasyEDA API",
)
def test_real_fetch_ne555(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(ee, "_CACHE_DIR", cache)
    monkeypatch.setattr(ee, "SYMBOL_LIB_PATH", cache / "easyeda.kicad_sym")
    monkeypatch.setattr(ee, "FOOTPRINT_LIB_DIR", cache / "easyeda.pretty")
    monkeypatch.setattr(ee, "_resolve_global_config_dir", lambda: tmp_path / "cfg")
    (tmp_path / "cfg").mkdir()

    res = ee.import_lcsc_part("C7593", overwrite=True)
    assert res["success"] is True
    assert res["symbol"]  # a real symbol name was found
    assert (cache / "easyeda.kicad_sym").exists()
