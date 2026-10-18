"""Bounded output sanitization; never serialize exceptions or raw environments."""
import re

from app.tools.files import redact


def safe_text(value: str, secrets: tuple[str, ...] = ()) -> str:
    value = redact(value, secrets)
    value = re.sub(r"[A-Za-z]:[\\/][^\s\"'<>]+|\\\\[^\s\"'<>]+", "[host-path]", value)
    value = re.sub(r"(?<![\w.])/(?!/)[^\s\"'<>]+", "[host-path]", value)
    value = re.sub(r"(?i)NEBIUS_API_KEY", "[credential-name]", value)
    return "".join(c for c in value if ord(c) >= 32 and ord(c) != 127)[:1000]
