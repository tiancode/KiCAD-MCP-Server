"""Tests for JLCPCB parts search (``search_jlcpcb_parts`` → ``search_parts_meta``).

Builds a tiny temp SQLite DB that mirrors the public JLCSearch import — in
particular ``category``/``manufacturer`` are left blank, since that is what
breaks naive filtering on the real ~7M-row database. Exercises the MPN-first
lookup, the AND→OR free-text fallback, and the blank-column fold-to-text path.
No real KiCAD or network access required.
"""

import json
import sys
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))

from commands.jlcpcb_parts import JLCPCBPartsManager  # noqa: E402

# (lcsc, mfr_part, package, library_type, description, stock)
_ROWS = [
    ("C9865", "TPS54331DR", "SOIC-8", "Extended", "3.5V~28V 3A 570kHz Buck Converter", 100),
    ("C90761", "TPS54331DDAR", "SOIC-8-EP", "Extended", "3.5V~28V 3A 570kHz Buck Converter", 50),
    ("C0", "RC0603FR-0710KL", "0603", "Basic", "10kΩ ±1% 100mW Thick Film Resistor", 1000),
    ("C_OOS", "TPS54331QDRQ1", "SOIC-8", "Extended", "3A Buck Converter", 0),  # out of stock
    # Alternatives fixture: a reference 10kΩ 0603 resistor, two more resistors
    # (C0 above is a Basic one), and a same-package capacitor that shares no
    # resistor tokens — it must NOT surface as an alternative.
    ("C_R_REF", "RES-10K-REF", "0603", "Extended", "10kΩ ±1% 100mW Thick Film Resistor", 100),
    ("C_R3", "RES-10K-J", "0603", "Extended", "10kΩ ±5% 100mW Thick Film Resistor", 50),
    ("C_CAP", "CAP-10NF", "0603", "Basic", "10nF 50V X7R Multilayer Ceramic Capacitor", 999),
    # No-token descriptions in a unique package → alternatives fallback path.
    ("C_REF_EMPTY", "EMPTY1", "QFN-99", "Extended", "", 10),
    ("C_QFN2", "QFN2", "QFN-99", "Extended", "", 20),
]


@pytest.fixture
def manager(tmp_path):
    mgr = JLCPCBPartsManager(db_path=str(tmp_path / "jlc.db"))
    cur = mgr.conn.cursor()
    for lcsc, mfr, pkg, lib, desc, stock in _ROWS:
        cur.execute(
            "INSERT INTO components (lcsc, category, subcategory, mfr_part, package, "
            "manufacturer, library_type, description, stock, price_json) "
            "VALUES (?, '', '', ?, ?, '', ?, ?, ?, ?)",
            (lcsc, mfr, pkg, lib, desc, stock, json.dumps([{"qty": 1, "price": 0.5}])),
        )
    cur.execute("INSERT INTO components_fts(components_fts) VALUES('rebuild')")
    mgr.conn.commit()
    yield mgr
    mgr.close()


def _lcscs(result):
    return {p["lcsc"] for p in result["parts"]}


def test_mpn_exact_takes_priority(manager):
    r = manager.search_parts_meta(mpn="TPS54331DR")
    assert r["match_mode"] == "mpn_exact"
    assert r["fuzzy"] is False
    assert _lcscs(r) == {"C9865"}


def test_mpn_prefix_is_case_insensitive(manager):
    r = manager.search_parts_meta(mpn="tps54331")  # lowercase, no exact row
    assert r["match_mode"] == "mpn_prefix"
    assert r["fuzzy"] is True
    assert {"C9865", "C90761"} <= _lcscs(r)
    assert r["warnings"]


def test_mpn_no_match(manager):
    r = manager.search_parts_meta(mpn="NOSUCHPARTXYZ")
    assert r["match_mode"] == "mpn_none"
    assert r["count"] == 0


def test_mpn_respects_in_stock(manager):
    # The out-of-stock buck must be filtered even on an exact-ish prefix.
    r = manager.search_parts_meta(mpn="TPS54331QDRQ1", in_stock=True)
    assert r["count"] == 0


def test_query_and_is_precise(manager):
    r = manager.search_parts_meta(query="buck 3a")
    assert r["match_mode"] == "and"
    assert r["fuzzy"] is False
    assert {"C9865", "C90761"} <= _lcscs(r)


def test_query_falls_back_to_or_when_and_empty(manager):
    # "buck" matches; the second term matches nothing → AND empty → OR fallback.
    r = manager.search_parts_meta(query="buck zzznotaterm")
    assert r["match_mode"] == "or"
    assert r["fuzzy"] is True
    assert "C9865" in _lcscs(r)
    assert r["warnings"]


def test_blank_category_is_folded_into_text_with_warning(manager):
    # category column is empty → value folded into the FTS terms, not a dead filter.
    r = manager.search_parts_meta(category="Resistor", package="0603")
    assert "C0" in _lcscs(r)
    assert any("category is empty" in w for w in r["warnings"])


def test_filter_only_query(manager):
    r = manager.search_parts_meta(package="SOIC-8")
    assert r["match_mode"] == "filter"
    assert r["fuzzy"] is False
    # SOIC-8 and SOIC-8-EP both match the substring; C_OOS is filtered (out of stock).
    assert _lcscs(r) == {"C9865", "C90761"}


def test_column_has_data_probe_is_cached(manager):
    assert manager._column_has_data("category") is False
    assert manager._column_has_data("package") is True
    assert manager._column_has_data_cache == {"category": False, "package": True}


def test_search_parts_back_compat_returns_list(manager):
    out = manager.search_parts(query="buck")
    assert isinstance(out, list)
    assert all("lcsc" in p for p in out)


def test_description_fts_terms_filters_noise(manager):
    terms = manager._description_fts_terms(
        "-55℃~+155℃ 100mW 510kΩ 75V Thick Film Resistor ±1% 0603 ROHS"
    )
    assert "510kω" in terms  # value token kept (and lowercased)
    assert "75v" in terms
    assert "0603" not in terms  # bare number dropped
    assert "55" not in terms and "155" not in terms  # temp-range fragments dropped
    assert terms == [t.lower() for t in terms]


def test_suggest_alternatives_finds_same_spec_not_same_package_junk(manager):
    alts = {p["lcsc"] for p in manager.suggest_alternatives("C_R_REF", limit=5)}
    assert {"C0", "C_R3"} <= alts  # other 10kΩ resistors surface
    assert "C_CAP" not in alts  # same package, different function — excluded
    assert "C_R_REF" not in alts  # never returns the reference itself


def test_suggest_alternatives_prefers_basic(manager):
    alts = manager.suggest_alternatives("C_R_REF", limit=5)
    assert alts[0]["lcsc"] == "C0"  # the Basic 10kΩ ranks first


def test_suggest_alternatives_falls_back_to_package(manager):
    # Empty description → no usable tokens → package-only fallback.
    alts = {p["lcsc"] for p in manager.suggest_alternatives("C_REF_EMPTY", limit=5)}
    assert "C_QFN2" in alts
    assert "C_REF_EMPTY" not in alts
