"""Tests for Bug A: enrich_datasheets must enumerate every placed component and
fill empty Datasheet fields.

The original implementation matched placed symbols line-by-line
(``(symbol (lib_id "``) and only recognised a ``"LCSC"`` property. Against a
KiCad-saved schematic — where ``(symbol`` and ``(lib_id`` land on separate lines
and easyeda2kicad parts carry ``"LCSC Part"`` — it enumerated zero symbols and
returned all-zero counts. These tests pin the format-robust, quote-aware
enumeration and the LCSC / lib-symbol fill sources.
"""

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.datasheet_manager import DatasheetManager

# ---------------------------------------------------------------------------
# Schematic builders
# ---------------------------------------------------------------------------


def _kicad_saved_symbol(
    lib_id: str,
    reference: str,
    *,
    datasheet: str = "~",
    lcsc: str = None,
    lcsc_prop: str = "LCSC Part",
    manufacturer: str = None,
) -> str:
    """A placed symbol block in the multi-line layout KiCad itself writes:
    ``(symbol`` and ``(lib_id`` on separate lines, each field spanning lines."""
    extra = ""
    if manufacturer is not None:
        extra += (
            f'\t\t(property "Manufacturer" "{manufacturer}"\n'
            f"\t\t\t(at 100 100 0)\n"
            f"\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n"
            f"\t\t)\n"
        )
    if lcsc is not None:
        extra += (
            f'\t\t(property "{lcsc_prop}" "{lcsc}"\n'
            f"\t\t\t(at 100 100 0)\n"
            f"\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n"
            f"\t\t)\n"
        )
    return (
        f"\t(symbol\n"
        f'\t\t(lib_id "{lib_id}")\n'
        f"\t\t(at 100 100 0)\n"
        f"\t\t(unit 1)\n"
        f"\t\t(in_bom yes)\n"
        f"\t\t(on_board yes)\n"
        f"\t\t(dnp no)\n"
        f'\t\t(uuid "11111111-1111-1111-1111-111111111111")\n'
        f'\t\t(property "Reference" "{reference}"\n'
        f"\t\t\t(at 100 97 0)\n"
        f"\t\t\t(effects (font (size 1.27 1.27)))\n"
        f"\t\t)\n"
        f'\t\t(property "Value" "{reference}_VAL"\n'
        f"\t\t\t(at 100 103 0)\n"
        f"\t\t\t(effects (font (size 1.27 1.27)))\n"
        f"\t\t)\n"
        f'\t\t(property "Footprint" "easyeda:LQFP-100"\n'
        f"\t\t\t(at 100 100 0)\n"
        f"\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n"
        f"\t\t)\n"
        f'\t\t(property "Datasheet" "{datasheet}"\n'
        f"\t\t\t(at 100 100 0)\n"
        f"\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n"
        f"\t\t)\n"
        f"{extra}"
        f"\t\t(instances\n"
        f'\t\t\t(project "proj"\n'
        f'\t\t\t\t(path "/root" (reference "{reference}") (unit 1))\n'
        f"\t\t\t)\n"
        f"\t\t)\n"
        f"\t)\n"
    )


def _easyeda_format_symbol(lib_id: str, reference: str, lcsc: str) -> str:
    """A placed symbol whose (property …) tokens sit on their own lines, as
    easyeda2kicad emits them — the layout that defeated the old line matcher."""
    return (
        f'\t(symbol (lib_id "{lib_id}") (at 100 100 0) (unit 1)\n'
        f'\t\t(uuid "22222222-2222-2222-2222-222222222222")\n'
        f"\t\t(property\n"
        f'\t\t\t"Reference"\n'
        f'\t\t\t"{reference}"\n'
        f"\t\t\t(at 100 97 0)\n"
        f"\t\t\t(effects (font (size 1.27 1.27)))\n"
        f"\t\t)\n"
        f"\t\t(property\n"
        f'\t\t\t"Datasheet"\n'
        f'\t\t\t"~"\n'
        f"\t\t\t(at 100 100 0)\n"
        f"\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n"
        f"\t\t)\n"
        f"\t\t(property\n"
        f'\t\t\t"LCSC Part"\n'
        f'\t\t\t"{lcsc}"\n'
        f"\t\t\t(at 100 100 0)\n"
        f"\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n"
        f"\t\t)\n"
        f"\t)\n"
    )


def _lib_symbols(entries: List[str] = None) -> str:
    """A (lib_symbols …) block. Each entry is raw (symbol "Lib:Name" …) text."""
    body = "".join(entries or [])
    return f"\t(lib_symbols\n{body}\t)\n"


def _lib_symbol_with_datasheet(lib_id: str, datasheet: str) -> str:
    return (
        f'\t\t(symbol "{lib_id}"\n'
        f'\t\t\t(property "Reference" "U" (at 0 0 0))\n'
        f'\t\t\t(property "Value" "{lib_id}" (at 0 0 0))\n'
        f'\t\t\t(property "Datasheet" "{datasheet}" (at 0 0 0))\n'
        f'\t\t\t(symbol "{lib_id.split(":")[-1]}_0_1"\n'
        f"\t\t\t\t(rectangle (start -1 1) (end 1 -1))\n"
        f"\t\t\t)\n"
        f"\t\t)\n"
    )


def _sch(symbols: str, lib_symbols_block: str = None) -> str:
    if lib_symbols_block is None:
        lib_symbols_block = _lib_symbols()
    return (
        "(kicad_sch\n"
        "\t(version 20250114)\n"
        '\t(generator "eeschema")\n'
        '\t(uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")\n'
        '\t(paper "A4")\n'
        f"{lib_symbols_block}"
        f"{symbols}"
        "\t(sheet_instances\n"
        '\t\t(path "/" (page "1"))\n'
        "\t)\n"
        ")\n"
    )


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "sch.kicad_sch"
    p.write_text(content, encoding="utf-8")
    return p


def _enrich(path: Path, dry_run: bool = False) -> Dict[str, Any]:
    return DatasheetManager().enrich_schematic(path, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Enumeration (the core regression)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnumeration:
    def test_enumerates_kicad_saved_multiline_format(self, tmp_path: Any) -> None:
        """Two placed symbols in KiCad's saved layout must both be seen — the
        old line-by-line matcher returned all-zero counts here."""
        sym1 = _kicad_saved_symbol("easyeda:GD32F103VET6", "U2", lcsc="C6186")
        sym2 = _kicad_saved_symbol("Device:R", "R1", datasheet="https://example.com/r.pdf")
        sch = _write(tmp_path, _sch(sym1 + sym2))
        res = _enrich(sch)
        assert res["success"] is True
        assert res["total_symbols"] == 2
        assert res["updated"] == 1  # U2 filled from LCSC
        assert res["already_set"] == 1  # R1 already had a datasheet

    def test_does_not_enumerate_lib_symbols_entries(self, tmp_path: Any) -> None:
        lib = _lib_symbols([_lib_symbol_with_datasheet("Device:R", "~")])
        sym = _kicad_saved_symbol("Device:R", "R1", lcsc="C25804")
        sch = _write(tmp_path, _sch(sym, lib))
        res = _enrich(sch)
        # Only the ONE placed symbol counts, never the library definition.
        assert res["total_symbols"] == 1


# ---------------------------------------------------------------------------
# Fill sources
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFillSources:
    def test_fill_from_lcsc_part_property(self, tmp_path: Any) -> None:
        sym = _kicad_saved_symbol("easyeda:GD32F103VET6", "U2", lcsc="C6186")
        sch = _write(tmp_path, _sch(sym))
        res = _enrich(sch)
        assert res["updated"] == 1
        assert res["details"][0]["source"] == "lcsc"
        assert res["details"][0]["lcsc"] == "C6186"
        # The URL is actually written into the file.
        assert "https://www.lcsc.com/datasheet/C6186.pdf" in sch.read_text()

    def test_fill_from_plain_lcsc_property(self, tmp_path: Any) -> None:
        sym = _kicad_saved_symbol("easyeda:PART", "U3", lcsc="C11702", lcsc_prop="LCSC")
        sch = _write(tmp_path, _sch(sym))
        res = _enrich(sch)
        assert res["updated"] == 1
        assert "https://www.lcsc.com/datasheet/C11702.pdf" in sch.read_text()

    def test_fill_from_lib_symbol_datasheet(self, tmp_path: Any) -> None:
        """No LCSC on the instance, but its library symbol carries a Datasheet."""
        lib = _lib_symbols(
            [_lib_symbol_with_datasheet("easyeda:MCU", "https://lib.example/mcu.pdf")]
        )
        sym = _kicad_saved_symbol("easyeda:MCU", "U4")  # empty datasheet, no LCSC
        sch = _write(tmp_path, _sch(sym, lib))
        res = _enrich(sch)
        assert res["updated"] == 1
        assert res["details"][0]["source"] == "lib_symbol"
        assert "https://lib.example/mcu.pdf" in sch.read_text()

    def test_lcsc_preferred_over_lib_symbol(self, tmp_path: Any) -> None:
        lib = _lib_symbols(
            [_lib_symbol_with_datasheet("easyeda:MCU", "https://lib.example/mcu.pdf")]
        )
        sym = _kicad_saved_symbol("easyeda:MCU", "U4", lcsc="C6186")
        sch = _write(tmp_path, _sch(sym, lib))
        res = _enrich(sch)
        assert res["details"][0]["source"] == "lcsc"
        assert "https://www.lcsc.com/datasheet/C6186.pdf" in sch.read_text()


# ---------------------------------------------------------------------------
# Counting / classification
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestClassification:
    def test_already_set_not_touched(self, tmp_path: Any) -> None:
        sym = _kicad_saved_symbol(
            "easyeda:PART", "U2", datasheet="https://set.example/x.pdf", lcsc="C6186"
        )
        sch = _write(tmp_path, _sch(sym))
        before = sch.read_text()
        res = _enrich(sch)
        assert res["already_set"] == 1
        assert res["updated"] == 0
        assert sch.read_text() == before  # untouched

    def test_no_source_counted(self, tmp_path: Any) -> None:
        sym = _kicad_saved_symbol("Device:R", "R1")  # empty datasheet, no LCSC/lib
        sch = _write(tmp_path, _sch(sym))
        res = _enrich(sch)
        assert res["no_lcsc"] == 1
        assert res["updated"] == 0

    def test_skips_power_and_template_symbols(self, tmp_path: Any) -> None:
        pwr = _kicad_saved_symbol("power:GND", "#PWR01")
        tmpl = _kicad_saved_symbol("easyeda:PART", "_TEMPLATE_easyeda_PART", lcsc="C6186")
        real = _kicad_saved_symbol("easyeda:PART", "U2", lcsc="C6186")
        sch = _write(tmp_path, _sch(pwr + tmpl + real))
        res = _enrich(sch)
        assert res["skipped"] == 2
        assert res["total_symbols"] == 1
        assert res["updated"] == 1


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRobustness:
    def test_manufacturer_with_literal_parens_does_not_break_scan(self, tmp_path: Any) -> None:
        """easyeda values carry unescaped parens ("GigaDevice(兆易创新)"); the
        quote-aware paren matcher must not desync on them."""
        sym = _kicad_saved_symbol(
            "easyeda:GD32", "U2", lcsc="C6186", manufacturer="GigaDevice(兆易创新)"
        )
        sch = _write(tmp_path, _sch(sym))
        res = _enrich(sch)
        assert res["total_symbols"] == 1
        assert res["updated"] == 1

    def test_easyeda_property_layout_is_enumerated(self, tmp_path: Any) -> None:
        """Property NAME/VALUE on their own lines below (property must still fill."""
        sym = _easyeda_format_symbol("easyeda:PART", "U5", "C6186")
        sch = _write(tmp_path, _sch(sym))
        res = _enrich(sch)
        assert res["total_symbols"] == 1
        assert res["updated"] == 1
        assert "https://www.lcsc.com/datasheet/C6186.pdf" in sch.read_text()

    def test_dry_run_does_not_write(self, tmp_path: Any) -> None:
        sym = _kicad_saved_symbol("easyeda:PART", "U2", lcsc="C6186")
        sch = _write(tmp_path, _sch(sym))
        before = sch.read_text()
        res = _enrich(sch, dry_run=True)
        assert res["updated"] == 1
        assert res["dry_run"] is True
        assert res["details"][0]["dry_run"] is True
        assert sch.read_text() == before  # nothing written

    def test_multiple_updates_keep_offsets_consistent(self, tmp_path: Any) -> None:
        """Back-to-front splicing must land each URL on the right symbol."""
        s1 = _kicad_saved_symbol("easyeda:A", "U1", lcsc="C111")
        s2 = _kicad_saved_symbol("easyeda:B", "U2", lcsc="C222")
        s3 = _kicad_saved_symbol("easyeda:C", "U3", lcsc="C333")
        sch = _write(tmp_path, _sch(s1 + s2 + s3))
        res = _enrich(sch)
        assert res["updated"] == 3
        text = sch.read_text()
        for lcsc in ("C111", "C222", "C333"):
            assert f"https://www.lcsc.com/datasheet/{lcsc}.pdf" in text
        # Details are reported in schematic order.
        assert [d["lcsc"] for d in res["details"]] == ["C111", "C222", "C333"]


# ---------------------------------------------------------------------------
# Handler wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandler:
    def test_handler_enriches(self, tmp_path: Any) -> None:
        from handlers.datasheet import handle_enrich_datasheets

        sym = _kicad_saved_symbol("easyeda:PART", "U2", lcsc="C6186")
        sch = _write(tmp_path, _sch(sym))
        res = handle_enrich_datasheets(None, {"schematic_path": str(sch)})
        assert res["success"] is True
        assert res["updated"] == 1

    def test_handler_missing_path(self, tmp_path: Any) -> None:
        from handlers.datasheet import handle_enrich_datasheets

        res = handle_enrich_datasheets(None, {})
        assert res["success"] is False
