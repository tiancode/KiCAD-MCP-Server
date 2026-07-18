"""Unit tests for the shared nm→mm/mil/inch conversion helper.

Both the SWIG and IPC get_component_pads paths route through utils.units so
the conversion factors can't drift between backends.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from utils.units import (  # noqa: E402
    InvalidUnitError,
    nm_to_unit,
    normalize_unit,
    unit_to_nm_scale,
)


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


# ---- unit_to_nm_scale: input parsing (mm default, refuse garbage) ----------


def test_unit_to_nm_scale_known_units():
    assert unit_to_nm_scale("mm") == 1_000_000
    assert unit_to_nm_scale("mil") == 25_400
    assert unit_to_nm_scale("inch") == 25_400_000


def test_unit_to_nm_scale_none_defaults_to_mm():
    # Missing unit is the uniform default of millimetres — NOT inch (the
    # historical bug where a missing/garbled unit silently scaled input ×25.4).
    assert unit_to_nm_scale(None) == 1_000_000


def test_unit_to_nm_scale_unknown_unit_raises():
    with pytest.raises(InvalidUnitError):
        unit_to_nm_scale("banana")


def test_invalid_unit_error_carries_unit_and_names_valid_units():
    try:
        unit_to_nm_scale("furlong")
    except InvalidUnitError as e:
        assert e.unit == "furlong"
        msg = str(e)
        assert "mm" in msg and "mil" in msg and "inch" in msg
    else:  # pragma: no cover - the raise above must fire
        pytest.fail("expected InvalidUnitError")


def test_failed_converts_invalid_unit_to_validation_refusal():
    from utils.responses import failed

    out = failed("Failed to add via", InvalidUnitError("banana"))
    assert out["success"] is False
    assert out["errorCode"] == "VALIDATION"
    assert "banana" in out["message"]
    assert "INTERNAL_ERROR" not in out.get("errorCode", "")


def test_classify_failure_type_branch_for_invalid_unit():
    from utils.failure import classify_failure

    code, hint = classify_failure("x", "y", exc=InvalidUnitError("banana"))
    assert code == "VALIDATION"
    assert hint and "mm" in hint
