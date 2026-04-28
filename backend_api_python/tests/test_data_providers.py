"""Test data_providers cache layer and helpers."""
from app.data_providers import safe_float, get_cached, set_cached, clear_cache


def test_safe_float_valid():
    assert safe_float("42.5") == 42.5
    assert safe_float(10) == 10.0


def test_safe_float_invalid():
    assert safe_float("bad") == 0.0
    assert safe_float(None, -1.0) == -1.0
    assert safe_float("", 0.0) == 0.0


def test_cache_round_trip():
    """set_cached + get_cached should return the same data."""
    set_cached("_test_unit", {"msg": "hello"}, 60)
    result = get_cached("_test_unit")
    assert result == {"msg": "hello"}
    clear_cache()
    assert get_cached("_test_unit") is None
