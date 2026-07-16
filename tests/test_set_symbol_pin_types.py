"""S7 tests — set_symbol_pin_types: fix a symbol's pin electrical types.

Companion to the easyeda import-time inference (test_easyeda_pin_types.py):
where inference guesses types at import, this tool lets an agent SET them
afterwards, on either the ``.kicad_sym`` source or a schematic's embedded
``lib_symbols`` snapshot, so the unclearable pin_to_pin "Unspecified …
connected" ERC warnings can be retyped away. All fixture-based — no network.
"""

import sys
from pathlib import Path

import pytest
import sexpdata

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))

import commands.easyeda_import as ee  # noqa: E402
import commands.symbol_pin_types as spt  # noqa: E402
from handlers.symbol_pin_types import handle_set_symbol_pin_types  # noqa: E402

# A .kicad_sym with one symbol whose pins are all blanket ``unspecified`` —
# exactly the shape easyeda2kicad produces. VDD/GND (power), SDA (bus), LOUT
# (audio out), NC (no-connect), MUTE (fallback → passive).
_LIB = """\
(kicad_symbol_lib
  (version 20211014)
  (generator https://github.com/uPesy/easyeda2kicad.py)
  (symbol "RDA5807M"
    (property "Reference" "U" (id 0) (at 0 0 0))
    (property "Value" "RDA5807M" (id 1) (at 0 0 0))
    (property "LCSC Part" "C123456" (id 6) (at 0 0 0))
    (symbol "RDA5807M_1_1"
      (pin unspecified line (at -10 5 0) (length 5)
        (name "VDD" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin unspecified line (at -10 0 0) (length 5)
        (name "GND" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27)))))
      (pin unspecified line (at -10 -5 0) (length 5)
        (name "SDA" (effects (font (size 1.27 1.27))))
        (number "3" (effects (font (size 1.27 1.27)))))
      (pin unspecified line (at 10 5 180) (length 5)
        (name "LOUT" (effects (font (size 1.27 1.27))))
        (number "4" (effects (font (size 1.27 1.27)))))
      (pin unspecified line (at 10 0 180) (length 5)
        (name "NC" (effects (font (size 1.27 1.27))))
        (number "5" (effects (font (size 1.27 1.27)))))
      (pin unspecified line (at 10 -5 180) (length 5)
        (name "MUTE" (effects (font (size 1.27 1.27))))
        (number "6" (effects (font (size 1.27 1.27)))))
    )
  )
)
"""


def _lib_pin_types(lib_path, symbol_name):
    """Map pin name → electrical type inside a top-level symbol block."""
    content = lib_path.read_text(encoding="utf-8")
    span = ee._symbol_span(content, symbol_name)
    return _pins_in(content[span[0] : span[1]])


def _pins_in(block):
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


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_valid_pin_types_set_matches_kicad():
    assert "power_in" in spt.VALID_PIN_TYPES
    assert "bidirectional" in spt.VALID_PIN_TYPES
    assert len(spt.VALID_PIN_TYPES) == 12


@pytest.mark.unit
def test_invalid_types_detected():
    lookup = spt.normalize_mapping({"VDD": "power_in", "SDA": "bogus", "1": "alsobad"})
    bad = spt.invalid_types(lookup)
    assert bad == {"SDA": "bogus", "1": "alsobad"}


# ---------------------------------------------------------------------------
# Block rewrite core
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_rewrite_matches_by_name_and_number():
    block = ee._symbol_span(_LIB, "RDA5807M")
    sym = _LIB[block[0] : block[1]]
    lookup = spt.normalize_mapping({"VDD": "power_in", "gnd": "power_in", "3": "bidirectional"})
    new_sym, records, matched = spt.rewrite_pins_in_block(sym, lookup)
    types = _pins_in(new_sym)
    assert types["VDD"] == "power_in"
    assert types["GND"] == "power_in"  # case-insensitive name match
    assert types["SDA"] == "bidirectional"  # matched by NUMBER "3"
    # Untouched pins keep their original type.
    assert types["LOUT"] == "unspecified"
    assert matched == {"VDD", "GND", "3"}


@pytest.mark.unit
def test_rewrite_number_takes_precedence_over_name():
    # Craft a lookup where a key could match either; number wins.
    block = ee._symbol_span(_LIB, "RDA5807M")
    sym = _LIB[block[0] : block[1]]
    # "1" is VDD's number. A pin whose NAME is also numeric doesn't exist here,
    # so this just proves number matching works.
    _, records, matched = spt.rewrite_pins_in_block(sym, {"1": "power_out"})
    rec = records[0]
    assert rec["number"] == "1" and rec["name"] == "VDD"
    assert rec["new_type"] == "power_out"


# ---------------------------------------------------------------------------
# Library-file surface
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_apply_to_library_retypes_and_reads_back(tmp_path):
    lib = tmp_path / "easyeda.kicad_sym"
    lib.write_text(_LIB, encoding="utf-8")
    lookup = spt.normalize_mapping(
        {
            "VDD": "power_in",
            "GND": "power_in",
            "SDA": "bidirectional",
            "LOUT": "output",
            "NC": "no_connect",
            "MUTE": "passive",
        }
    )
    res = spt.apply_to_library(lib, "RDA5807M", lookup)
    assert res["success"] is True
    assert res["target"] == "library"
    assert res["changed"] == 6
    assert res["wrote"] is True
    types = _lib_pin_types(lib, "RDA5807M")
    assert types == {
        "VDD": "power_in",
        "GND": "power_in",
        "SDA": "bidirectional",
        "LOUT": "output",
        "NC": "no_connect",
        "MUTE": "passive",
    }
    # No unspecified survives → the ERC pin_to_pin noise is gone.
    assert "unspecified" not in types.values()
    # Still a valid s-expression.
    sexpdata.loads(lib.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_apply_to_library_idempotent_no_write(tmp_path):
    lib = tmp_path / "easyeda.kicad_sym"
    lib.write_text(_LIB, encoding="utf-8")
    lookup = spt.normalize_mapping({"VDD": "power_in"})
    spt.apply_to_library(lib, "RDA5807M", lookup)
    mtime = lib.stat().st_mtime_ns
    # Second application is a no-op: already power_in, nothing to write.
    res = spt.apply_to_library(lib, "RDA5807M", lookup)
    assert res["changed"] == 0
    assert res["wrote"] is False
    assert res["matched"] == 1  # still matched, just unchanged
    assert lib.stat().st_mtime_ns == mtime


@pytest.mark.unit
def test_apply_to_library_reports_unmatched(tmp_path):
    lib = tmp_path / "easyeda.kicad_sym"
    lib.write_text(_LIB, encoding="utf-8")
    lookup = spt.normalize_mapping({"VDD": "power_in", "NONEXISTENT": "input"})
    res = spt.apply_to_library(lib, "RDA5807M", lookup)
    assert res["unmatched_keys"] == ["NONEXISTENT"]


@pytest.mark.unit
def test_apply_to_library_symbol_not_found(tmp_path):
    lib = tmp_path / "easyeda.kicad_sym"
    lib.write_text(_LIB, encoding="utf-8")
    with pytest.raises(spt.SymbolPinTypeError):
        spt.apply_to_library(lib, "NOPE", spt.normalize_mapping({"VDD": "power_in"}))


# ---------------------------------------------------------------------------
# Schematic-embedded surface
# ---------------------------------------------------------------------------
# A schematic with the imported symbol embedded (all unspecified) and one
# placed instance U1 referencing it.
_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator eeschema)
  (lib_symbols
    (symbol "easyeda:RDA5807M"
      (property "Reference" "U" (at 0 0 0))
      (property "Value" "RDA5807M" (at 0 0 0))
      (symbol "easyeda:RDA5807M_1_1"
        (pin unspecified line (at -10 5 0) (length 5)
          (name "VDD" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin unspecified line (at -10 0 0) (length 5)
          (name "GND" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27)))))
        (pin unspecified line (at -10 -5 0) (length 5)
          (name "SDA" (effects (font (size 1.27 1.27))))
          (number "3" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "easyeda:RDA5807M") (at 100 80 0) (unit 1)
    (property "Reference" "U1" (at 100 70 0))
    (property "Value" "RDA5807M" (at 100 90 0))
  )
)
"""


@pytest.mark.unit
def test_find_reference_lib_id():
    assert spt.find_reference_lib_id(_SCHEMATIC, "U1") == "easyeda:RDA5807M"
    assert spt.find_reference_lib_id(_SCHEMATIC, "U9") is None


@pytest.mark.unit
def test_apply_to_schematic_retypes_embedded(tmp_path):
    sch = tmp_path / "demo.kicad_sch"
    sch.write_text(_SCHEMATIC, encoding="utf-8")
    lookup = spt.normalize_mapping({"VDD": "power_in", "GND": "power_in", "SDA": "bidirectional"})
    res = spt.apply_to_schematic(sch, "easyeda:RDA5807M", lookup)
    assert res["success"] is True
    assert res["target"] == "schematic"
    assert res["changed"] == 3
    # Read the embedded copy back.
    content = sch.read_text(encoding="utf-8")
    ls_start = content.find("(lib_symbols")
    ls_end = ee._match_paren(content, ls_start)
    span = ee._symbol_span(content[ls_start:ls_end], "easyeda:RDA5807M")
    embedded = content[ls_start:ls_end][span[0] : span[1]]
    types = _pins_in(embedded)
    assert types == {"VDD": "power_in", "GND": "power_in", "SDA": "bidirectional"}
    # The placed instance is untouched (still one root, valid s-expr).
    assert content.count("(kicad_sch") == 1
    sexpdata.loads(content)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_handler_requires_pin_types():
    res = handle_set_symbol_pin_types(None, {"symbolId": "easyeda:RDA5807M"})
    assert res["success"] is False
    assert "pinTypes" in res["message"]


@pytest.mark.unit
def test_handler_rejects_invalid_type():
    res = handle_set_symbol_pin_types(
        None,
        {"libraryPath": "/x.kicad_sym", "symbolName": "Y", "pinTypes": {"VDD": "nope"}},
    )
    assert res["success"] is False
    assert res["invalid_types"] == {"VDD": "nope"}


@pytest.mark.unit
def test_handler_library_path_mode(tmp_path):
    lib = tmp_path / "easyeda.kicad_sym"
    lib.write_text(_LIB, encoding="utf-8")
    res = handle_set_symbol_pin_types(
        None,
        {
            "libraryPath": str(lib),
            "symbolName": "RDA5807M",
            "pinTypes": {"VDD": "power_in", "SDA": "bidirectional"},
        },
    )
    assert res["success"] is True
    types = _lib_pin_types(lib, "RDA5807M")
    assert types["VDD"] == "power_in" and types["SDA"] == "bidirectional"


@pytest.mark.unit
def test_handler_symbol_id_easyeda_fast_path(tmp_path, monkeypatch):
    lib = tmp_path / "easyeda.kicad_sym"
    lib.write_text(_LIB, encoding="utf-8")
    monkeypatch.setattr(ee, "SYMBOL_LIB_PATH", lib)
    # Force the sym-lib-table lookup to miss so the easyeda cache fallback fires
    # (keeps the test hermetic regardless of the host's registered libraries).
    import commands.dynamic_symbol_loader as dsl

    monkeypatch.setattr(dsl.DynamicSymbolLoader, "find_library_file", lambda self, n: None)

    res = handle_set_symbol_pin_types(
        None, {"symbolId": "easyeda:RDA5807M", "pinTypes": {"GND": "power_in"}}
    )
    assert res["success"] is True
    assert res["file"] == str(lib)
    assert _lib_pin_types(lib, "RDA5807M")["GND"] == "power_in"


@pytest.mark.unit
def test_handler_schematic_by_reference(tmp_path):
    sch = tmp_path / "demo.kicad_sch"
    sch.write_text(_SCHEMATIC, encoding="utf-8")
    res = handle_set_symbol_pin_types(
        None,
        {
            "schematicPath": str(sch),
            "reference": "U1",
            "pinTypes": {"VDD": "power_in", "GND": "power_in", "SDA": "bidirectional"},
        },
    )
    assert res["success"] is True
    assert res["target"] == "schematic"
    assert res["symbol"] == "easyeda:RDA5807M"
    assert res["changed"] == 3


@pytest.mark.unit
def test_handler_schematic_reference_not_found(tmp_path):
    sch = tmp_path / "demo.kicad_sch"
    sch.write_text(_SCHEMATIC, encoding="utf-8")
    res = handle_set_symbol_pin_types(
        None, {"schematicPath": str(sch), "reference": "U9", "pinTypes": {"VDD": "power_in"}}
    )
    assert res["success"] is False
    assert "U9" in res["message"]


@pytest.mark.unit
def test_handler_needs_a_target():
    res = handle_set_symbol_pin_types(None, {"pinTypes": {"VDD": "power_in"}})
    assert res["success"] is False
    assert "target" in res["message"].lower()


# ---------------------------------------------------------------------------
# ERC-relevant: unspecified count drops to zero on the embedded copy
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_embedded_unspecified_count_drops_to_zero(tmp_path):
    sch = tmp_path / "demo.kicad_sch"
    sch.write_text(_SCHEMATIC, encoding="utf-8")

    def _embedded_types():
        content = sch.read_text(encoding="utf-8")
        ls_start = content.find("(lib_symbols")
        ls_end = ee._match_paren(content, ls_start)
        span = ee._symbol_span(content[ls_start:ls_end], "easyeda:RDA5807M")
        return _pins_in(content[ls_start:ls_end][span[0] : span[1]])

    before = _embedded_types()
    assert list(before.values()).count("unspecified") == 3

    handle_set_symbol_pin_types(
        None,
        {
            "schematicPath": str(sch),
            "symbolId": "easyeda:RDA5807M",
            "pinTypes": {"VDD": "power_in", "GND": "power_in", "SDA": "bidirectional"},
        },
    )
    after = _embedded_types()
    assert list(after.values()).count("unspecified") == 0


# ---------------------------------------------------------------------------
# Composition with refresh_schematic_lib_symbols
# ---------------------------------------------------------------------------
# A schematic whose embedded snapshot of Device:RDA5807M is the stale
# all-unspecified copy; the on-disk Device.kicad_sym is the source of truth.
_COMPOSE_SCH = """\
(kicad_sch
  (version 20231120)
  (generator eeschema)
  (lib_symbols
    (symbol "Device:RDA5807M"
      (property "Reference" "U" (at 0 0 0))
      (property "Value" "RDA5807M" (at 0 0 0))
      (symbol "Device:RDA5807M_1_1"
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

_COMPOSE_LIB = """\
(kicad_symbol_lib
  (version 20231120)
  (generator kicad_symbol_editor)
  (symbol "RDA5807M"
    (property "Reference" "U" (at 0 0 0))
    (property "Value" "RDA5807M" (at 0 0 0))
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


@pytest.mark.unit
def test_set_pin_types_composes_with_refresh(monkeypatch, tmp_path):
    """set_symbol_pin_types edits the .kicad_sym SOURCE; then
    refresh_schematic_lib_symbols pushes the typed copy into the schematic's
    embedded lib_symbols so ERC picks it up — the documented library-mode flow.
    """
    # Keep the loader hermetic (see test_refresh_schematic_lib_symbols).
    monkeypatch.setattr(
        "commands.dynamic_symbol_loader.DynamicSymbolLoader._global_sym_lib_table_paths",
        lambda self: [],
    )
    lib_dir = tmp_path / "symbols"
    lib_dir.mkdir()
    lib_path = lib_dir / "Device.kicad_sym"
    lib_path.write_text(_COMPOSE_LIB, encoding="utf-8")

    sch_path = tmp_path / "demo.kicad_sch"
    sch_path.write_text(_COMPOSE_SCH, encoding="utf-8")

    # 1) Retype the library source.
    handle_set_symbol_pin_types(
        None,
        {
            "libraryPath": str(lib_path),
            "symbolName": "RDA5807M",
            "pinTypes": {"VDD": "power_in", "GND": "power_in"},
        },
    )
    assert _lib_pin_types(lib_path, "RDA5807M") == {"VDD": "power_in", "GND": "power_in"}

    # 2) Refresh the embedded snapshot from disk.
    from commands.dynamic_symbol_loader import DynamicSymbolLoader

    loader = DynamicSymbolLoader(project_path=tmp_path)
    monkeypatch.setattr(loader, "find_kicad_symbol_libraries", lambda: [lib_dir])
    out = loader.refresh_embedded_lib_symbols(sch_path)
    assert out["success"] is True
    assert out["refreshed"] == ["Device:RDA5807M"]

    # 3) The embedded copy now carries the typed pins.
    content = sch_path.read_text(encoding="utf-8")
    ls_start = content.find("(lib_symbols")
    ls_end = ee._match_paren(content, ls_start)
    span = ee._symbol_span(content[ls_start:ls_end], "Device:RDA5807M")
    embedded = content[ls_start:ls_end][span[0] : span[1]]
    types = _pins_in(embedded)
    assert types == {"VDD": "power_in", "GND": "power_in"}
    assert "unspecified" not in types.values()


# ---------------------------------------------------------------------------
# A13 — pin-type override marker persists across run_erc's pre-refresh
# ---------------------------------------------------------------------------
# set_symbol_pin_types (schematic mode) rewrites the EMBEDDED lib_symbols pin
# types.  run_erc's pre-ERC refresh_schematic_lib_symbols wholesale-replaces
# each embedded entry with the on-disk .kicad_sym copy, silently REVERTING the
# deliberate edit (and persisting the revert).  The fix stamps a hidden
# ``ki_pin_type_override`` marker so the refresh re-applies the marked pins
# onto the fresh copy instead of dropping them.
@pytest.mark.unit
def test_marker_serialize_roundtrip():
    ov = {"1": "output", "GND": "power_in", "SDA": "bidirectional"}
    s = spt.serialize_pin_overrides(ov)
    assert spt.deserialize_pin_overrides(s) == ov
    # Deterministic (sorted) so apply/refresh stamps are byte-identical.
    reversed_ov = dict(reversed(list(ov.items())))
    assert spt.serialize_pin_overrides(ov) == spt.serialize_pin_overrides(reversed_ov)
    # Bad types are dropped, never persisted.
    assert spt.serialize_pin_overrides({"1": "bogus"}) == ""


@pytest.mark.unit
def test_apply_to_schematic_stamps_override_marker(tmp_path):
    sch = tmp_path / "demo.kicad_sch"
    sch.write_text(_SCHEMATIC, encoding="utf-8")
    res = spt.apply_to_schematic(
        sch, "easyeda:RDA5807M", spt.normalize_mapping({"1": "output", "GND": "power_in"})
    )
    assert res["success"] is True
    content = sch.read_text(encoding="utf-8")
    ls_start = content.find("(lib_symbols")
    ls_end = ee._match_paren(content, ls_start)
    span = ee._symbol_span(content[ls_start:ls_end], "easyeda:RDA5807M")
    embedded = content[ls_start:ls_end][span[0] : span[1]]
    assert spt.PIN_TYPE_OVERRIDE_PROP in embedded
    assert spt.read_pin_overrides(embedded) == {"1": "output", "GND": "power_in"}
    # Still a single valid s-expression root.
    assert content.count("(kicad_sch") == 1
    sexpdata.loads(content)


@pytest.mark.unit
def test_apply_to_schematic_merges_override_marker_across_calls(tmp_path):
    sch = tmp_path / "demo.kicad_sch"
    sch.write_text(_SCHEMATIC, encoding="utf-8")
    spt.apply_to_schematic(sch, "easyeda:RDA5807M", spt.normalize_mapping({"1": "output"}))
    spt.apply_to_schematic(sch, "easyeda:RDA5807M", spt.normalize_mapping({"2": "power_in"}))
    content = sch.read_text(encoding="utf-8")
    ls_start = content.find("(lib_symbols")
    ls_end = ee._match_paren(content, ls_start)
    span = ee._symbol_span(content[ls_start:ls_end], "easyeda:RDA5807M")
    embedded = content[ls_start:ls_end][span[0] : span[1]]
    # Both edits recorded; exactly one marker property (no duplicates).
    assert spt.read_pin_overrides(embedded) == {"1": "output", "2": "power_in"}
    assert embedded.count(f'"{spt.PIN_TYPE_OVERRIDE_PROP}"') == 1


@pytest.mark.unit
def test_apply_to_library_does_not_stamp_marker(tmp_path):
    # The library file is the source of truth — no override marker belongs there.
    lib = tmp_path / "easyeda.kicad_sym"
    lib.write_text(_LIB, encoding="utf-8")
    spt.apply_to_library(lib, "RDA5807M", spt.normalize_mapping({"VDD": "power_in"}))
    assert spt.PIN_TYPE_OVERRIDE_PROP not in lib.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Wiring: the command is registered and dispatchable
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_command_registered_in_handler_map():
    from kicad_interface import KiCADInterface

    assert KiCADInterface._HANDLER_MAP["set_symbol_pin_types"] == "symbol_pin_types"
    import handlers.symbol_pin_types as h

    assert hasattr(h, "handle_set_symbol_pin_types")
