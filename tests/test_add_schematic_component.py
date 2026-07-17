"""
Tests for add_schematic_component handler, focusing on the unit parameter
for multi-unit symbols (e.g. quad optocouplers, dual op-amps).
"""

import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

TEMPLATES_DIR = Path(__file__).parent.parent / "python" / "templates"
EMPTY_SCH = TEMPLATES_DIR / "empty.kicad_sch"


def _write_temp_sch(content: str) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False, mode="w", encoding="utf-8")
    tmp.write(content)
    tmp.close()
    return Path(tmp.name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unit_values_in_file(path: Path) -> list[int]:
    """Return all (unit N) values written for symbol instances in the schematic."""
    content = path.read_text()
    # Match top-level symbol instances: (symbol (lib_id ...) (at ...) (unit N) ...)
    return [
        int(n)
        for n in re.findall(r"\(symbol \(lib_id [^)]+\) \(at [^)]+\) \(unit (\d+)\)", content)
    ]


# ---------------------------------------------------------------------------
# Unit tests – create_component_instance
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateComponentInstanceUnit:
    """Tests for DynamicSymbolLoader.create_component_instance unit parameter."""

    def setup_method(self) -> None:
        from commands.dynamic_symbol_loader import DynamicSymbolLoader

        self.DynamicSymbolLoader = DynamicSymbolLoader

    def _loader(self) -> Any:
        return self.DynamicSymbolLoader()

    def test_default_unit_is_1(self, tmp_path: Any) -> None:
        sch = tmp_path / "test.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        loader = self._loader()
        loader.create_component_instance(
            sch, "Device", "R", reference="R1", value="10k", x=10, y=10
        )
        units = _unit_values_in_file(sch)
        assert 1 in units

    def test_explicit_unit_1(self, tmp_path: Any) -> None:
        sch = tmp_path / "test.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        loader = self._loader()
        loader.create_component_instance(
            sch, "Device", "R", reference="R1", value="10k", x=10, y=10, unit=1
        )
        units = _unit_values_in_file(sch)
        assert units.count(1) >= 1

    def test_unit_2_written_correctly(self, tmp_path: Any) -> None:
        sch = tmp_path / "test.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        loader = self._loader()
        loader.create_component_instance(
            sch, "Device", "R", reference="U1", value="TLP291-4", x=10, y=10, unit=2
        )
        units = _unit_values_in_file(sch)
        assert 2 in units

    def test_unit_4_written_correctly(self, tmp_path: Any) -> None:
        sch = tmp_path / "test.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        loader = self._loader()
        loader.create_component_instance(
            sch, "Device", "R", reference="U1", value="TLP291-4", x=10, y=10, unit=4
        )
        units = _unit_values_in_file(sch)
        assert 4 in units

    def test_instances_block_uses_same_unit(self, tmp_path: Any) -> None:
        """The (instances ...) path block must also record the correct unit number."""
        sch = tmp_path / "test.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        loader = self._loader()
        loader.create_component_instance(
            sch, "Device", "R", reference="U1", value="val", x=5, y=5, unit=3
        )
        content = sch.read_text()
        # The (unit 3) inside the (instances ...) block
        assert "(unit 3)" in content
        # Count occurrences — should appear at least twice (symbol header + instances)
        assert content.count("(unit 3)") >= 2

    def test_multiple_units_same_reference(self, tmp_path: Any) -> None:
        """Placing units A and B of the same reference produces two distinct unit entries."""
        sch = tmp_path / "test.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        loader = self._loader()
        loader.create_component_instance(
            sch, "Device", "R", reference="U10", value="TLP291-4", x=10, y=10, unit=1
        )
        loader.create_component_instance(
            sch, "Device", "R", reference="U10", value="TLP291-4", x=10, y=35, unit=2
        )
        units = _unit_values_in_file(sch)
        assert 1 in units
        assert 2 in units


# ---------------------------------------------------------------------------
# Handler-level tests – _handle_add_schematic_component
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerAddSchematicComponent:
    """Tests for KiCADInterface._handle_add_schematic_component unit plumbing."""

    def _call_handler(self, params: dict) -> dict:
        from kicad_interface import KiCADInterface

        iface = KiCADInterface()
        return iface._handle_add_schematic_component(params)

    def test_missing_schematic_path_returns_error(self) -> None:
        result = self._call_handler({"component": {"type": "R", "library": "Device"}})
        assert result["success"] is False
        assert "path" in result["message"].lower() or "schematic" in result["message"].lower()

    def test_missing_component_returns_error(self, tmp_path: Any) -> None:
        sch = tmp_path / "test.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        result = self._call_handler({"schematicPath": str(sch)})
        assert result["success"] is False

    def test_unit_defaults_to_1_in_handler(self, tmp_path: Any) -> None:
        sch = tmp_path / "test.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        result = self._call_handler(
            {
                "schematicPath": str(sch),
                "component": {
                    "library": "Device",
                    "type": "R",
                    "reference": "R99",
                    "value": "1k",
                    "x": 10,
                    "y": 10,
                    # no "unit" key — should default to 1
                },
            }
        )
        assert result["success"] is True
        units = _unit_values_in_file(sch)
        assert 1 in units

    def test_unit_2_passed_through_handler(self, tmp_path: Any) -> None:
        sch = tmp_path / "test.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        result = self._call_handler(
            {
                "schematicPath": str(sch),
                "component": {
                    "library": "Device",
                    "type": "R",
                    "reference": "U10",
                    "value": "TLP291-4",
                    "x": 25,
                    "y": 35,
                    "unit": 2,
                },
            }
        )
        assert result["success"] is True
        units = _unit_values_in_file(sch)
        assert 2 in units


# ---------------------------------------------------------------------------
# Hierarchical sub-sheets — no (sheet_instances ...) block
# ---------------------------------------------------------------------------


# Minimal sub-sheet: same outer (kicad_sch ...) form as a root schematic but
# WITHOUT (sheet_instances ...). Hierarchical KiCad designs only carry that
# block in the root .kicad_sch — every child sheet ends after lib_symbols /
# any placed (symbol ...) blocks. The fix under test must insert new symbol
# instances before the closing paren of (kicad_sch ...) when the marker is
# missing.
SUB_SHEET_NO_SHEET_INSTANCES = """(kicad_sch
\t(version 20260306)
\t(generator "eeschema")
\t(generator_version "10.0")
\t(uuid "bbbb2222-2222-2222-2222-bbbbbbbbbbbb")
\t(paper "A4")
\t(lib_symbols)
)
"""


@pytest.mark.unit
class TestCreateComponentInstanceSubSheet:
    """Hierarchical sub-sheets don't have (sheet_instances ...).

    Before the fix, create_component_instance raised
    'Could not find insertion point in schematic' on any sub-sheet, blocking
    every add_schematic_component call into a hierarchical design's child
    sheet.
    """

    def setup_method(self) -> None:
        from commands.dynamic_symbol_loader import DynamicSymbolLoader

        self.DynamicSymbolLoader = DynamicSymbolLoader

    def _loader(self) -> Any:
        return self.DynamicSymbolLoader()

    def test_sub_sheet_insertion_succeeds(self, tmp_path: Any) -> None:
        sch = tmp_path / "child.kicad_sch"
        sch.write_text(SUB_SHEET_NO_SHEET_INSTANCES, encoding="utf-8")

        ok = self._loader().create_component_instance(
            sch, "Device", "R", reference="R_TEST", value="100k", x=50, y=50
        )

        assert ok is True
        content = sch.read_text(encoding="utf-8")
        assert '"R_TEST"' in content
        assert "100k" in content

    def test_sub_sheet_keeps_outer_form_balanced(self, tmp_path: Any) -> None:
        """The new symbol must land inside (kicad_sch ...), with parens balanced."""
        sch = tmp_path / "child.kicad_sch"
        sch.write_text(SUB_SHEET_NO_SHEET_INSTANCES, encoding="utf-8")

        self._loader().create_component_instance(
            sch, "Device", "R", reference="R_TEST", value="1k", x=10, y=10
        )

        content = sch.read_text(encoding="utf-8")
        assert content.count("(") == content.count(
            ")"
        ), "Inserting into a sub-sheet must keep parens balanced"
        # The outer form must still parse via sexpdata.
        import sexpdata

        parsed = sexpdata.loads(content)
        assert isinstance(parsed, list)
        assert parsed[0] == sexpdata.Symbol("kicad_sch")

    def test_sub_sheet_round_trips_via_sexpdata(self, tmp_path: Any) -> None:
        """The injected symbol must survive a sexpdata load+dump round-trip."""
        import sexpdata

        sch = tmp_path / "child.kicad_sch"
        sch.write_text(SUB_SHEET_NO_SHEET_INSTANCES, encoding="utf-8")

        self._loader().create_component_instance(
            sch, "Device", "R", reference="R_TEST", value="1k", x=10, y=10
        )

        parsed = sexpdata.loads(sch.read_text(encoding="utf-8"))
        # The placed (symbol (lib_id ...) ...) block must be a top-level child of kicad_sch.
        symbol_items = [
            item
            for item in parsed[1:]
            if isinstance(item, list) and len(item) > 0 and item[0] == sexpdata.Symbol("symbol")
        ]
        # Confirm at least one of those carries our reference.
        assert any(
            sexpdata.dumps(s).find('"R_TEST"') >= 0 for s in symbol_items
        ), "Reference 'R_TEST' should appear in a top-level (symbol ...) child"


# ---------------------------------------------------------------------------
# S-expression string escaping — values with quotes / parens / backslashes
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestComponentValueEscaping:
    """A component value with an unescaped double-quote corrupted the whole
    .kicad_sch.

    Reproduces the field bug: placing a connector with value
    ``2.9" EPD FPC (24P)`` wrote a raw ``"`` into ``(property "Value" "...")``,
    which opened a second string and left the file with an odd number of
    quotes.  KiCad/kicad-cli then refused to load it ("failed to load
    schematic") and every sexpdata-based reader failed with
    ``'NoneType' object has no attribute 'start'``.  create_component_instance
    must escape every user-controlled value before interpolating it.
    """

    def setup_method(self) -> None:
        from commands.dynamic_symbol_loader import DynamicSymbolLoader

        self.DynamicSymbolLoader = DynamicSymbolLoader

    def _loader(self) -> Any:
        return self.DynamicSymbolLoader()

    def _place_and_load(self, sch: Path, **kwargs: Any) -> Any:
        """Place a component and return the sexpdata-parsed schematic."""
        import sexpdata

        self._loader().create_component_instance(sch, "Device", "R", **kwargs)
        return sexpdata.loads(sch.read_text(encoding="utf-8"))

    def _instance_props(self, parsed: Any) -> dict:
        """Collect {name: value} from the placed (symbol ...) instance's properties."""
        import sexpdata

        props: dict = {}
        for item in parsed[1:]:
            if not (isinstance(item, list) and item and item[0] == sexpdata.Symbol("symbol")):
                continue
            # Only the placed instance carries (lib_id ...); skip lib_symbols.
            if not any(
                isinstance(s, list) and s and s[0] == sexpdata.Symbol("lib_id") for s in item
            ):
                continue
            for sub in item:
                if (
                    isinstance(sub, list)
                    and len(sub) >= 3
                    and sub[0] == sexpdata.Symbol("property")
                ):
                    props[str(sub[1])] = str(sub[2])
        return props

    def test_value_with_unescaped_quote_keeps_file_loadable(self, tmp_path: Any) -> None:
        sch = tmp_path / "test.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        # The exact failing value from the field report.
        parsed = self._place_and_load(sch, reference="J1", value='2.9" EPD FPC (24P)', x=10, y=10)
        assert isinstance(parsed, list)
        # The value round-trips verbatim once unescaped by the parser.
        assert self._instance_props(parsed)["Value"] == '2.9" EPD FPC (24P)'

    def test_quotes_in_file_are_backslash_escaped(self, tmp_path: Any) -> None:
        import re

        sch = tmp_path / "test.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        self._loader().create_component_instance(
            sch, "Device", "R", reference="J1", value='2.9" EPD', x=10, y=10
        )
        text = sch.read_text(encoding="utf-8")
        # Every string is closed: an even number of *unescaped* quotes.  A
        # naive count would include the escaped quote and read odd.
        assert len(re.findall(r'(?<!\\)"', text)) % 2 == 0
        # The literal quote in the value is escaped, not raw.
        assert '2.9\\" EPD' in text

    def test_reference_and_footprint_quotes_escaped(self, tmp_path: Any) -> None:
        sch = tmp_path / "test.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        parsed = self._place_and_load(
            sch,
            reference='R"1',
            value="10k",
            footprint='Lib:Foo"Bar',
            x=10,
            y=10,
        )
        props = self._instance_props(parsed)
        assert props["Reference"] == 'R"1'
        assert props["Footprint"] == 'Lib:Foo"Bar'

    def test_backslash_in_value_is_escaped(self, tmp_path: Any) -> None:
        sch = tmp_path / "test.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        parsed = self._place_and_load(sch, reference="R1", value="path\\to\\thing", x=10, y=10)
        assert self._instance_props(parsed)["Value"] == "path\\to\\thing"

    def test_validation_guard_rejects_unparseable_content(self) -> None:
        """The defence-in-depth guard turns a malformed write into a clear
        error instead of committing an unloadable file."""
        from commands.dynamic_symbol_loader import DynamicSymbolLoader

        with pytest.raises(ValueError, match="unparseable"):
            DynamicSymbolLoader._validate_schematic_text('(kicad_sch (property "x" "y)', "R1")


# ---------------------------------------------------------------------------
# Mirror parameter — known gap
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAddComponentMirrorParam:
    """ComponentManager.add_component does NOT honor a 'mirror' kwarg today.

    The MCP add_schematic_component tool schema also doesn't expose mirror.
    A mirror is currently only applicable post-add via rotate_schematic_component.

    These tests pin down the silent-drop behavior so a fixture that passes
    'mirror': 'x' and then asserts something against the resulting schematic
    cannot accidentally pass for the wrong reason (the symbol ends up
    unmirrored). If/when add_component grows real mirror support, update both
    tests together — the second test then becomes the positive assertion."""

    def setup_method(self) -> None:
        from commands.component_schematic import ComponentManager
        from commands.schematic import SchematicManager

        self.ComponentManager = ComponentManager
        self.SchematicManager = SchematicManager

    def _add(self, sch_path: Path, mirror_value: Any) -> None:
        sch = self.SchematicManager.load_schematic(str(sch_path))
        params = {
            "type": "R",
            "reference": "R1",
            "value": "10k",
            "x": 100.0,
            "y": 100.0,
            "rotation": 0,
        }
        if mirror_value is not None:
            params["mirror"] = mirror_value
        self.ComponentManager.add_component(sch, params, sch_path)
        self.SchematicManager.save_schematic(sch, str(sch_path))

    def test_mirror_x_arg_is_silently_dropped(self, tmp_path: Any) -> None:
        sch = tmp_path / "mirror_x.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        self._add(sch, "x")
        text = sch.read_text()
        assert "(mirror x)" not in text, (
            "ComponentManager.add_component now appears to honor mirror='x'. "
            "Update _build_mirror_case in test_pin_world_xy_eeschema_truth.py "
            "to drop the post-add mirror application and remove this test."
        )

    def test_mirror_y_arg_is_silently_dropped(self, tmp_path: Any) -> None:
        sch = tmp_path / "mirror_y.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        self._add(sch, "y")
        text = sch.read_text()
        assert "(mirror y)" not in text, (
            "ComponentManager.add_component now appears to honor mirror='y'. "
            "See sibling test_mirror_x_arg_is_silently_dropped."
        )


# ---------------------------------------------------------------------------
# Grid snap (default-on; opt out via snapToGrid=false)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSchematicGridSnap:
    """KiCad's stock schematic grid is 1.27 mm.  Off-grid components
    produce 'pin/wire not aligned to grid' ERC warnings on every pin —
    a user reported 11 warnings from a single off-grid placement at
    round mm coords like (130, 80).  Snap is now **default-on**: pass
    ``snapToGrid: false`` only when sub-grid placement is intentional."""

    def test_snap_helper_rounds_to_nearest_grid_multiple(self) -> None:
        from handlers.schematic_component import _snap_to_schematic_grid

        # 150 mm / 1.27 = 118.11 → round to 118 → 149.86 mm
        assert _snap_to_schematic_grid(150.0) == pytest.approx(149.86, abs=1e-9)
        # 100 mm / 1.27 = 78.74 → round to 79 → 100.33 mm
        assert _snap_to_schematic_grid(100.0) == pytest.approx(100.33, abs=1e-9)
        # An on-grid value stays put.
        assert _snap_to_schematic_grid(1.27 * 50) == pytest.approx(1.27 * 50, abs=1e-9)
        # Zero stays zero.
        assert _snap_to_schematic_grid(0.0) == 0.0
        # Negative values snap correctly.
        assert _snap_to_schematic_grid(-1.27 * 3) == pytest.approx(-1.27 * 3, abs=1e-9)

    def test_snap_helper_custom_grid(self) -> None:
        from handlers.schematic_component import _snap_to_schematic_grid

        # 2.54 mm grid (100 mil) — 100 mm / 2.54 = 39.37 → round 39 → 99.06 mm
        assert _snap_to_schematic_grid(100.0, grid_mm=2.54) == pytest.approx(99.06, abs=1e-9)

    def test_snap_helper_no_op_on_zero_or_negative_grid(self) -> None:
        from handlers.schematic_component import _snap_to_schematic_grid

        # Defensive: a zero/negative grid would loop or divide-by-zero.
        # The helper returns the value untouched.
        assert _snap_to_schematic_grid(150.0, grid_mm=0) == 150.0
        assert _snap_to_schematic_grid(150.0, grid_mm=-1.27) == 150.0

    def test_apply_grid_snap_is_on_by_default(self) -> None:
        """The user-facing default: no flag passed → snap fires.  An
        agent that places at round mm like (150, 100) ends up on-grid
        without having to know about the 1.27 mm quirk."""
        from handlers.schematic_component import _apply_grid_snap

        x, y, snapped = _apply_grid_snap(150.0, 100.0, {})

        assert x == pytest.approx(149.86, abs=1e-9)
        assert y == pytest.approx(100.33, abs=1e-9)
        assert snapped is True

    def test_apply_grid_snap_explicit_true_still_snaps(self) -> None:
        from handlers.schematic_component import _apply_grid_snap

        x, y, snapped = _apply_grid_snap(150.0, 100.0, {"snapToGrid": True})

        assert x == pytest.approx(149.86, abs=1e-9)
        assert y == pytest.approx(100.33, abs=1e-9)
        assert snapped is True

    def test_apply_grid_snap_false_opts_out(self) -> None:
        """Explicit ``snapToGrid: false`` is the one way to keep the
        exact coordinates — for callers reproducing a pre-existing
        sub-grid placement."""
        from handlers.schematic_component import _apply_grid_snap

        x, y, snapped = _apply_grid_snap(150.0, 100.0, {"snapToGrid": False})

        assert (x, y) == (150.0, 100.0)
        assert snapped is False

    def test_apply_grid_snap_reports_no_movement_on_grid_input(self) -> None:
        from handlers.schematic_component import _apply_grid_snap

        on_grid_x = 1.27 * 50  # exactly on grid
        on_grid_y = 1.27 * 70

        x, y, snapped = _apply_grid_snap(on_grid_x, on_grid_y, {"snapToGrid": True})

        assert (x, y) == (on_grid_x, on_grid_y)
        # `snapped` reports whether coordinates moved, not whether snap
        # was requested — on-grid input must report False so the
        # response doesn't include a misleading `.snap` field.
        assert snapped is False

    def test_apply_grid_snap_custom_grid_mm_param(self) -> None:
        from handlers.schematic_component import _apply_grid_snap

        # 2.54 mm grid via param.
        x, y, snapped = _apply_grid_snap(10.0, 20.0, {"snapToGrid": True, "snapGridMm": 2.54})

        # 10 / 2.54 = 3.937 → 4 → 10.16 mm
        assert x == pytest.approx(10.16, abs=1e-9)
        # 20 / 2.54 = 7.874 → 8 → 20.32 mm
        assert y == pytest.approx(20.32, abs=1e-9)
        assert snapped is True

    def test_add_handler_default_snaps_user_reported_coords(self, monkeypatch) -> None:
        """User report: add_schematic_component(x=130, y=80) produced 11
        off-grid ERC warnings.  With default-on snap, the integer mm
        coords land on-grid and the response surfaces the actual
        position + the snap delta."""
        from handlers.schematic_component import handle_add_schematic_component

        # Capture what the symbol loader is asked to write — no need to
        # touch a real schematic file.
        captured: dict = {}

        class _FakeLoader:
            def __init__(self, project_path=None):
                pass

            def add_component(self, *args, **kwargs):
                captured["x"] = kwargs["x"]
                captured["y"] = kwargs["y"]

        monkeypatch.setattr("commands.dynamic_symbol_loader.DynamicSymbolLoader", _FakeLoader)

        # Need a writable path so the handler's parent-dir walk doesn't crash.
        sch = tmp_path = Path("/tmp/__snap_default_probe.kicad_sch")
        sch.write_text("(kicad_sch)\n", encoding="utf-8")
        try:
            out = handle_add_schematic_component(
                iface=None,  # not used in this code path
                params={
                    "schematicPath": str(sch),
                    "component": {
                        "type": "R",
                        "library": "Device",
                        "reference": "R1",
                        "value": "10k",
                        # User's exact reproduction — round mm, no snap flag.
                        "x": 130,
                        "y": 80,
                    },
                },
            )
        finally:
            sch.unlink(missing_ok=True)

        assert out["success"] is True
        # 130 / 1.27 = 102.36 → 102 → 129.54 mm
        # 80 / 1.27 = 62.99 → 63 → 80.01 mm
        assert captured["x"] == pytest.approx(129.54, abs=1e-2)
        assert captured["y"] == pytest.approx(80.01, abs=1e-2)
        # Response surfaces the snap delta so the agent isn't surprised.
        assert out["snap"]["applied"] is True
        assert out["snap"]["requested"] == {"x": 130, "y": 80}
        assert out["position"]["x"] == pytest.approx(129.54, abs=1e-2)
        assert out["position"]["y"] == pytest.approx(80.01, abs=1e-2)

    def test_add_handler_snap_false_preserves_exact_coords(self, monkeypatch) -> None:
        """``snapToGrid: false`` opts back into exact placement."""
        from handlers.schematic_component import handle_add_schematic_component

        captured: dict = {}

        class _FakeLoader:
            def __init__(self, project_path=None):
                pass

            def add_component(self, *args, **kwargs):
                captured["x"] = kwargs["x"]
                captured["y"] = kwargs["y"]

        monkeypatch.setattr("commands.dynamic_symbol_loader.DynamicSymbolLoader", _FakeLoader)

        sch = Path("/tmp/__snap_optout_probe.kicad_sch")
        sch.write_text("(kicad_sch)\n", encoding="utf-8")
        try:
            out = handle_add_schematic_component(
                iface=None,
                params={
                    "schematicPath": str(sch),
                    "snapToGrid": False,
                    "component": {
                        "type": "R",
                        "library": "Device",
                        "reference": "R1",
                        "x": 130,
                        "y": 80,
                    },
                },
            )
        finally:
            sch.unlink(missing_ok=True)

        assert out["success"] is True
        assert captured["x"] == 130
        assert captured["y"] == 80
        # No snap delta in the response when opt-out is honoured.
        assert "snap" not in out


# ---------------------------------------------------------------------------
# Reference validation (A6/A11): refuse empty / duplicate refdes by default;
# opt-in autoAssign numbers the next free reference of the same prefix.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReferenceValidation:
    """add_schematic_component previously accepted an empty reference (A11) and
    a DUPLICATE reference (A6), producing an invalid schematic (KiCad ERC flags
    'duplicate reference').  It must now refuse both by default with a
    structured errorCode, and honor an opt-in autoAssign that numbers the next
    free reference of the same prefix instead."""

    def _call(
        self,
        sch: Path,
        reference: str,
        *,
        auto_assign: bool = False,
        unit: int = 1,
        x: float = 60,
        y: float = 60,
        value: str = "x",
    ) -> dict:
        from handlers.schematic_component import handle_add_schematic_component

        params: dict = {
            "schematicPath": str(sch),
            "component": {
                "library": "Device",
                "type": "R",
                "reference": reference,
                "value": value,
                "x": x,
                "y": y,
                "unit": unit,
            },
        }
        if auto_assign:
            params["autoAssign"] = True
        return handle_add_schematic_component(iface=None, params=params)

    def test_empty_reference_rejected(self, tmp_path: Any) -> None:
        sch = tmp_path / "t.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        res = self._call(sch, "")
        assert res["success"] is False
        assert res["errorCode"] == "INVALID_REFERENCE"

    def test_whitespace_reference_rejected(self, tmp_path: Any) -> None:
        sch = tmp_path / "t.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        res = self._call(sch, "   ")
        assert res["success"] is False
        assert res["errorCode"] == "INVALID_REFERENCE"

    def test_empty_reference_autoassigned(self, tmp_path: Any) -> None:
        sch = tmp_path / "t.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        res = self._call(sch, "", auto_assign=True)
        assert res["success"] is True
        # Prefix derived from the component type (Device:R → "R").
        assert res["component_reference"] == "R1"
        assert res["autoAssignedReference"] is True

    def test_duplicate_reference_rejected(self, tmp_path: Any) -> None:
        sch = tmp_path / "t.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        assert self._call(sch, "R1", x=50, y=50)["success"] is True
        res = self._call(sch, "R1", x=60, y=60, value="DUP")
        assert res["success"] is False
        assert res["errorCode"] == "REFERENCE_EXISTS"

    def test_duplicate_reference_autoassigned(self, tmp_path: Any) -> None:
        sch = tmp_path / "t.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        assert self._call(sch, "R1", x=50, y=50)["success"] is True
        res = self._call(sch, "R1", x=60, y=60, auto_assign=True)
        assert res["success"] is True
        assert res["component_reference"] == "R2"
        assert res["requestedReference"] == "R1"

    def test_fresh_reference_still_succeeds(self, tmp_path: Any) -> None:
        """A unique reference is unaffected by the new guard."""
        sch = tmp_path / "t.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        res = self._call(sch, "R7", x=50, y=50)
        assert res["success"] is True
        assert res["component_reference"] == "R7"

    def test_new_unit_of_existing_multiunit_reference_allowed(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """Placing a further UNIT of a multi-unit part already on the sheet
        legitimately reuses its reference and must NOT be rejected as a
        duplicate (the F1 add-remaining-units workflow)."""
        sch = tmp_path / "t.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        assert self._call(sch, "U1", x=50, y=50, unit=1)["success"] is True

        from commands.pin_locator import PinLocator

        # Simulate a genuine 2-unit part with unit 1 placed, unit 2 free.
        monkeypatch.setattr(
            PinLocator,
            "get_unit_placement",
            lambda self, p, r: {
                "lib_id": "Device:R",
                "defined_units": [1, 2],
                "total_units": 2,
                "placed_units": [1],
                "unplaced_units": [2],
                "is_multi_unit": True,
            },
        )
        res = self._call(sch, "U1", x=60, y=60, unit=2)
        assert res["success"] is True
        # Reference kept — it's a legit second unit, not an auto-reassignment.
        assert res["component_reference"] == "U1"
        assert "autoAssignedReference" not in res


# ---------------------------------------------------------------------------
# Power-symbol autoAssign: the '#' prefix must survive auto-numbering
# (#FLG? → #FLG?01, like the KiCad GUI).  A bare "PWR_FLAG1" would turn the
# instance into a regular (non-power) symbol that ERC/netlist treats as a
# real component and that sync_schematic_to_board pushes to the PCB.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPowerSymbolAutoAssign:
    def _call(
        self,
        sch: Path,
        reference: str,
        *,
        comp_type: str = "PWR_FLAG",
        auto_assign: bool = False,
        x: float = 60,
        y: float = 60,
    ) -> dict:
        from handlers.schematic_component import handle_add_schematic_component

        params: dict = {
            "schematicPath": str(sch),
            "component": {
                "library": "power",
                "type": comp_type,
                "reference": reference,
                "value": comp_type,
                "x": x,
                "y": y,
                "unit": 1,
            },
        }
        if auto_assign:
            params["autoAssign"] = True
        return handle_add_schematic_component(iface=None, params=params)

    def test_duplicate_power_ref_keeps_hash_prefix(self, tmp_path: Any) -> None:
        sch = tmp_path / "t.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        assert self._call(sch, "#FLG?", x=50, y=50)["success"] is True
        res = self._call(sch, "#FLG?", x=60, y=60, auto_assign=True)
        assert res["success"] is True
        assert res["component_reference"] == "#FLG?01"
        assert res["component_reference"].startswith("#")

    def test_second_duplicate_power_ref_numbers_up(self, tmp_path: Any) -> None:
        sch = tmp_path / "t.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        assert self._call(sch, "#FLG?", x=50, y=50)["success"] is True
        assert self._call(sch, "#FLG?", x=60, y=60, auto_assign=True)["success"] is True
        res = self._call(sch, "#FLG?", x=70, y=70, auto_assign=True)
        assert res["success"] is True
        assert res["component_reference"] == "#FLG?02"

    def test_empty_power_ref_autoassigned_with_hash(self, tmp_path: Any) -> None:
        sch = tmp_path / "t.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        res = self._call(sch, "", auto_assign=True)
        assert res["success"] is True
        # Always numbered: a bare "#FLG?" ends in '?' = unannotated to KiCad.
        assert res["component_reference"] == "#FLG?01"

    def test_empty_power_ref_non_flag_symbol(self, tmp_path: Any) -> None:
        sch = tmp_path / "t.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        res = self._call(sch, "", comp_type="GND", auto_assign=True)
        assert res["success"] is True
        assert res["component_reference"] == "#PWR?01"

    def test_duplicate_power_ref_rejection_hint_mentions_hash(self, tmp_path: Any) -> None:
        sch = tmp_path / "t.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        assert self._call(sch, "#FLG?", x=50, y=50)["success"] is True
        res = self._call(sch, "#FLG?", x=60, y=60)
        assert res["success"] is False
        assert res["errorCode"] == "REFERENCE_EXISTS"
        assert "#FLG?" in res["message"]
