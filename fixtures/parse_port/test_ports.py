import pytest
from ports import parse_port


def test_non_integer_notation_rejected():
    for text in ["80.5", "1e2"]:
        with pytest.raises(ValueError):
            parse_port(text)


def test_valid_boundaries():
    assert parse_port("1") == 1
    assert parse_port("65535") == 65535


def test_whitespace():
    assert parse_port(" 8080 ") == 8080


def test_out_of_range():
    for text in ["0", "-1", "65536"]:
        with pytest.raises(ValueError):
            parse_port(text)


def test_non_numeric():
    with pytest.raises(ValueError):
        parse_port("http")
