"""Unit tests for the pure / geometry helpers in commands.component.

Safety net ahead of an internal refactor of the 1600-line component.py.
Covers the deterministic logic a split could silently change: the nm→mm
scale, AABB rotation, and the override branch of the board-outline resolver,
plus an API-surface guard over the public command methods.

pcbnew is stubbed globally by tests/conftest.py.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.component import ComponentCommands  # noqa: E402


def _cmds() -> ComponentCommands:
    """ComponentCommands with a stubbed library manager (truthy → no real load)."""
    return ComponentCommands(board=None, library_manager=MagicMock())


# ---------------------------------------------------------------------------
# _nm_to_mm  (staticmethod, pure)
# ---------------------------------------------------------------------------


class TestNmToMm:
    def test_one_million_nm_is_one_mm(self):
        assert ComponentCommands._nm_to_mm(1_000_000) == pytest.approx(1.0)

    def test_fractional(self):
        assert ComponentCommands._nm_to_mm(2_500_000) == pytest.approx(2.5)

    def test_zero(self):
        assert ComponentCommands._nm_to_mm(0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _rotate_aabb  (staticmethod, pure)
# ---------------------------------------------------------------------------


class TestRotateAabb:
    def test_zero_degrees_is_identity(self):
        assert ComponentCommands._rotate_aabb(0, 0, 10, 4, 0) == pytest.approx((0, 0, 10, 4))

    def test_180_degrees_flips_extents(self):
        x1, y1, x2, y2 = ComponentCommands._rotate_aabb(0, 0, 10, 4, 180)
        assert (x1, y1, x2, y2) == pytest.approx((-10, -4, 0, 0))

    def test_45_degrees_expands_diagonally(self):
        # Unit square centred on origin → corners reach ±sqrt(2) on each axis.
        x1, y1, x2, y2 = ComponentCommands._rotate_aabb(-1, -1, 1, 1, 45)
        r = math.sqrt(2)
        assert (x1, y1, x2, y2) == pytest.approx((-r, -r, r, r))


# ---------------------------------------------------------------------------
# _resolve_outline_bbox  (override branch is board-independent)
# ---------------------------------------------------------------------------


class TestResolveOutlineBbox:
    def test_override_mm_passthrough(self):
        bbox = _cmds()._resolve_outline_bbox({"x1": 1, "y1": 2, "x2": 3, "y2": 4})
        assert bbox == pytest.approx((1, 2, 3, 4))

    def test_override_inch_scaled(self):
        bbox = _cmds()._resolve_outline_bbox({"x1": 1, "y1": 2, "x2": 3, "y2": 4, "unit": "in"})
        assert bbox == pytest.approx((25.4, 50.8, 76.2, 101.6))

    def test_falls_back_to_board_bbox(self):
        # No override → reads board.GetBoardEdgesBoundingBox() (in nm → mm).
        rc = _cmds()
        bb = SimpleNamespace(
            GetLeft=lambda: 1_000_000,
            GetTop=lambda: 2_000_000,
            GetRight=lambda: 3_000_000,
            GetBottom=lambda: 4_000_000,
        )
        rc.board = SimpleNamespace(GetBoardEdgesBoundingBox=lambda: bb)
        assert rc._resolve_outline_bbox(None) == pytest.approx((1, 2, 3, 4))


# ---------------------------------------------------------------------------
# Public API surface — guard for the upcoming internal refactor.
# Update deliberately when adding/removing a command.
# ---------------------------------------------------------------------------

EXPECTED_COMPONENT_COMMANDS = {
    "align_components",
    "check_courtyard_overlaps",
    "delete_component",
    "duplicate_component",
    "edit_component",
    "find_component",
    "get_component_list",
    "get_component_pads",
    "get_component_properties",
    "get_pad_position",
    "move_component",
    "place_component",
    "place_component_array",
    "rotate_component",
}


class TestPublicApiSurface:
    def test_public_command_methods_unchanged(self):
        actual = {
            name
            for name in dir(ComponentCommands)
            if not name.startswith("_") and callable(getattr(ComponentCommands, name))
        }
        assert actual == EXPECTED_COMPONENT_COMMANDS
