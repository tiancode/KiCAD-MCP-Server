"""Unit tests for the pure helpers in commands.wire_manager.

Safety net ahead of an internal refactor of the 1775-line wire_manager.py.
Covers the deterministic, file-independent logic a split could silently
change: label-type normalisation, strict point-on-wire geometry, junction
s-expression construction, orthogonal path routing, and root-uuid parsing —
plus an API-surface guard over the public methods.

pcbnew is stubbed globally by tests/conftest.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.wire_manager import WireManager, _normalize_label_type  # noqa: E402

# ---------------------------------------------------------------------------
# _normalize_label_type
# ---------------------------------------------------------------------------


class TestNormalizeLabelType:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("label", "label"),
            ("local", "label"),
            ("local_label", "label"),
            ("net", "label"),
            ("net_label", "label"),
            ("global", "global_label"),
            ("global_label", "global_label"),
            ("hier", "hierarchical_label"),
            ("hierarchical", "hierarchical_label"),
            ("hierarchical_label", "hierarchical_label"),
            ("sheet", "hierarchical_label"),
        ],
    )
    def test_known_aliases(self, raw, expected):
        assert _normalize_label_type(raw) == expected

    def test_case_insensitive_and_stripped(self):
        assert _normalize_label_type("  GLOBAL  ") == "global_label"

    def test_unknown_raises_value_error(self):
        with pytest.raises(ValueError):
            _normalize_label_type("bogus")


# ---------------------------------------------------------------------------
# _point_strictly_on_wire
# ---------------------------------------------------------------------------


class TestPointStrictlyOnWire:
    def test_midpoint_of_horizontal_wire(self):
        assert WireManager._point_strictly_on_wire(5, 0, 0, 0, 10, 0) is True

    def test_midpoint_of_vertical_wire(self):
        assert WireManager._point_strictly_on_wire(0, 5, 0, 0, 0, 10) is True

    def test_endpoint_is_not_strictly_on_wire(self):
        assert WireManager._point_strictly_on_wire(0, 0, 0, 0, 10, 0) is False
        assert WireManager._point_strictly_on_wire(10, 0, 0, 0, 10, 0) is False

    def test_point_off_the_line(self):
        assert WireManager._point_strictly_on_wire(5, 1, 0, 0, 10, 0) is False

    def test_diagonal_wire_never_matches(self):
        assert WireManager._point_strictly_on_wire(5, 5, 0, 0, 10, 10) is False


# ---------------------------------------------------------------------------
# _make_junction_sexp
# ---------------------------------------------------------------------------


class TestMakeJunctionSexp:
    def test_structure_and_position(self):
        sexp = WireManager._make_junction_sexp(1.5, 2.5, diameter=0.5)
        assert str(sexp[0]) == "junction"
        assert str(sexp[1][0]) == "at"
        assert sexp[1][1] == 1.5
        assert sexp[1][2] == 2.5
        assert str(sexp[2][0]) == "diameter"
        assert sexp[2][1] == 0.5

    def test_each_call_gets_a_fresh_uuid(self):
        a = WireManager._make_junction_sexp(0, 0)
        b = WireManager._make_junction_sexp(0, 0)
        # last element is [uuid, "<value>"]
        assert str(a[-1][0]) == "uuid"
        assert a[-1][1] != b[-1][1]


# ---------------------------------------------------------------------------
# create_orthogonal_path  (public, pure)
# ---------------------------------------------------------------------------


class TestCreateOrthogonalPath:
    def test_horizontal_first(self):
        path = WireManager.create_orthogonal_path([0, 0], [10, 5], prefer_horizontal_first=True)
        assert path == [[0, 0], [10, 0], [10, 5]]

    def test_vertical_first(self):
        path = WireManager.create_orthogonal_path([0, 0], [10, 5], prefer_horizontal_first=False)
        assert path == [[0, 0], [0, 5], [10, 5]]

    def test_already_aligned_horizontally_is_direct(self):
        assert WireManager.create_orthogonal_path([0, 0], [10, 0]) == [[0, 0], [10, 0]]

    def test_already_aligned_vertically_is_direct(self):
        assert WireManager.create_orthogonal_path([0, 0], [0, 10]) == [[0, 0], [0, 10]]


# ---------------------------------------------------------------------------
# _root_schematic_uuid
# ---------------------------------------------------------------------------


class TestRootSchematicUuid:
    def test_returns_first_root_uuid(self):
        content = '(kicad_sch (version 20230121) (uuid "abc-123") (paper "A4"))'
        assert WireManager._root_schematic_uuid(content) == "abc-123"

    def test_non_kicad_sch_root_returns_none(self):
        assert WireManager._root_schematic_uuid('(foo (uuid "x"))') is None

    def test_missing_uuid_returns_none(self):
        assert WireManager._root_schematic_uuid("(kicad_sch (version 20230121))") is None

    def test_malformed_content_returns_none(self):
        assert WireManager._root_schematic_uuid("not a sexpr (((") is None


# ---------------------------------------------------------------------------
# Public API surface — guard for the upcoming internal refactor.
# Update deliberately when adding/removing a command.
# ---------------------------------------------------------------------------

EXPECTED_WIRE_MANAGER_METHODS = {
    "add_hierarchical_label",
    "add_label",
    "add_no_connect",
    "add_polyline_wire",
    "add_sheet",
    "add_sheet_pin",
    "add_text",
    "add_wire",
    "create_orthogonal_path",
    "delete_label",
    "delete_no_connect",
    "delete_wire",
    "delete_wires",
    "edit_label",
    "list_texts",
    "sync_junctions",
}


class TestPublicApiSurface:
    def test_public_methods_unchanged(self):
        actual = {
            name
            for name in dir(WireManager)
            if not name.startswith("_") and callable(getattr(WireManager, name))
        }
        assert actual == EXPECTED_WIRE_MANAGER_METHODS
