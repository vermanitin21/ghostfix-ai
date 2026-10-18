import pytest
from eligibility import free_shipping


def test_independent_qualifications():
    assert free_shipping(50, False)
    assert free_shipping(10, True)


def test_both_qualifications():
    assert free_shipping(80, True)


def test_no_qualification():
    assert not free_shipping(49, False)


def test_negative_total():
    with pytest.raises(ValueError):
        free_shipping(-1, True)
