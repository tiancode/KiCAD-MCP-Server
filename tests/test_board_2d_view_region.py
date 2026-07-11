"""Tests for get_board_2d_view region cropping (SVG viewBox rewrite)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.board._svg_region import _parse_length_mm, crop_svg_to_region

SVG = (
    '<?xml version="1.0" standalone="no"?>\n'
    '<svg xmlns="http://www.w3.org/2000/svg" version="1.1"\n'
    '  width="297.002mm" height="210.002mm" viewBox="0.000 0.000 297002 210002">\n'
    '<g><rect x="1" y="1" width="10" height="10"/></g>\n'
    "</svg>\n"
)


@pytest.mark.unit
class TestParseLength:
    def test_mm(self):
        assert _parse_length_mm("297.002mm") == pytest.approx(297.002)

    def test_cm(self):
        assert _parse_length_mm("29.7cm") == pytest.approx(297.0)

    def test_unitless(self):
        assert _parse_length_mm("100") == pytest.approx(100.0)

    def test_garbage(self):
        assert _parse_length_mm("wide") is None


@pytest.mark.unit
class TestCropSvgToRegion:
    def test_viewbox_rewritten_to_region(self):
        out = crop_svg_to_region(SVG, (10.0, 20.0, 60.0, 50.0))
        assert out is not None
        # scale = 297002 / 297.002 = 1000 viewBox units per mm
        assert 'viewBox="10000.000 20000.000 50000.000 30000.000"' in out
        assert 'width="50.000mm"' in out
        assert 'height="30.000mm"' in out

    def test_body_content_untouched(self):
        out = crop_svg_to_region(SVG, (0.0, 0.0, 100.0, 100.0))
        assert "<rect" in out and "</svg>" in out

    def test_nonzero_viewbox_origin_offsets(self):
        svg = SVG.replace('viewBox="0.000 0.000 297002 210002"', 'viewBox="500 600 297002 210002"')
        out = crop_svg_to_region(svg, (1.0, 2.0, 3.0, 4.0))
        assert 'viewBox="1500.000 2600.000 2000.000 2000.000"' in out

    def test_degenerate_region_rejected(self):
        assert crop_svg_to_region(SVG, (10.0, 10.0, 10.0, 20.0)) is None
        assert crop_svg_to_region(SVG, (10.0, 30.0, 20.0, 20.0)) is None

    def test_missing_viewbox_rejected(self):
        svg = SVG.replace(' viewBox="0.000 0.000 297002 210002"', "")
        assert crop_svg_to_region(svg, (0.0, 0.0, 10.0, 10.0)) is None

    def test_not_svg_rejected(self):
        assert crop_svg_to_region("<html></html>", (0.0, 0.0, 10.0, 10.0)) is None
