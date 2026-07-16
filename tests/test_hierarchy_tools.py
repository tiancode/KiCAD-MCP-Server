"""
Tests for add_schematic_hierarchical_label and add_sheet_pin tools.

Covers:
  - Hierarchical label insertion with correct S-expression format
  - Sheet pin insertion into the correct sheet block
  - Parameter validation (missing required fields)
  - Orientation and justification mapping
  - Sheet-not-found error handling
"""

import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


def _make_iface() -> Any:
    with patch("kicad_interface.USE_IPC_BACKEND", False):
        from kicad_interface import KiCADInterface

        iface = KiCADInterface.__new__(KiCADInterface)
    return iface


@pytest.fixture()
def iface():
    return _make_iface()


# ---------------------------------------------------------------------------
# Minimal schematic templates
# ---------------------------------------------------------------------------

_MINIMAL_SUBSHEET = textwrap.dedent(
    """\
    (kicad_sch
    \t(version 20231120)
    \t(generator "eeschema")
    \t(generator_version "9.0")
    \t(sheet_instances
    \t\t(path "/"
    \t\t\t(page "1")
    \t\t)
    \t)
    )
"""
)

_MINIMAL_PARENT = textwrap.dedent(
    """\
    (kicad_sch
    \t(version 20231120)
    \t(generator "eeschema")
    \t(sheet
    \t\t(at 100 50)
    \t\t(size 40 30)
    \t\t(property "Sheetname" "Storage"
    \t\t\t(at 100 49 0)
    \t\t\t(effects
    \t\t\t\t(font
    \t\t\t\t\t(size 1.27 1.27)
    \t\t\t\t)
    \t\t\t)
    \t\t)
    \t\t(property "Sheetfile" "sheets/storage.kicad_sch"
    \t\t\t(at 100 82 0)
    \t\t\t(effects
    \t\t\t\t(font
    \t\t\t\t\t(size 1.27 1.27)
    \t\t\t\t)
    \t\t\t)
    \t\t)
    \t)
    \t(sheet_instances
    \t\t(path "/"
    \t\t\t(page "1")
    \t\t)
    \t)
    )
"""
)

_ROOT_WITH_UUID = textwrap.dedent(
    """\
    (kicad_sch
    \t(version 20250114)
    \t(generator "eeschema")
    \t(uuid "5b9623a5-6d01-41fc-9865-e1bc779418c8")
    \t(paper "A4")
    \t(lib_symbols
    \t)
    \t(sheet_instances
    \t\t(path "/"
    \t\t\t(page "1")
    \t\t)
    \t)
    )
"""
)


_PARENT_TWO_SHEETS = textwrap.dedent(
    """\
    (kicad_sch
    \t(version 20231120)
    \t(sheet
    \t\t(at 50 50)
    \t\t(size 40 30)
    \t\t(property "Sheetname" "Power"
    \t\t\t(at 50 49 0)
    \t\t\t(effects (font (size 1.27 1.27)))
    \t\t)
    \t\t(property "Sheetfile" "sheets/power.kicad_sch"
    \t\t\t(at 50 82 0)
    \t\t\t(effects (font (size 1.27 1.27)))
    \t\t)
    \t)
    \t(sheet
    \t\t(at 150 50)
    \t\t(size 40 30)
    \t\t(property "Sheetname" "Storage"
    \t\t\t(at 150 49 0)
    \t\t\t(effects (font (size 1.27 1.27)))
    \t\t)
    \t\t(property "Sheetfile" "sheets/storage.kicad_sch"
    \t\t\t(at 150 82 0)
    \t\t\t(effects (font (size 1.27 1.27)))
    \t\t)
    \t)
    \t(sheet_instances
    \t\t(path "/" (page "1"))
    \t)
    )
"""
)


# ===========================================================================
# Hierarchical label tests
# ===========================================================================


@pytest.mark.unit
class TestAddHierarchicalLabel:
    def test_inserts_label_into_subsheet(self, iface, tmp_path):
        sch = tmp_path / "sub.kicad_sch"
        sch.write_text(_MINIMAL_SUBSHEET)

        result = iface._handle_add_schematic_hierarchical_label(
            {
                "schematicPath": str(sch),
                "text": "SD_CLK",
                "position": [50.8, 25.4],
                "shape": "output",
            }
        )

        assert result["success"] is True
        content = sch.read_text()
        assert '(hierarchical_label "SD_CLK"' in content
        assert "(shape output)" in content
        assert "(at 50.8 25.4 0)" in content

    def test_orientation_180_uses_right_justify(self, iface, tmp_path):
        sch = tmp_path / "sub.kicad_sch"
        sch.write_text(_MINIMAL_SUBSHEET)

        result = iface._handle_add_schematic_hierarchical_label(
            {
                "schematicPath": str(sch),
                "text": "VBUS",
                "position": [10, 20],
                "shape": "input",
                "orientation": 180,
            }
        )

        assert result["success"] is True
        content = sch.read_text()
        assert "(at 10 20 180)" in content
        assert "(justify right)" in content

    def test_orientation_0_uses_left_justify(self, iface, tmp_path):
        sch = tmp_path / "sub.kicad_sch"
        sch.write_text(_MINIMAL_SUBSHEET)

        result = iface._handle_add_schematic_hierarchical_label(
            {
                "schematicPath": str(sch),
                "text": "SDA",
                "position": [30, 40],
                "shape": "bidirectional",
                "orientation": 0,
            }
        )

        assert result["success"] is True
        content = sch.read_text()
        assert "(justify left)" in content

    def test_missing_text_fails(self, iface, tmp_path):
        sch = tmp_path / "sub.kicad_sch"
        sch.write_text(_MINIMAL_SUBSHEET)

        result = iface._handle_add_schematic_hierarchical_label(
            {
                "schematicPath": str(sch),
                "position": [10, 20],
                "shape": "input",
            }
        )

        assert result["success"] is False
        assert "text" in result["message"].lower()

    def test_invalid_shape_fails(self, iface, tmp_path):
        sch = tmp_path / "sub.kicad_sch"
        sch.write_text(_MINIMAL_SUBSHEET)

        result = iface._handle_add_schematic_hierarchical_label(
            {
                "schematicPath": str(sch),
                "text": "SIG",
                "position": [10, 20],
                "shape": "passive",
            }
        )

        assert result["success"] is False

    def test_nonexistent_file_fails(self, iface, tmp_path):
        result = iface._handle_add_schematic_hierarchical_label(
            {
                "schematicPath": str(tmp_path / "nope.kicad_sch"),
                "text": "SIG",
                "position": [10, 20],
                "shape": "input",
            }
        )

        assert result["success"] is False
        assert "not found" in result["message"].lower()

    def test_inserts_before_sheet_instances(self, iface, tmp_path):
        sch = tmp_path / "sub.kicad_sch"
        sch.write_text(_MINIMAL_SUBSHEET)

        iface._handle_add_schematic_hierarchical_label(
            {
                "schematicPath": str(sch),
                "text": "TEST",
                "position": [10, 20],
                "shape": "input",
            }
        )

        content = sch.read_text()
        label_pos = content.find("hierarchical_label")
        instances_pos = content.find("sheet_instances")
        assert (
            label_pos < instances_pos
        ), "Hierarchical label should be inserted before sheet_instances"


# ===========================================================================
# Sheet pin tests
# ===========================================================================


@pytest.mark.unit
class TestAddSheetPin:
    def test_inserts_pin_into_correct_sheet(self, iface, tmp_path):
        sch = tmp_path / "parent.kicad_sch"
        sch.write_text(_MINIMAL_PARENT)

        result = iface._handle_add_sheet_pin(
            {
                "schematicPath": str(sch),
                "sheetName": "Storage",
                "pinName": "SD_CLK",
                "shape": "output",
                "position": [140, 60],
            }
        )

        assert result["success"] is True
        content = sch.read_text()
        assert '(pin "SD_CLK" output' in content
        assert "(at 140 60 0)" in content

    def test_pin_in_multi_sheet_parent_targets_correct_sheet(self, iface, tmp_path):
        sch = tmp_path / "parent.kicad_sch"
        sch.write_text(_PARENT_TWO_SHEETS)

        result = iface._handle_add_sheet_pin(
            {
                "schematicPath": str(sch),
                "sheetName": "Storage",
                "pinName": "SD_D0",
                "shape": "bidirectional",
                "position": [190, 60],
            }
        )

        assert result["success"] is True
        content = sch.read_text()
        # Pin should be inside the Storage sheet block, not the Power block
        storage_pos = content.find('"Storage"')
        pin_pos = content.find('"SD_D0"')
        power_end = content.find('"Power"')
        assert pin_pos > storage_pos, "Pin should be after Storage sheet name"

    def test_sheet_not_found_fails(self, iface, tmp_path):
        sch = tmp_path / "parent.kicad_sch"
        sch.write_text(_MINIMAL_PARENT)

        result = iface._handle_add_sheet_pin(
            {
                "schematicPath": str(sch),
                "sheetName": "NonExistent",
                "pinName": "SIG",
                "shape": "input",
                "position": [100, 50],
            }
        )

        assert result["success"] is False
        assert "not found" in result["message"].lower()

    def test_missing_pin_name_fails(self, iface, tmp_path):
        sch = tmp_path / "parent.kicad_sch"
        sch.write_text(_MINIMAL_PARENT)

        result = iface._handle_add_sheet_pin(
            {
                "schematicPath": str(sch),
                "sheetName": "Storage",
                "shape": "input",
                "position": [100, 50],
            }
        )

        assert result["success"] is False

    def test_orientation_180_uses_right_justify(self, iface, tmp_path):
        sch = tmp_path / "parent.kicad_sch"
        sch.write_text(_MINIMAL_PARENT)

        result = iface._handle_add_sheet_pin(
            {
                "schematicPath": str(sch),
                "sheetName": "Storage",
                "pinName": "VBUS",
                "shape": "input",
                "position": [100, 60],
                "orientation": 180,
            }
        )

        assert result["success"] is True
        content = sch.read_text()
        assert "(at 100 60 180)" in content
        assert "(justify right)" in content

    def test_pintype_deprecated_alias_still_accepted(self, iface, tmp_path):
        # A9: `shape` is canonical, but the old `pinType` key must still work.
        sch = tmp_path / "parent.kicad_sch"
        sch.write_text(_MINIMAL_PARENT)

        result = iface._handle_add_sheet_pin(
            {
                "schematicPath": str(sch),
                "sheetName": "Storage",
                "pinName": "SD_CMD",
                "pinType": "output",  # deprecated alias for shape
                "position": [140, 62],
            }
        )

        assert result["success"] is True, result
        assert '(pin "SD_CMD" output' in sch.read_text()

    def test_broadened_shape_enum_accepts_passive(self, iface, tmp_path):
        # A9: enum breadth aligned with sibling tools (5 KiCad sheet-pin shapes).
        sch = tmp_path / "parent.kicad_sch"
        sch.write_text(_MINIMAL_PARENT)

        result = iface._handle_add_sheet_pin(
            {
                "schematicPath": str(sch),
                "sheetName": "Storage",
                "pinName": "NC",
                "shape": "passive",
                "position": [140, 64],
            }
        )

        assert result["success"] is True, result
        assert '(pin "NC" passive' in sch.read_text()

    def test_add_pin_on_compact_single_line_sheet_block(self, iface, tmp_path):
        # A10: a (sheet ...) block serialized COMPACT on a single line (mixed with
        # a compact parent) must still be found — the old line-anchored
        # ^\s*\(sheet lookup silently missed it and reported "not found".
        sch = tmp_path / "compact.kicad_sch"
        sch.write_text(
            '(kicad_sch (version 20250114) (generator "x") '
            '(uuid "5b9623a5-6d01-41fc-9865-e1bc779418c8") (paper "A4") (lib_symbols) '
            '(sheet (at 180 50) (size 50 40) (uuid "aaaa1111-1111-4111-8111-aaaaaaaaaaaa") '
            '(property "Sheetname" "power" (at 180 49 0)) '
            '(property "Sheetfile" "power_child.kicad_sch" (at 180 91 0))) '
            '(sheet_instances (path "/" (page "1"))))'
        )

        result = iface._handle_add_sheet_pin(
            {
                "schematicPath": str(sch),
                "sheetName": "power",
                "pinName": "GND",
                "shape": "input",
                "position": [180, 55],
                "orientation": 180,
            }
        )

        assert result["success"] is True, result
        content = sch.read_text()
        assert '(pin "GND" input' in content
        assert content.count("(") == content.count(")")
        import sexpdata

        assert sexpdata.loads(content)[0] == sexpdata.Symbol("kicad_sch")


# ===========================================================================
# Hierarchical sheet box tests
# ===========================================================================

_ROOT_UUID = "5b9623a5-6d01-41fc-9865-e1bc779418c8"


@pytest.mark.unit
class TestAddSchematicSheet:
    def _root(self, tmp_path):
        sch = tmp_path / "board.kicad_sch"
        sch.write_text(_ROOT_WITH_UUID)
        return sch

    def test_inserts_sheet_block_with_props(self, iface, tmp_path):
        sch = self._root(tmp_path)
        result = iface._handle_add_schematic_sheet(
            {
                "schematicPath": str(sch),
                "sheetName": "Power",
                "sheetFile": "power.kicad_sch",
                "position": [50.8, 50.8],
                "createSubSheet": False,
            }
        )
        assert result["success"] is True, result
        content = sch.read_text()
        assert '(property "Sheetname" "Power"' in content
        assert '(property "Sheetfile" "power.kicad_sch"' in content
        # Page lives in the block's own (instances), keyed on the ROOT uuid.
        assert f'(path "/{_ROOT_UUID}"' in content
        assert '(page "2")' in content

    def test_root_sheet_instances_untouched(self, iface, tmp_path):
        sch = self._root(tmp_path)
        iface._handle_add_schematic_sheet(
            {
                "schematicPath": str(sch),
                "sheetName": "Power",
                "sheetFile": "power.kicad_sch",
                "position": [50.8, 50.8],
                "createSubSheet": False,
            }
        )
        content = sch.read_text()
        si = content[content.find("(sheet_instances") :]
        # KiCad 9/10 does not list sub-sheets in the root sheet_instances.
        assert si.count("(path") == 1, si

    def test_page_number_auto_increments(self, iface, tmp_path):
        sch = self._root(tmp_path)
        r1 = iface._handle_add_schematic_sheet(
            {
                "schematicPath": str(sch),
                "sheetName": "Power",
                "sheetFile": "power.kicad_sch",
                "position": [50, 50],
                "createSubSheet": False,
            }
        )
        r2 = iface._handle_add_schematic_sheet(
            {
                "schematicPath": str(sch),
                "sheetName": "MCU",
                "sheetFile": "mcu.kicad_sch",
                "position": [120, 50],
                "createSubSheet": False,
            }
        )
        assert r1["page"] == "2"
        assert r2["page"] == "3"

    def test_explicit_page_number(self, iface, tmp_path):
        sch = self._root(tmp_path)
        result = iface._handle_add_schematic_sheet(
            {
                "schematicPath": str(sch),
                "sheetName": "Power",
                "sheetFile": "power.kicad_sch",
                "position": [50, 50],
                "pageNumber": 7,
                "createSubSheet": False,
            }
        )
        assert result["page"] == "7"
        assert '(page "7")' in sch.read_text()

    def test_creates_sub_sheet_when_missing(self, iface, tmp_path):
        sch = self._root(tmp_path)
        result = iface._handle_add_schematic_sheet(
            {
                "schematicPath": str(sch),
                "sheetName": "Power",
                "sheetFile": "power.kicad_sch",
                "position": [50, 50],
            }
        )
        assert result["success"] is True
        assert result["createdSubSheet"] is True
        assert (tmp_path / "power.kicad_sch").exists()

    def test_created_sub_sheet_has_no_placeholder_components(self, iface, tmp_path):
        # The auto-created sub-sheet must be genuinely empty — the default
        # create template carries offscreen _TEMPLATE_* placeholder instances
        # that would otherwise surface as phantom parts in ERC/BOM.
        sch = self._root(tmp_path)
        iface._handle_add_schematic_sheet(
            {
                "schematicPath": str(sch),
                "sheetName": "Power",
                "sheetFile": "power.kicad_sch",
                "position": [50, 50],
            }
        )
        sub = (tmp_path / "power.kicad_sch").read_text()
        assert "_TEMPLATE" not in sub
        assert "(symbol (lib_id" not in sub

    def test_skip_create_sub_sheet(self, iface, tmp_path):
        sch = self._root(tmp_path)
        result = iface._handle_add_schematic_sheet(
            {
                "schematicPath": str(sch),
                "sheetName": "Power",
                "sheetFile": "power.kicad_sch",
                "position": [50, 50],
                "createSubSheet": False,
            }
        )
        assert result["success"] is True
        assert result["createdSubSheet"] is False
        assert not (tmp_path / "power.kicad_sch").exists()

    def test_absolute_sheetfile_normalized_to_relative(self, iface, tmp_path):
        sch = self._root(tmp_path)
        abs_sub = tmp_path / "sub" / "power.kicad_sch"
        result = iface._handle_add_schematic_sheet(
            {
                "schematicPath": str(sch),
                "sheetName": "Power",
                "sheetFile": str(abs_sub),
                "position": [50, 50],
                "createSubSheet": False,
            }
        )
        assert result["success"] is True
        assert result["sheetFile"] == str(Path("sub") / "power.kicad_sch")
        assert '(property "Sheetfile" "sub/power.kicad_sch"' in sch.read_text()

    def test_discover_finds_new_sheet(self, iface, tmp_path):
        from commands.wire_connectivity import _discover_sub_sheets

        sch = self._root(tmp_path)
        iface._handle_add_schematic_sheet(
            {
                "schematicPath": str(sch),
                "sheetName": "Power",
                "sheetFile": "power.kicad_sch",
                "position": [50, 50],
            }
        )
        subs = [Path(s).name for s in _discover_sub_sheets(str(sch))]
        assert "power.kicad_sch" in subs

    def test_result_still_loads_as_sexpr(self, iface, tmp_path):
        import sexpdata

        sch = self._root(tmp_path)
        iface._handle_add_schematic_sheet(
            {
                "schematicPath": str(sch),
                "sheetName": "Power",
                "sheetFile": "power.kicad_sch",
                "position": [50, 50],
                "createSubSheet": False,
            }
        )
        tree = sexpdata.loads(sch.read_text())
        assert tree[0] == sexpdata.Symbol("kicad_sch")

    def test_missing_sheet_file_fails(self, iface, tmp_path):
        sch = self._root(tmp_path)
        result = iface._handle_add_schematic_sheet(
            {
                "schematicPath": str(sch),
                "sheetName": "Power",
                "position": [50, 50],
            }
        )
        assert result["success"] is False
        assert "sheetfile" in result["message"].lower()

    def test_nonexistent_parent_fails(self, iface, tmp_path):
        result = iface._handle_add_schematic_sheet(
            {
                "schematicPath": str(tmp_path / "nope.kicad_sch"),
                "sheetName": "Power",
                "sheetFile": "power.kicad_sch",
                "position": [50, 50],
            }
        )
        assert result["success"] is False
        assert "not found" in result["message"].lower()
