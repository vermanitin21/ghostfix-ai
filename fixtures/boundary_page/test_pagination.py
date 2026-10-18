import pytest
from pagination import page


def test_complete_page():
    assert page([1, 2, 3, 4, 5], 2, 2) == [3, 4]


def test_empty_input():
    assert page([], 1, 3) == []


def test_page_beyond_end():
    assert page([1, 2], 5, 2) == []


def test_invalid_bounds():
    with pytest.raises(ValueError):
        page([1], 0, 2)
    with pytest.raises(ValueError):
        page([1], 1, 0)
