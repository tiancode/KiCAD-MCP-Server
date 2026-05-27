"""Regression tests for route_pad_to_pad's obstacle reporting.

The original implementation excluded the trace's own start/end pads via
``id(pad)``.  That doesn't work for SWIG-backed pcbnew bindings: every
``fp.Pads()`` iteration creates fresh Python proxy objects for the same
underlying C++ pad, so the IDs collected at ``find_pad`` time never
matched the IDs the obstacle scanner saw later.  Result: every trace
reported its OWN endpoints in the warning list, drowning out the real
"trace crosses a third pad" signal the warning was designed to surface.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_pad(ref: str, num: str, *, bbox: tuple[float, float, float, float]) -> MagicMock:
    """A fresh MagicMock pad with a fixed bounding box (left, top, right, bottom).

    SWIG normally re-issues fresh Python proxies per iteration, so we mimic
    that by NOT sharing a single mock across "iterations" — see
    ``_stub_footprint`` for how that's enforced.
    """
    pad = MagicMock(name=f"{ref}.{num}")
    pad.GetNumber.return_value = num
    bb = MagicMock(name=f"{ref}.{num}.bbox")
    bb.GetLeft.return_value = bbox[0]
    bb.GetTop.return_value = bbox[1]
    bb.GetRight.return_value = bbox[2]
    bb.GetBottom.return_value = bbox[3]
    pad.GetBoundingBox.return_value = bb
    return pad


def _stub_footprint(
    ref: str, pads_spec: List[tuple[str, tuple[float, float, float, float]]]
) -> MagicMock:
    """Build a footprint whose ``Pads()`` returns a *new* set of pad mocks
    each call — that's what real SWIG bindings do, and it's exactly what
    breaks ``id(pad)``-based exclusion.  Without this behaviour the test
    would silently pass even with the original buggy code."""
    fp = MagicMock(name=f"fp_{ref}")
    fp.GetReference.return_value = ref

    def fresh_pads() -> List[MagicMock]:
        return [_stub_pad(ref, num, bbox=bbox) for num, bbox in pads_spec]

    fp.Pads.side_effect = fresh_pads
    return fp


def _stub_board(footprints: List[MagicMock]) -> MagicMock:
    board = MagicMock(name="board")
    board.GetFootprints.return_value = footprints
    return board


def _make_routing_commands(board: Any) -> Any:
    from commands.routing import RoutingCommands

    cmds = RoutingCommands.__new__(RoutingCommands)
    cmds.board = board
    return cmds


# ---------------------------------------------------------------------------
# _pads_intersecting_segment
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPadsIntersectingSegment:
    """Direct tests of the obstacle scanner with fresh-per-call pad
    proxies — proves the (ref, num) exclusion is robust against SWIG's
    proxy-churn, which is what the original ``id()`` approach missed."""

    def test_excludes_endpoint_pads_by_ref_and_number(self):
        """Start and end pads must NEVER appear in the warnings list,
        even when fp.Pads() returns fresh proxy objects each call."""
        # R1.1 at (0, 0)..(2, 2); R1.2 at (10, 0)..(12, 2)
        r1 = _stub_footprint(
            "R1",
            [
                ("1", (0.0, 0.0, 2.0, 2.0)),
                ("2", (10.0, 0.0, 12.0, 2.0)),
            ],
        )
        board = _stub_board([r1])
        cmds = _make_routing_commands(board)

        # Segment from (1, 1) → (11, 1) — passes through both R1.1 and R1.2.
        start = MagicMock(x=1.0, y=1.0)
        end = MagicMock(x=11.0, y=1.0)

        warnings = cmds._pads_intersecting_segment(
            start, end, exclude_pad_keys={("R1", "1"), ("R1", "2")}
        )

        assert warnings == [], (
            "endpoint pads must be excluded by (ref, num) — old id()-based "
            f"exclusion failed because SWIG re-proxies the same C++ pad on "
            f"every Pads() call.  Got: {warnings}"
        )

    def test_reports_genuinely_crossed_third_party_pad(self):
        """A REAL obstacle (a pad on a different footprint that the
        segment crosses) must still appear in the warnings."""
        # R1.1 and R2.1 are the endpoints.  D1.1 sits in the middle.
        r1 = _stub_footprint("R1", [("1", (0.0, 0.0, 2.0, 2.0))])
        r2 = _stub_footprint("R2", [("1", (20.0, 0.0, 22.0, 2.0))])
        d1 = _stub_footprint("D1", [("1", (9.0, 0.0, 11.0, 2.0))])  # in path
        board = _stub_board([r1, r2, d1])
        cmds = _make_routing_commands(board)

        start = MagicMock(x=1.0, y=1.0)
        end = MagicMock(x=21.0, y=1.0)

        warnings = cmds._pads_intersecting_segment(
            start, end, exclude_pad_keys={("R1", "1"), ("R2", "1")}
        )

        assert len(warnings) == 1
        assert "D1.1" in warnings[0]

    def test_id_based_exclusion_would_have_failed_here(self):
        """Demonstrates the failure mode of the old code by NOT
        excluding anything — should report the start pad too.  This
        proves the test setup actually exercises the bug scenario."""
        r1 = _stub_footprint("R1", [("1", (0.0, 0.0, 2.0, 2.0))])
        r2 = _stub_footprint("R2", [("1", (20.0, 0.0, 22.0, 2.0))])
        board = _stub_board([r1, r2])
        cmds = _make_routing_commands(board)

        start = MagicMock(x=1.0, y=1.0)
        end = MagicMock(x=21.0, y=1.0)

        # No exclude → both endpoint pads should show up.
        warnings = cmds._pads_intersecting_segment(start, end)
        joined = " ".join(warnings)
        assert "R1.1" in joined
        assert "R2.1" in joined

    def test_empty_exclude_set_does_not_crash(self):
        r1 = _stub_footprint("R1", [("1", (0.0, 0.0, 2.0, 2.0))])
        board = _stub_board([r1])
        cmds = _make_routing_commands(board)

        warnings = cmds._pads_intersecting_segment(
            MagicMock(x=10.0, y=10.0),
            MagicMock(x=20.0, y=20.0),
            exclude_pad_keys=set(),
        )

        assert warnings == []  # segment doesn't intersect R1.1
