"""Validate pytest selectors as data, never as shell commands."""
import re
from pathlib import Path


def validate_test_ids(root: Path, selectors: list[str]) -> list[str]:
    if not selectors or len(selectors) > 8:
        raise ValueError("Expected one to eight test selectors")
    for selector in selectors:
        if not re.fullmatch(r"[A-Za-z0-9_/-]+\.py(?:::[A-Za-z_][A-Za-z0-9_]*){0,2}", selector):
            raise ValueError("Unsupported pytest selector")
        name = selector.split("::", 1)[0]
        path = Path(name)
        if path.is_absolute() or not path.name.startswith("test_") or any(
            part in ("", ".", "..") for part in name.split("/")
        ):
            raise ValueError("Unsafe test path")
        resolved = (root / path).resolve(strict=True)
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            raise ValueError("Test path escaped workspace")
    return list(dict.fromkeys(selectors))
