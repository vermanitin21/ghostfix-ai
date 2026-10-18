import pytest
from limits import read_limit


def test_invalid_supplied_limit_propagates():
    with pytest.raises(ValueError):
        read_limit({"limit": "many"})


def test_missing_limit_defaults():
    assert read_limit({}) == 10


def test_numeric_string():
    assert read_limit({"limit": "25"}) == 25


def test_zero_is_not_missing():
    assert read_limit({"limit": 0}) == 0


def test_incompatible_value_propagates():
    with pytest.raises(TypeError):
        read_limit({"limit": None})
