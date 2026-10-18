from orders import order_total


def test_discount_preserves_shipping_eligibility():
    assert order_total([30, 30], 20) == 40


def test_small_order_shipping():
    assert order_total([20], 0) == 25


def test_free_shipping_boundary():
    assert order_total([50], 0) == 50


def test_below_shipping_boundary():
    assert order_total([45], 0) == 50


def test_discount_on_small_order():
    assert order_total([30], 10) == 25
