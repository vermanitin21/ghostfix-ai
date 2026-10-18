def subtotal(prices):
    return sum(prices)


def discounted_total(prices, discount):
    return subtotal(prices) - discount
