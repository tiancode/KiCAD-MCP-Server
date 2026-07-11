"""Tests for report_net_lengths: pure length aggregation + skew report."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.routing._lengths import build_length_report, compute_net_lengths


def _track(net, sx, sy, ex, ey, layer="F.Cu", length=None):
    t = {"net": net, "startX": sx, "startY": sy, "endX": ex, "endY": ey, "layer": layer}
    if length is not None:
        t["length"] = length
    return t


@pytest.mark.unit
class TestComputeNetLengths:
    def test_straight_segments_sum_per_net(self):
        tracks = [
            _track("SIG_A", 0, 0, 3, 4),  # 5 mm
            _track("SIG_A", 3, 4, 3, 14),  # 10 mm
            _track("SIG_B", 0, 0, 1, 0),  # 1 mm
        ]
        report = compute_net_lengths(tracks, [])
        assert report["SIG_A"]["lengthMm"] == pytest.approx(15.0)
        assert report["SIG_A"]["segmentCount"] == 2
        assert report["SIG_B"]["lengthMm"] == pytest.approx(1.0)

    def test_arc_precomputed_length_overrides_chord(self):
        # Arc chord is 2 mm but true length is 3.14 mm.
        tracks = [_track("ARCNET", 0, 0, 2, 0, length=3.14)]
        report = compute_net_lengths(tracks, [])
        assert report["ARCNET"]["lengthMm"] == pytest.approx(3.14)

    def test_vias_counted_not_measured(self):
        tracks = [_track("N1", 0, 0, 1, 0, layer="F.Cu"), _track("N1", 1, 0, 2, 0, layer="B.Cu")]
        vias = [{"net": "N1"}, {"net": "N1"}]
        report = compute_net_lengths(tracks, vias)
        assert report["N1"]["viaCount"] == 2
        assert report["N1"]["lengthMm"] == pytest.approx(2.0)
        assert report["N1"]["layers"] == ["B.Cu", "F.Cu"]

    def test_via_only_net_appears_with_zero_length(self):
        report = compute_net_lengths([], [{"net": "LONELY"}])
        assert report["LONELY"]["lengthMm"] == 0.0
        assert report["LONELY"]["viaCount"] == 1


@pytest.mark.unit
class TestBuildLengthReport:
    PER_NET = {
        "DDR_DQ0": {"lengthMm": 50.0, "segmentCount": 3, "viaCount": 0, "layers": ["F.Cu"]},
        "DDR_DQ1": {"lengthMm": 52.5, "segmentCount": 4, "viaCount": 1, "layers": ["F.Cu"]},
        "GND": {"lengthMm": 200.0, "segmentCount": 40, "viaCount": 12, "layers": ["B.Cu", "F.Cu"]},
    }

    def test_no_filter_reports_all_sorted_by_length(self):
        report = build_length_report(self.PER_NET)
        assert report["netCount"] == 3
        assert [r["net"] for r in report["nets"]] == ["GND", "DDR_DQ1", "DDR_DQ0"]

    def test_pattern_filter_and_skew(self):
        report = build_length_report(self.PER_NET, pattern="DDR_DQ*")
        assert report["netCount"] == 2
        assert report["skew"]["longestNet"] == "DDR_DQ1"
        assert report["skew"]["shortestNet"] == "DDR_DQ0"
        assert report["skew"]["maxSkewMm"] == pytest.approx(2.5)

    def test_explicit_nets_with_missing_reported(self):
        report = build_length_report(self.PER_NET, nets=["DDR_DQ0", "NOPE"])
        assert report["netCount"] == 1
        assert report["missingNets"] == ["NOPE"]

    def test_single_net_has_no_skew(self):
        report = build_length_report(self.PER_NET, nets=["GND"])
        assert "skew" not in report

    def test_nets_and_pattern_union(self):
        report = build_length_report(self.PER_NET, nets=["GND"], pattern="DDR_DQ*")
        assert report["netCount"] == 3
