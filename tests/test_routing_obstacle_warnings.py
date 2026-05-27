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

    def test_endpoint_pads_appear_when_no_exclude_given(self):
        """Sanity-check that the fixture actually exercises the bug
        scenario: without exclusion, both endpoint pads ARE reported.
        If this test ever started returning [] the SWIG-churn fixture
        in _stub_footprint would be silently broken and the
        excludes-endpoints test above would pass for the wrong reason."""
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

    def test_unnumbered_pads_are_skipped(self):
        """Pads with empty GetNumber() (mounting holes, fiducials, NPTH)
        have no electrical role — they must NOT appear in the warning
        list even when geometrically in the segment's path.  Previously
        the warning text rendered as `"MH1."` (trailing dot, empty
        number) which agents wouldn't recognise as a pad."""
        r1 = _stub_footprint("R1", [("1", (0.0, 0.0, 2.0, 2.0))])
        # MH1 has an unnumbered mounting pad geometrically in the path.
        mh1 = _stub_footprint("MH1", [("", (9.0, 0.0, 11.0, 2.0))])
        board = _stub_board([r1, mh1])
        cmds = _make_routing_commands(board)

        warnings = cmds._pads_intersecting_segment(
            MagicMock(x=1.0, y=1.0),
            MagicMock(x=12.0, y=1.0),
            exclude_pad_keys={("R1", "1")},
        )

        assert warnings == [], (
            f"unnumbered (mechanical) pads must be filtered from "
            f"obstacle warnings; got {warnings}"
        )


# ---------------------------------------------------------------------------
# route_pad_to_pad integration — verifies the new per-leg obstacle check
# and the obstacleCount field on the response.
# ---------------------------------------------------------------------------


def _stub_pad_for_route(
    ref: str,
    num: str,
    *,
    pos: tuple[float, float],
    netname: str = "N$1",
) -> MagicMock:
    """A pad with the methods route_pad_to_pad reads at the
    coarse-grained level (position, netname, number).  Returned from a
    fresh-each-call Pads() iterator to match SWIG behaviour."""
    pad = MagicMock(name=f"{ref}.{num}")
    pad.GetNumber.return_value = num
    pad.GetNetname.return_value = netname
    pad.GetPosition.return_value = MagicMock(x=pos[0], y=pos[1])
    bb = MagicMock()
    bb.GetLeft.return_value = pos[0] - 1
    bb.GetTop.return_value = pos[1] - 1
    bb.GetRight.return_value = pos[0] + 1
    bb.GetBottom.return_value = pos[1] + 1
    pad.GetBoundingBox.return_value = bb
    return pad


def _stub_footprint_for_route(
    ref: str,
    layer_id: int,
    pads_spec: list[tuple[str, tuple[float, float]]],
) -> MagicMock:
    fp = MagicMock(name=f"fp_{ref}")
    fp.GetReference.return_value = ref
    fp.GetLayer.return_value = layer_id

    def fresh_pads():
        return [_stub_pad_for_route(ref, num, pos=pos) for num, pos in pads_spec]

    fp.Pads.side_effect = fresh_pads
    return fp


@pytest.mark.unit
class TestRoutePadToPadObstacleResponse:
    def _make_cmds(self, footprints: list[MagicMock]) -> Any:
        from commands.routing import RoutingCommands

        board = MagicMock(name="board")
        board.GetFootprints.return_value = footprints

        # Layer id → name lookup (0=F.Cu, 31=B.Cu in real pcbnew, but
        # any consistent mapping works for the stub).
        def layer_name(layer_id):
            return {0: "F.Cu", 31: "B.Cu"}.get(layer_id, "F.Cu")

        board.GetLayerName.side_effect = layer_name

        cmds = RoutingCommands.__new__(RoutingCommands)
        cmds.board = board
        # Bypass netclass lookup so we don't have to mock GetDesignSettings.
        cmds._netclass_track_width_mm = lambda pad: 0.25  # type: ignore[method-assign]
        # Stub the trace/via writers — we only care about the obstacle
        # bookkeeping, not the actual board mutations.
        cmds.route_trace = lambda params: {"success": True}  # type: ignore[method-assign]
        cmds.add_via = lambda params: {"success": True}  # type: ignore[method-assign]
        return cmds

    def test_obstacle_path_refuses_by_default(self):
        """The straight segment crosses D1 — default behaviour is now to
        refuse before mutating the board.  obstaclesCrossed must list
        the offending pad with its full reference; obstacleCount must
        match the list length so the agent can branch on it without
        scanning the array."""
        # R1.1 → R2.1 on same layer, with D1.1 in the path.
        r1 = _stub_footprint_for_route("R1", 0, [("1", (0.0, 0.0))])
        r2 = _stub_footprint_for_route("R2", 0, [("1", (20.0, 0.0))])
        d1 = _stub_footprint_for_route("D1", 0, [("1", (10.0, 0.0))])
        cmds = self._make_cmds([r1, r2, d1])

        result = cmds.route_pad_to_pad(
            {"fromRef": "R1", "fromPad": "1", "toRef": "R2", "toPad": "1"}
        )

        assert result["success"] is False
        assert result["hasObstacles"] is True
        assert result["obstacleCount"] == len(result["obstaclesCrossed"])
        assert result["obstacleCount"] >= 1
        # D1.1 must show up; neither R1.1 nor R2.1 (the endpoints) can.
        joined = " ".join(result["obstaclesCrossed"])
        assert "D1.1" in joined
        assert "R1.1" not in joined
        assert "R2.1" not in joined
        # The agent reads `hint` to know about `force` — make sure it's there.
        assert "force" in result["hint"]

    def test_obstacle_path_inserts_when_force_true(self):
        """``force=True`` opts back into the legacy behaviour: insert the
        trace, return warnings alongside ``success: True`` so the agent
        knows to run DRC."""
        r1 = _stub_footprint_for_route("R1", 0, [("1", (0.0, 0.0))])
        r2 = _stub_footprint_for_route("R2", 0, [("1", (20.0, 0.0))])
        d1 = _stub_footprint_for_route("D1", 0, [("1", (10.0, 0.0))])
        cmds = self._make_cmds([r1, r2, d1])

        result = cmds.route_pad_to_pad(
            {
                "fromRef": "R1",
                "fromPad": "1",
                "toRef": "R2",
                "toPad": "1",
                "force": True,
            }
        )

        assert result["success"] is True
        assert "obstacleCount" in result
        assert result["obstacleCount"] >= 1
        # D1.1 still surfaced so the caller can see what they accepted.
        joined = " ".join(result["obstaclesCrossed"])
        assert "D1.1" in joined

    def test_via_case_runs_obstacle_scanner_per_leg(self, monkeypatch):
        """Cross-layer route → the scanner must be called TWICE (once
        per actual leg), each time excluding ONLY the endpoint pad on
        that leg.  This contract is what makes the per-leg check
        correct: leg 1's endpoint is start_pad (on start_layer), leg
        2's is end_pad (on end_layer); excluding both from both legs
        would silently hide a real "trace passes through start pad on
        the OTHER leg" obstacle."""
        # U1 on F.Cu, U2 on B.Cu → cross-layer → via inserted.
        u1 = _stub_footprint_for_route("U1", 0, [("1", (0.0, 0.0))])
        u2 = _stub_footprint_for_route("U2", 31, [("1", (0.0, 20.0))])
        cmds = self._make_cmds([u1, u2])

        calls: list[tuple[Any, Any, set]] = []

        def spy(start, end, *, exclude_pad_keys=None):
            calls.append((start, end, exclude_pad_keys or set()))
            return []

        cmds._pads_intersecting_segment = spy  # type: ignore[method-assign]

        result = cmds.route_pad_to_pad(
            {"fromRef": "U1", "fromPad": "1", "toRef": "U2", "toPad": "1"}
        )

        assert result["success"] is True
        assert result.get("via_added") is True
        assert (
            len(calls) == 2
        ), f"via case must invoke the obstacle scanner once per leg; got {len(calls)}"
        # Leg 1: start_pad → via.  Excludes ONLY (U1, 1).
        assert calls[0][2] == {
            ("U1", "1")
        }, f"leg 1 must exclude only the start pad; got {calls[0][2]}"
        # Leg 2: via → end_pad.  Excludes ONLY (U2, 1).
        assert calls[1][2] == {
            ("U2", "1")
        }, f"leg 2 must exclude only the end pad; got {calls[1][2]}"

    def test_via_case_refuses_when_either_leg_crosses_a_pad(self):
        """If leg 1 or leg 2 of a via path crosses a third pad, the call
        must refuse before any of route_trace / add_via runs — a partial
        insert (leg 1 OK, leg 2 blocked) would leave the board with an
        orphan stub.  Refusal happens before any mutation."""
        u1 = _stub_footprint_for_route("U1", 0, [("1", (0.0, 0.0))])
        u2 = _stub_footprint_for_route("U2", 31, [("1", (0.0, 20.0))])
        cmds = self._make_cmds([u1, u2])

        # Stub scanner: leg 2 (via → end_pad) reports a crossing.
        leg_count = {"n": 0}

        def fake_scanner(start, end, *, exclude_pad_keys=None):
            leg_count["n"] += 1
            return ["Trace segment passes through D1.1 — consider routing around"] if leg_count["n"] == 2 else []

        cmds._pads_intersecting_segment = fake_scanner  # type: ignore[method-assign]

        # Sentinels so the test fails loudly if the partial insert sneaks through.
        cmds.route_trace = lambda params: pytest.fail(  # type: ignore[method-assign]
            "route_trace must not be called when an obstacle is detected"
        )
        cmds.add_via = lambda params: pytest.fail(  # type: ignore[method-assign]
            "add_via must not be called when an obstacle is detected"
        )

        result = cmds.route_pad_to_pad(
            {"fromRef": "U1", "fromPad": "1", "toRef": "U2", "toPad": "1"}
        )

        assert result["success"] is False
        assert result["hasObstacles"] is True
        assert result["obstacleCount"] == 1
        assert "D1.1" in " ".join(result["obstaclesCrossed"])
