"""Unit tests for the shared list-pagination helper."""

from utils.pagination import DEFAULT_LIMIT, paginate


def test_default_caps_at_default_limit():
    items = list(range(250))
    page, meta = paginate(items, {})
    assert page == list(range(DEFAULT_LIMIT))
    assert meta == {
        "total": 250,
        "count": DEFAULT_LIMIT,
        "offset": 0,
        "limit": DEFAULT_LIMIT,
        "truncated": True,
    }


def test_no_truncation_when_under_limit():
    items = [1, 2, 3]
    page, meta = paginate(items, {})
    assert page == [1, 2, 3]
    assert meta["total"] == 3
    assert meta["count"] == 3
    assert meta["truncated"] is False


def test_offset_and_limit():
    items = list(range(10))
    page, meta = paginate(items, {"offset": 3, "limit": 4})
    assert page == [3, 4, 5, 6]
    assert meta["offset"] == 3
    assert meta["limit"] == 4
    assert meta["truncated"] is True


def test_offset_past_end_returns_empty():
    page, meta = paginate([1, 2], {"offset": 5})
    assert page == []
    assert meta["count"] == 0
    assert meta["truncated"] is False
    assert meta["total"] == 2


def test_limit_zero_means_no_cap():
    items = list(range(150))
    page, meta = paginate(items, {"limit": 0})
    assert page == items
    assert meta["limit"] is None
    assert meta["truncated"] is False


def test_negative_offset_clamped():
    page, meta = paginate([1, 2, 3], {"offset": -5, "limit": 2})
    assert page == [1, 2]
    assert meta["offset"] == 0


def test_non_numeric_params_fall_back():
    items = list(range(200))
    page, meta = paginate(items, {"offset": "abc", "limit": "xyz"})
    assert meta["offset"] == 0
    assert meta["limit"] == DEFAULT_LIMIT
    assert len(page) == DEFAULT_LIMIT
