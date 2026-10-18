def free_shipping(total, member):
    """Members or orders of at least 50 qualify; negative totals are invalid."""
    if total < 0:
        raise ValueError("Negative order total")
    return total >= 50 and member
