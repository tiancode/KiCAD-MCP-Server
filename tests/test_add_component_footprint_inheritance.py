"""S14: add_schematic_component must inherit a footprint when the caller passes
none.

Repro that motivated this: import_jlcpcb_symbols(["C80215"]) writes an easyeda
symbol whose .kicad_sym records Footprint "easyeda:LQFP-100_…", then
add_schematic_component (no footprint arg) placed it with an EMPTY Footprint
field, so sync_schematic_to_board skipped the IC ("no Library:Name footprint
set on schematic symbol"). Native KiCad symbols with a library default footprint
(AMS1117-3.3 → SOT-223) were dropped the same way.

Required behaviour:
  (a) library symbols (native or imported .kicad_sym) inherit their own
      Footprint property value when non-empty,
  (b) an explicit footprint arg always wins,
  (c) if neither exists the field stays "" and the response says so.

The easyeda import path must also carry the Footprint property in the generated
.kicad_sym so (a) works — verified fixture-based (no network) at the bottom.
"""

import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
import sexpdata

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.dynamic_symbol_loader import DynamicSymbolLoader
from handlers.schematic_component import handle_add_schematic_component

TEMPLATES_DIR = Path(__file__).parent.parent / "python" / "templates"
EMPTY_SCH = TEMPLATES_DIR / "empty.kicad_sch"


# A project library carrying three cases:
#   REG_SOT223 — native-style symbol with a real Footprint default (like the
#                stock Regulator_Linear:AMS1117-3.3 → SOT-223).
#   IC_EASYEDA — easyeda2kicad's multiline property format (NAME + VALUE on
#                their own lines) with an "easyeda:LQFP-100" footprint.
#   NOFP       — a symbol whose Footprint property is empty (the stock-part
#                case: nothing to inherit).
FIXTURE_LIB = """(kicad_symbol_lib
  (version 20211014)
  (generator test)

  (symbol "REG_SOT223"
    (in_bom yes)
    (on_board yes)
    (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "AMS1117-3.3" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Package_TO_SOT_SMD:SOT-223-3_TabPin2" (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (symbol "REG_SOT223_0_1"
      (rectangle (start -5 5) (end 5 -5)
        (stroke (width 0.254) (type default)) (fill (type none))))
  )

  (symbol "IC_EASYEDA"
    (in_bom yes)
    (on_board yes)
    (property
      "Reference"
      "U"
      (id 0)
      (at 0 20 0)
      (effects (font (size 1.27 1.27)))
    )
    (property
      "Value"
      "IC_EASYEDA"
      (id 1)
      (at 0 -20 0)
      (effects (font (size 1.27 1.27)))
    )
    (property
      "Footprint"
      "easyeda:LQFP-100"
      (id 2)
      (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property
      "LCSC Part"
      "C80215"
      (id 6)
      (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (symbol "IC_EASYEDA_0_1"
      (rectangle (start -10 10) (end 10 -10)
        (stroke (width 0.254) (type default)) (fill (type background))))
  )

  (symbol "NOFP"
    (in_bom yes)
    (on_board yes)
    (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "NOFP" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (symbol "NOFP_0_1"
      (rectangle (start -2 1) (end 2 -1)
        (stroke (width 0.254) (type default)) (fill (type none))))
  )
)
"""


def _make_project(tmp_path: Path) -> Path:
    """Project dir with the fixture library registered in a sym-lib-table.

    The handler derives its project path by walking up from the schematic to
    the dir owning the sym-lib-table, so schematic + table live together here.
    """
    lib_file = tmp_path / "testlib.kicad_sym"
    lib_file.write_text(FIXTURE_LIB, encoding="utf-8")
    table = tmp_path / "sym-lib-table"
    table.write_text(
        "(sym_lib_table\n"
        f'  (lib (name "testlib")(type "KiCad")(uri "{lib_file}")(options "")(descr ""))\n'
        ")\n",
        encoding="utf-8",
    )
    return tmp_path


def _fresh_sch(tmp_path: Path) -> Path:
    sch = tmp_path / "test.kicad_sch"
    shutil.copy(EMPTY_SCH, sch)
    return sch


def _placed_instance_nodes(sch: Path) -> List[Any]:
    parsed = sexpdata.loads(sch.read_text(encoding="utf-8"))
    out = []
    for item in parsed[1:]:
        if not (isinstance(item, list) and item and item[0] == sexpdata.Symbol("symbol")):
            continue
        if any(isinstance(s, list) and s and s[0] == sexpdata.Symbol("lib_id") for s in item):
            out.append(item)
    return out


def _instance_props(sch: Path) -> Dict[str, str]:
    props: Dict[str, str] = {}
    nodes = _placed_instance_nodes(sch)
    assert nodes, "no placed instance found"
    for sub in nodes[0]:
        if isinstance(sub, list) and len(sub) >= 3 and sub[0] == sexpdata.Symbol("property"):
            props[str(sub[1])] = str(sub[2])
    return props


def _call_add(sch: Path, symbol: str, reference: str, **extra: Any) -> Dict[str, Any]:
    component: Dict[str, Any] = {
        "library": "testlib",
        "type": symbol,
        "reference": reference,
        "x": 50,
        "y": 50,
    }
    component.update(extra)
    return handle_add_schematic_component(None, {"schematicPath": str(sch), "component": component})


# ---------------------------------------------------------------------------
# get_library_footprint read helper (placement-read path)
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestGetLibraryFootprint:
    def test_reads_native_style_default(self, tmp_path: Any) -> None:
        project = _make_project(tmp_path)
        loader = DynamicSymbolLoader(project_path=project)
        assert (
            loader.get_library_footprint("testlib", "REG_SOT223")
            == "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
        )

    def test_reads_easyeda_multiline_property_format(self, tmp_path: Any) -> None:
        project = _make_project(tmp_path)
        loader = DynamicSymbolLoader(project_path=project)
        assert loader.get_library_footprint("testlib", "IC_EASYEDA") == "easyeda:LQFP-100"

    def test_empty_footprint_returns_empty(self, tmp_path: Any) -> None:
        project = _make_project(tmp_path)
        loader = DynamicSymbolLoader(project_path=project)
        assert loader.get_library_footprint("testlib", "NOFP") == ""

    def test_missing_symbol_returns_empty(self, tmp_path: Any) -> None:
        project = _make_project(tmp_path)
        loader = DynamicSymbolLoader(project_path=project)
        assert loader.get_library_footprint("testlib", "NoSuchSymbol") == ""

    def test_missing_library_returns_empty(self, tmp_path: Any) -> None:
        loader = DynamicSymbolLoader(project_path=tmp_path)
        assert loader.get_library_footprint("NoSuchLib", "NoSuchPart") == ""


# ---------------------------------------------------------------------------
# Placement inheritance through the add_schematic_component handler
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestPlacementFootprintInheritance:
    def test_native_default_footprint_is_inherited(self, tmp_path: Any) -> None:
        project = _make_project(tmp_path)
        sch = _fresh_sch(project)
        result = _call_add(sch, "REG_SOT223", "U1")  # no footprint arg
        assert result["success"] is True
        assert _instance_props(sch)["Footprint"] == "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
        assert result["footprint"] == "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
        assert result["footprintSource"] == "library"

    def test_easyeda_import_footprint_is_inherited(self, tmp_path: Any) -> None:
        # The core repro: an easyeda-style symbol placed with no footprint arg
        # must land its recorded footprint so sync_schematic_to_board keeps it.
        project = _make_project(tmp_path)
        sch = _fresh_sch(project)
        result = _call_add(sch, "IC_EASYEDA", "U1")
        assert result["success"] is True
        assert _instance_props(sch)["Footprint"] == "easyeda:LQFP-100"
        assert result["footprintSource"] == "library"

    def test_explicit_footprint_arg_wins_over_library_default(self, tmp_path: Any) -> None:
        project = _make_project(tmp_path)
        sch = _fresh_sch(project)
        result = _call_add(sch, "REG_SOT223", "U1", footprint="MyLib:CUSTOM_FP")
        assert result["success"] is True
        assert _instance_props(sch)["Footprint"] == "MyLib:CUSTOM_FP"
        assert result["footprint"] == "MyLib:CUSTOM_FP"
        assert result["footprintSource"] == "explicit"

    def test_no_footprint_anywhere_stays_empty_and_notes_it(self, tmp_path: Any) -> None:
        project = _make_project(tmp_path)
        sch = _fresh_sch(project)
        result = _call_add(sch, "NOFP", "R1")
        assert result["success"] is True
        assert _instance_props(sch)["Footprint"] == ""
        assert result["footprint"] == ""
        assert result["footprintSource"] == "none"
        # The response must flag that sync will skip this footprint-less symbol.
        assert "footprintNote" in result
        assert "sync_schematic_to_board" in result["footprintNote"]

    def test_place_all_units_inherits_footprint_on_every_unit(self, tmp_path: Any) -> None:
        # Footprint is a per-part field; every placed unit gets the inherited FP.
        project = _make_project(tmp_path)
        sch = _fresh_sch(project)
        result = _call_add(sch, "IC_EASYEDA", "U1", placeAllUnits=True)
        assert result["success"] is True
        # Whatever units were placed, none may carry an empty Footprint.
        for node in _placed_instance_nodes(sch):
            fp = None
            for sub in node:
                if (
                    isinstance(sub, list)
                    and len(sub) >= 3
                    and sub[0] == sexpdata.Symbol("property")
                    and str(sub[1]) == "Footprint"
                ):
                    fp = str(sub[2])
            assert fp == "easyeda:LQFP-100"


# ---------------------------------------------------------------------------
# easyeda import must carry the Footprint property (fixture-based, no network)
# ---------------------------------------------------------------------------
_EASYEDA_FIXTURE_LIB = """\
(kicad_symbol_lib
  (version 20211014)
  (generator https://github.com/uPesy/easyeda2kicad.py)
  (symbol "NE555DR"
    (property "Reference" "U" (id 0) (at 0 0 0))
    (property "Value" "NE555DR" (id 1) (at 0 0 0))
    (property "Footprint" "easyeda:SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL" (id 2) (at 0 0 0))
    (property "LCSC Part" "C7593" (id 6) (at 0 0 0))
    (symbol "NE555DR_0_1")
  )
)
"""


@pytest.mark.unit
class TestEasyEdaImportCarriesFootprint:
    @pytest.fixture
    def env(self, tmp_path: Any, monkeypatch: Any) -> Any:
        import subprocess
        from types import SimpleNamespace

        import commands.easyeda_import as ee

        cache = tmp_path / "cache"
        sym = cache / "easyeda.kicad_sym"
        pretty = cache / "easyeda.pretty"
        cfg = tmp_path / "config" / "kicad" / "10.0"

        monkeypatch.setattr(ee, "_CACHE_DIR", cache)
        monkeypatch.setattr(ee, "SYMBOL_LIB_PATH", sym)
        monkeypatch.setattr(ee, "FOOTPRINT_LIB_DIR", pretty)

        def _cfg_dir() -> Path:
            cfg.mkdir(parents=True, exist_ok=True)
            return cfg

        monkeypatch.setattr(ee, "_resolve_global_config_dir", _cfg_dir)

        def _runner(cmd: Any, timeout: Any) -> Any:
            sym.parent.mkdir(parents=True, exist_ok=True)
            sym.write_text(_EASYEDA_FIXTURE_LIB, encoding="utf-8")
            pretty.mkdir(parents=True, exist_ok=True)
            (pretty / "SOIC-8.kicad_mod").write_text("(footprint)", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(ee, "_run", _runner)
        return SimpleNamespace(ee=ee, sym=sym)

    def test_generated_kicad_sym_has_footprint_property(self, env: Any) -> None:
        env.ee.import_lcsc_part("C7593")
        syms = env.ee._parse_symbols(env.sym)
        assert len(syms) == 1
        fp = syms[0]["properties"].get("Footprint")
        assert fp == "easyeda:SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL"
        assert fp, "imported .kicad_sym must carry a non-empty Footprint so placement can inherit"

    def test_import_response_advertises_footprint(self, env: Any) -> None:
        resp = env.ee.import_lcsc_part("C7593")
        assert resp["success"] is True
        assert resp["footprint"] == "easyeda:SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL"

    def test_get_library_footprint_reads_imported_symbol(self, env: Any, tmp_path: Any) -> None:
        # End-to-end for requirement (a): the placement read path recovers the
        # imported footprint straight from the generated .kicad_sym file.
        env.ee.import_lcsc_part("C7593")
        # Point a project sym-lib-table at the freshly-written cache library so
        # the loader resolves it without depending on real user config.
        table = tmp_path / "sym-lib-table"
        table.write_text(
            "(sym_lib_table\n"
            f'  (lib (name "easyeda")(type "KiCad")(uri "{env.sym}")(options "")(descr ""))\n'
            ")\n",
            encoding="utf-8",
        )
        loader = DynamicSymbolLoader(project_path=tmp_path)
        assert (
            loader.get_library_footprint("easyeda", "NE555DR")
            == "easyeda:SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL"
        )
