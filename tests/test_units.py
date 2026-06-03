"""Unit tests for the shared nm→mm/mil/inch conversion helper.

Both the SWIG and IPC get_component_pads paths route through utils.units so
the conversion factors can't drift between backends.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from utils.units import nm_to_unit, normalize_unit  # noqa: E402


def test_nm_to_unit_mm():
    assert nm_to_unit(1_000_000, "mm") == 1.0


def test_nm_to_unit_mil():
    # 1 mil = 25_400 nm
    assert nm_to_unit(25_400, "mil") == 1.0


def test_nm_to_unit_inch():
    # 1 inch = 25_400_000 nm
    assert nm_to_unit(25_400_000, "inch") == 1.0


def test_nm_to_unit_defaults_to_mm():
    assert nm_to_unit(1_000_000) == 1.0
    assert nm_to_unit(1_000_000, "bogus") == 1.0
    assert nm_to_unit(1_000_000, None) == 1.0


def test_normalize_unit():
    assert normalize_unit("mm") == "mm"
    assert normalize_unit("mil") == "mil"
    assert normalize_unit("inch") == "inch"
    assert normalize_unit(None) == "mm"
    assert normalize_unit("furlong") == "mm"
