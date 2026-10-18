def parse_port(text):
    """Parse an integer port, allowing surrounding whitespace, in range 1..65535."""
    port = int(float(text))
    if not 1 <= port <= 65535:
        raise ValueError("Port out of range")
    return port
