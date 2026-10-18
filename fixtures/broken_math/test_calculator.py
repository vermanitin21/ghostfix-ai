from calculator import add


def test_adds_two_numbers() -> None:
    assert add(2, 3) == 5


def test_right_identity() -> None:
    assert add(7, 0) == 7


def test_negative_right_identity() -> None:
    assert add(-4, 0) == -4


def test_zero_pair() -> None:
    assert add(0, 0) == 0
