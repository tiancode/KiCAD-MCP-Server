"""
Tests for python/commands/hierarchy_sheet.py — hierarchical-sheet authoring.

Covers:
  - create_hierarchical_sheet: block insertion, paren balance, child file
    creation, page-number assignment, duplicate/absolute-path refusals
  - add_sheet_pin: pin placement inside the correct sheet block, side
    positioning, 2.54 mm auto-stacking, matching child hierarchical_label,
    duplicate-pin / unknown-sheet / bad-shape refusals
  - round-trip: parent keeps a single (kicad_sch root and pre-existing
    content untouched after both operations
"""

import re
import sys
from pathlib import Path
from typing import Tuple

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.hierarchy_sheet import add_sheet_pin, create_hierarchical_sheet  # noqa: E402

_ROOT_UUID = "5b9623a5-6d01-41fc-9865-e1bc779418c8"

# Minimal parent: version header, uuid, one placed symbol, sheet_instances.
_PARENT = f"""(kicad_sch
\t(version 20250114)
\t(generator "eeschema")
\t(generator_version "9.0")
\t(uuid "{_ROOT_UUID}")
\t(paper "A4")
\t(lib_symbols)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 30.48 30.48 0)
\t\t(uuid "cccc3333-3333-4333-8333-cccccccccccc")
\t\t(property "Reference" "R1"
\t\t\t(at 33 30.48 0)
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


def _write_parent(tmp_path: Path) -> Path:
    sch = tmp_path / "main.kicad_sch"
    sch.write_text(_PARENT, encoding="utf-8")
    return sch


def _pin_at(content: str, pin_name: str) -> Tuple[float, float, float]:
    """Extract (x, y, angle) of a named sheet pin from schematic text."""
    m = re.search(
        r'\(pin\s+"' + re.escape(pin_name) + r'"[^(]*\(at\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)',
        content,
        re.DOTALL,
    )
    assert m, f"pin {pin_name!r} with (at ...) not found"
    return float(m.group(1)), float(m.group(2)), float(m.group(3))


@pytest.mark.unit
class TestCreateHierarchicalSheet:
    def test_inserts_sheet_block(self, tmp_path: Path) -> None:
        sch = _write_parent(tmp_path)
        result = create_hierarchical_sheet(
            str(sch),
            sheet_name="Power",
            child_filename="power.kicad_sch",
            position=(50.8, 50.8),
        )
        assert result["success"] is True, result
        assert result["sheetName"] == "Power"
        assert result["sheetFile"] == "power.kicad_sch"
        assert result["page"] == "2"

        content = sch.read_text(encoding="utf-8")
        assert '(property "Sheetname" "Power"' in content
        assert '(property "Sheetfile" "power.kicad_sch"' in content
        assert f'(path "/{_ROOT_UUID}"' in content
        assert '(page "2")' in content
        assert f'(uuid "{result["uuid"]}")' in content
        assert content.count("(") == content.count(")")
        # Sheet block goes before the root sheet_instances trailer.
        assert content.find("(sheet\n") < content.find("(sheet_instances")

    def test_child_file_created_with_valid_header(self, tmp_path: Path) -> None:
        sch = _write_parent(tmp_path)
        result = create_hierarchical_sheet(
            str(sch),
            sheet_name="Power",
            child_filename="power.kicad_sch",
            position=(50.8, 50.8),
        )
        assert result["childCreated"] is True
        child = tmp_path / "power.kicad_sch"
        assert child.exists()
        child_content = child.read_text(encoding="utf-8")
        assert child_content.lstrip().startswith("(kicad_sch")
        assert "(version 20250114)" in child_content
        assert '(paper "A4")' in child_content
        assert child_content.count("(") == child_content.count(")")
        # Fresh uuid, not the parent's (child comes from SchematicManager's
        # template, whose top-level uuid is written unquoted).
        m = re.search(r'\(uuid\s+"?([0-9a-f-]+)"?\)', child_content)
        assert m and m.group(1) != _ROOT_UUID

    def test_existing_child_not_overwritten(self, tmp_path: Path) -> None:
        sch = _write_parent(tmp_path)
        child = tmp_path / "power.kicad_sch"
        child.write_text('(kicad_sch\n\t(version 20250114)\n\t(uuid "aa")\n)\n', encoding="utf-8")
        result = create_hierarchical_sheet(
            str(sch),
            sheet_name="Power",
            child_filename="power.kicad_sch",
            position=(50.8, 50.8),
        )
        assert result["success"] is True
        assert result["childCreated"] is False
        assert '(uuid "aa")' in child.read_text(encoding="utf-8")

    def test_page_number_with_existing_sheet(self, tmp_path: Path) -> None:
        sch = _write_parent(tmp_path)
        r1 = create_hierarchical_sheet(
            str(sch),
            sheet_name="Power",
            child_filename="power.kicad_sch",
            position=(50.8, 50.8),
        )
        r2 = create_hierarchical_sheet(
            str(sch),
            sheet_name="MCU",
            child_filename="mcu.kicad_sch",
            position=(120.0, 50.8),
        )
        assert r1["page"] == "2"
        assert r2["success"] is True, r2
        assert r2["page"] == "3"
        content = sch.read_text(encoding="utf-8")
        assert '(page "2")' in content
        assert '(page "3")' in content
        assert content.count("(") == content.count(")")

    def test_duplicate_sheet_name_refused(self, tmp_path: Path) -> None:
        sch = _write_parent(tmp_path)
        create_hierarchical_sheet(
            str(sch),
            sheet_name="Power",
            child_filename="power.kicad_sch",
            position=(50.8, 50.8),
        )
        result = create_hierarchical_sheet(
            str(sch),
            sheet_name="Power",
            child_filename="power2.kicad_sch",
            position=(120.0, 50.8),
        )
        assert result["success"] is False
        assert "already exists" in result["message"]

    def test_absolute_child_filename_refused(self, tmp_path: Path) -> None:
        sch = _write_parent(tmp_path)
        result = create_hierarchical_sheet(
            str(sch),
            sheet_name="Power",
            child_filename=str(tmp_path / "power.kicad_sch"),
            position=(50.8, 50.8),
        )
        assert result["success"] is False
        assert "absolute" in result["message"].lower()

    def test_missing_parent_refused(self, tmp_path: Path) -> None:
        result = create_hierarchical_sheet(
            str(tmp_path / "nope.kicad_sch"),
            sheet_name="Power",
            child_filename="power.kicad_sch",
            position=(50.8, 50.8),
        )
        assert result["success"] is False
        assert "not found" in result["message"].lower()

    def test_unparsable_parent_refused(self, tmp_path: Path) -> None:
        sch = tmp_path / "broken.kicad_sch"
        sch.write_text("(kicad_sch (version 20250114)", encoding="utf-8")
        result = create_hierarchical_sheet(
            str(sch),
            sheet_name="Power",
            child_filename="power.kicad_sch",
            position=(50.8, 50.8),
        )
        assert result["success"] is False
        assert "parseable" in result["message"].lower()


@pytest.mark.unit
class TestAddSheetPin:
    def _parent_with_sheet(self, tmp_path: Path) -> Path:
        sch = _write_parent(tmp_path)
        result = create_hierarchical_sheet(
            str(sch),
            sheet_name="Power",
            child_filename="power.kicad_sch",
            position=(50.0, 50.0),
            size=(50.0, 40.0),
        )
        assert result["success"] is True, result
        return sch

    def test_pin_inserted_inside_sheet_block(self, tmp_path: Path) -> None:
        sch = self._parent_with_sheet(tmp_path)
        result = add_sheet_pin(
            str(sch), sheet_name="Power", pin_name="EN", shape="input", side="left"
        )
        assert result["success"] is True, result
        content = sch.read_text(encoding="utf-8")
        assert '(pin "EN" input' in content
        assert content.count("(") == content.count(")")
        # Pin must land inside the sheet block (after its Sheetname, before
        # the sheet_instances trailer that follows the block).
        pin_pos = content.find('(pin "EN"')
        assert content.find('"Sheetname" "Power"') < pin_pos < content.find("(sheet_instances")

    def test_pin_position_on_left_side_within_bounds(self, tmp_path: Path) -> None:
        sch = self._parent_with_sheet(tmp_path)
        result = add_sheet_pin(
            str(sch), sheet_name="Power", pin_name="EN", shape="input", side="left"
        )
        x, y, angle = _pin_at(sch.read_text(encoding="utf-8"), "EN")
        assert x == 50.0  # left edge
        assert 50.0 <= y <= 90.0  # within sheet vertical span
        assert angle == 180  # KiCad convention: left-side pin points 180
        assert result["pin"]["position"] == [50.0, 52.54]

    def test_pin_position_on_right_side(self, tmp_path: Path) -> None:
        sch = self._parent_with_sheet(tmp_path)
        add_sheet_pin(str(sch), sheet_name="Power", pin_name="OUT", shape="output", side="right")
        x, y, angle = _pin_at(sch.read_text(encoding="utf-8"), "OUT")
        assert x == 100.0  # right edge = 50 + 50
        assert 50.0 <= y <= 90.0
        assert angle == 0

    def test_pins_auto_stack_on_same_side(self, tmp_path: Path) -> None:
        sch = self._parent_with_sheet(tmp_path)
        r1 = add_sheet_pin(str(sch), sheet_name="Power", pin_name="EN", shape="input", side="left")
        r2 = add_sheet_pin(
            str(sch), sheet_name="Power", pin_name="FAULT", shape="output", side="left"
        )
        assert r1["success"] and r2["success"]
        content = sch.read_text(encoding="utf-8")
        _, y1, _ = _pin_at(content, "EN")
        x2, y2, _ = _pin_at(content, "FAULT")
        assert x2 == 50.0  # same (left) edge
        assert round(y2 - y1, 4) == 2.54  # stacked one step below
        assert content.count("(") == content.count(")")

    def test_child_hierarchical_label_added(self, tmp_path: Path) -> None:
        sch = self._parent_with_sheet(tmp_path)
        result = add_sheet_pin(
            str(sch), sheet_name="Power", pin_name="EN", shape="input", side="left"
        )
        assert result["childLabelAdded"] is True
        child_content = (tmp_path / "power.kicad_sch").read_text(encoding="utf-8")
        assert '(hierarchical_label "EN"' in child_content
        assert "(shape input)" in child_content
        assert child_content.count("(") == child_content.count(")")

    def test_child_labels_stack(self, tmp_path: Path) -> None:
        sch = self._parent_with_sheet(tmp_path)
        add_sheet_pin(str(sch), sheet_name="Power", pin_name="A", shape="input", side="left")
        add_sheet_pin(str(sch), sheet_name="Power", pin_name="B", shape="input", side="left")
        child_content = (tmp_path / "power.kicad_sch").read_text(encoding="utf-8")
        ys = [float(m.group(1)) for m in re.finditer(r"\(at 25\.4 ([-\d.]+) 0\)", child_content)]
        assert len(ys) == 2
        assert round(abs(ys[1] - ys[0]), 4) == 2.54

    def test_no_child_label_when_disabled(self, tmp_path: Path) -> None:
        sch = self._parent_with_sheet(tmp_path)
        result = add_sheet_pin(
            str(sch),
            sheet_name="Power",
            pin_name="EN",
            shape="input",
            side="left",
            add_child_label=False,
        )
        assert result["success"] is True
        assert result["childLabelAdded"] is False
        child_content = (tmp_path / "power.kicad_sch").read_text(encoding="utf-8")
        assert "hierarchical_label" not in child_content

    def test_duplicate_pin_refused(self, tmp_path: Path) -> None:
        sch = self._parent_with_sheet(tmp_path)
        add_sheet_pin(str(sch), sheet_name="Power", pin_name="EN", shape="input", side="left")
        result = add_sheet_pin(
            str(sch), sheet_name="Power", pin_name="EN", shape="output", side="right"
        )
        assert result["success"] is False
        assert "already has a pin" in result["message"]

    def test_unknown_sheet_refused(self, tmp_path: Path) -> None:
        sch = self._parent_with_sheet(tmp_path)
        result = add_sheet_pin(
            str(sch), sheet_name="NoSuchSheet", pin_name="EN", shape="input", side="left"
        )
        assert result["success"] is False
        assert "not found" in result["message"].lower()

    def test_invalid_shape_refused(self, tmp_path: Path) -> None:
        sch = self._parent_with_sheet(tmp_path)
        result = add_sheet_pin(
            str(sch), sheet_name="Power", pin_name="EN", shape="wrong", side="left"
        )
        assert result["success"] is False
        assert "shape" in result["message"].lower()

    def test_invalid_side_refused(self, tmp_path: Path) -> None:
        sch = self._parent_with_sheet(tmp_path)
        result = add_sheet_pin(
            str(sch), sheet_name="Power", pin_name="EN", shape="input", side="diagonal"
        )
        assert result["success"] is False
        assert "side" in result["message"].lower()


@pytest.mark.unit
class TestRoundTrip:
    def test_parent_still_single_root_and_untouched_content(self, tmp_path: Path) -> None:
        sch = _write_parent(tmp_path)
        create_hierarchical_sheet(
            str(sch),
            sheet_name="Power",
            child_filename="power.kicad_sch",
            position=(50.0, 50.0),
        )
        add_sheet_pin(str(sch), sheet_name="Power", pin_name="EN", shape="input", side="left")

        content = sch.read_text(encoding="utf-8")
        assert content.count("(kicad_sch") == 1
        assert content.count("(") == content.count(")")
        # Pre-existing content untouched: the placed R1 symbol and root uuid.
        assert '(property "Reference" "R1"' in content
        assert '(lib_id "Device:R")' in content
        assert f'(uuid "{_ROOT_UUID}")' in content
        assert '(path "/"\n\t\t\t(page "1")' in content  # root sheet_instances intact

        # The whole file must still parse as a single (kicad_sch ...) tree.
        import sexpdata

        tree = sexpdata.loads(content)
        assert tree[0] == sexpdata.Symbol("kicad_sch")

        child_tree = sexpdata.loads((tmp_path / "power.kicad_sch").read_text(encoding="utf-8"))
        assert child_tree[0] == sexpdata.Symbol("kicad_sch")
