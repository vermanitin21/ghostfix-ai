def read_limit(settings):
    """Use 10 when limit is absent; propagate errors in supplied values."""
    try:
        return int(settings["limit"])
    except (KeyError, ValueError):
        return 10
