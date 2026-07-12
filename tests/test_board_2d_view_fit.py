"""Regression: get_board_2d_view must honor explicit width/height.

Phase C E2E found full-board renders ignored a requested width/height when
cropToBoard was true (the alpha-crop resized down to native content
resolution).  The fit helper rescales the cropped board into the requested
box, aspect-preserving.
"""

import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.board.view import _fit_within_box


@pytest.mark.unit
def test_fit_scales_down_preserving_aspect():
    img = Image.new("RGBA", (400, 200))
    out = _fit_within_box(img, 100, 100)
    assert out.size == (100, 50)


@pytest.mark.unit
def test_fit_scales_up_preserving_aspect():
    # A small cropped board (100x50) asked to fill a 1400x1200 box.
    # scale = min(1400/100, 1200/50) = 14 -> 1400x700 (fits within the box).
    img = Image.new("RGBA", (100, 50))
    out = _fit_within_box(img, 1400, 1200)
    assert out.size == (1400, 700)


@pytest.mark.unit
def test_fit_is_noop_when_size_already_matches():
    img = Image.new("RGBA", (100, 100))
    assert _fit_within_box(img, 100, 100) is img


@pytest.mark.unit
def test_fit_returns_original_on_degenerate_box():
    img = Image.new("RGBA", (100, 100))
    assert _fit_within_box(img, 0, 100) is img
    assert _fit_within_box(img, 100, -5) is img
