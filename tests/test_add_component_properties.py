"""Tests for Bug B: add_schematic_component / create_component_instance must copy
the library symbol's non-positional sourcing properties (Datasheet URL, LCSC Part,
MPN, Manufacturer, Description, …) onto the placed instance — exactly as KiCad does
on placement — instead of writing only Reference/Value/Footprint/Datasheet="~".

Library-internal ``ki_*`` fields (ki_keywords / ki_description / ki_fp_filters)
stay in the lib symbol and must NOT be stamped onto the instance.
"""

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import sexpdata

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.dynamic_symbol_loader import DynamicSymbolLoader

TEMPLATES_DIR = Path(__file__).parent.parent / "python" / "templates"
EMPTY_SCH = TEMPLATES_DIR / "empty.kicad_sch"


# An easyeda2kicad-style library: each (property …) writes NAME and VALUE on their
# own lines below "(property", carries a real Datasheet URL and BOM sourcing fields
# (Manufacturer with literal parens, MPN, "LCSC Part"), plus a library-internal
# ki_keywords that must NOT be copied. A second symbol has Datasheet "~" and no
# extra fields (the stock-part case).
EASYEDA_STYLE_LIB = """(kicad_symbol_lib
  (version 20211014)
  (generator easyeda2kicad)

  (symbol "GD32F103VET6"
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
      "GD32F103VET6"
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
      "Datasheet"
      "https://www.lcsc.com/datasheet/C6186.pdf"
      (id 3)
      (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property
      "Manufacturer"
      "GigaDevice(兆易创新)"
      (id 4)
      (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property
      "MPN"
      "GD32F103VET6"
      (id 5)
      (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property
      "LCSC Part"
      "C6186"
      (id 6)
      (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property
      "ki_keywords"
      "MCU ARM"
      (id 7)
      (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (symbol "GD32F103VET6_0_1"
      (rectangle
        (start -10 10)
        (end 10 -10)
        (stroke (width 0.254) (type default))
        (fill (type background))
      )
    )
    (symbol "GD32F103VET6_1_1"
      (pin power_in line
        (at -12.7 7.62 0)
        (length 2.54)
        (name "VDD" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27))))
      )
    )
  )

  (symbol "PLAINR"
    (in_bom yes)
    (on_board yes)
    (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "PLAINR" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (symbol "PLAINR_0_1"
      (rectangle
        (start -2 1)
        (end 2 -1)
        (stroke (width 0.254) (type default))
        (fill (type none))
      )
    )
  )
)
"""


def _make_project(tmp_path: Path) -> Path:
    """Lay out a project dir with an easyeda-style library + sym-lib-table."""
    lib_file = tmp_path / "testlib.kicad_sym"
    lib_file.write_text(EASYEDA_STYLE_LIB, encoding="utf-8")
    table = tmp_path / "sym-lib-table"
    table.write_text(
        "(sym_lib_table\n"
        f'  (lib (name "testlib")(type "KiCad")(uri "{lib_file}")(options "")(descr ""))\n'
        ")\n",
        encoding="utf-8",
    )
    return tmp_path


def _fresh_sch(tmp_path: Path) -> Path:
    import shutil

    sch = tmp_path / "test.kicad_sch"
    shutil.copy(EMPTY_SCH, sch)
    return sch


def _placed_instance_nodes(sch: Path) -> List[Any]:
    parsed = sexpdata.loads(sch.read_text(encoding="utf-8"))
    out = []
    for item in parsed[1:]:
        if not (isinstance(item, list) and item and item[0] == sexpdata.Symbol("symbol")):
            continue
        # Placed instances carry (lib_id …); library-definition symbols (inside
        # lib_symbols) are nested and never appear at the top level here.
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


def _prop_node(sch: Path, name: str) -> Optional[List[Any]]:
    for sub in _placed_instance_nodes(sch)[0]:
        if (
            isinstance(sub, list)
            and len(sub) >= 3
            and sub[0] == sexpdata.Symbol("property")
            and str(sub[1]) == name
        ):
            return sub
    return None


def _is_hidden(prop_node: List[Any]) -> bool:
    for el in prop_node:
        if isinstance(el, list) and el and el[0] == sexpdata.Symbol("effects"):
            for e in el:
                if isinstance(e, list) and e and e[0] == sexpdata.Symbol("hide"):
                    return str(e[1]) == "yes"
    return False


def _place(tmp_path: Path, symbol: str, **kwargs: Any) -> Path:
    project = _make_project(tmp_path)
    sch = _fresh_sch(tmp_path)
    loader = DynamicSymbolLoader(project_path=project)
    loader.add_component(sch, "testlib", symbol, project_path=project, **kwargs)
    return sch


@pytest.mark.unit
class TestSourcingPropertyCopy:
    def test_extra_sourcing_properties_are_copied(self, tmp_path: Any) -> None:
        sch = _place(tmp_path, "GD32F103VET6", reference="U2", value="GD32F103VET6", x=100, y=100)
        props = _instance_props(sch)
        assert props["Manufacturer"] == "GigaDevice(兆易创新)"
        assert props["MPN"] == "GD32F103VET6"
        assert props["LCSC Part"] == "C6186"

    def test_datasheet_copied_verbatim_from_library(self, tmp_path: Any) -> None:
        sch = _place(tmp_path, "GD32F103VET6", reference="U2", value="GD32F103VET6")
        assert _instance_props(sch)["Datasheet"] == "https://www.lcsc.com/datasheet/C6186.pdf"

    def test_copied_properties_are_hidden(self, tmp_path: Any) -> None:
        sch = _place(tmp_path, "GD32F103VET6", reference="U2")
        for name in ("LCSC Part", "MPN", "Manufacturer", "Datasheet"):
            node = _prop_node(sch, name)
            assert node is not None, f"{name} not copied onto instance"
            assert _is_hidden(node), f"{name} should be hidden on the instance"

    def test_ki_fields_are_not_copied(self, tmp_path: Any) -> None:
        sch = _place(tmp_path, "GD32F103VET6", reference="U2")
        assert "ki_keywords" not in _instance_props(sch)

    def test_reference_is_the_annotated_value_not_lib_default(self, tmp_path: Any) -> None:
        sch = _place(tmp_path, "GD32F103VET6", reference="U7", value="GD32F103VET6")
        props = _instance_props(sch)
        # Reference is the caller's annotated ref (U7), never the lib's "U".
        assert props["Reference"] == "U7"

    def test_value_uses_caller_value_not_lib_value(self, tmp_path: Any) -> None:
        sch = _place(tmp_path, "GD32F103VET6", reference="U2", value="CUSTOMVAL")
        assert _instance_props(sch)["Value"] == "CUSTOMVAL"

    def test_footprint_keeps_param_logic(self, tmp_path: Any) -> None:
        sch = _place(tmp_path, "GD32F103VET6", reference="U2", footprint="MyLib:CUSTOM_FP")
        assert _instance_props(sch)["Footprint"] == "MyLib:CUSTOM_FP"

    def test_stock_style_symbol_keeps_tilde_datasheet(self, tmp_path: Any) -> None:
        # PLAINR has Datasheet "~" and no sourcing fields: instance mirrors it.
        sch = _place(tmp_path, "PLAINR", reference="R1", value="10k")
        props = _instance_props(sch)
        assert props["Datasheet"] == "~"
        # Only the four standard fields, nothing spurious copied.
        assert set(props) == {"Reference", "Value", "Footprint", "Datasheet"}

    def test_schematic_stays_parseable(self, tmp_path: Any) -> None:
        # The Manufacturer value has non-ASCII text and literal parens; the file
        # must remain valid S-expression after the copy.
        sch = _place(tmp_path, "GD32F103VET6", reference="U2")
        parsed = sexpdata.loads(sch.read_text(encoding="utf-8"))
        assert parsed[0] == sexpdata.Symbol("kicad_sch")

    def test_create_component_instance_direct_copies_properties(self, tmp_path: Any) -> None:
        # The lower-level entry point (used by the template/clone path) copies too.
        project = _make_project(tmp_path)
        sch = _fresh_sch(tmp_path)
        loader = DynamicSymbolLoader(project_path=project)
        loader.create_component_instance(
            sch, "testlib", "GD32F103VET6", reference="U9", value="GD32F103VET6", x=50, y=50
        )
        props = _instance_props(sch)
        assert props["LCSC Part"] == "C6186"
        assert props["Reference"] == "U9"

    def test_missing_library_falls_back_gracefully(self, tmp_path: Any) -> None:
        # No sym-lib-table / library on disk: placement still works with the
        # minimal template (Datasheet "~", no extras) rather than failing.
        import shutil

        sch = tmp_path / "test.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        loader = DynamicSymbolLoader(project_path=tmp_path)
        ok = loader.create_component_instance(
            sch, "NoSuchLib", "NoSuchPart", reference="U1", value="v", x=10, y=10
        )
        assert ok is True
        props = _instance_props(sch)
        assert props["Datasheet"] == "~"
        assert set(props) == {"Reference", "Value", "Footprint", "Datasheet"}
