"""Bounded, read-only context collection. Never import repository code."""

import re
import stat
from pathlib import Path, PureWindowsPath

from app.agent.state import ContextFile


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?im)^.*(?:api[_-]?key|access[_-]?token|password|secret|authorization)\s*[:=].*$",
        "[REDACTED sensitive line]", text,
    )
    return re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----",
        "[REDACTED private key]", text,
    )


class RepositoryContext:
    MAX_FILES = 4
    MAX_FILE_BYTES = 4096
    MAX_TOTAL_BYTES = 8192

    def __init__(self, approved_root: Path, secrets: tuple[str, ...] = ()) -> None:
        self.root = approved_root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Approved root must be a directory")
        self.secrets = secrets

    def read_files(self, paths: list[str]) -> list[ContextFile]:
        if not paths or len(paths) > self.MAX_FILES:
            raise ValueError("Context requires one to four explicitly selected files")
        result = []
        total = 0
        for name in dict.fromkeys(paths):
            relative = Path(name)
            if relative.is_absolute() or PureWindowsPath(name).drive or "\\" in name or ":" in name:
                raise ValueError("Context paths must be relative POSIX paths")
            if any(part in ("..", ".") or part.startswith(".") for part in relative.parts):
                raise ValueError("Traversal and hidden paths are forbidden")
            if any(re.search(r"env|secret|credential|password|token|private|api[_-]?key", part, re.I)
                   for part in relative.parts):
                raise ValueError("Sensitive paths are forbidden")
            if relative.suffix.lower() not in {".py", ".txt", ".md"}:
                raise ValueError("Unsupported text file type")
            candidate = self.root
            for part in relative.parts:
                candidate = candidate / part
                info = candidate.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("Links and reparse points are forbidden")
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(self.root):
                raise ValueError("Context path escaped approved root")
            if not resolved.is_file() or resolved.stat().st_nlink > 1:
                raise ValueError("Only regular, non-hardlinked files are accepted")
            with resolved.open("rb") as source:
                data = source.read(self.MAX_FILE_BYTES + 1)
            total += len(data)
            if len(data) > self.MAX_FILE_BYTES or total > self.MAX_TOTAL_BYTES:
                raise ValueError("Repository context exceeds size budget")
            content = data.decode("utf-8")
            if any(ord(char) < 32 and char not in "\n\r\t" for char in content):
                raise ValueError("Binary content is forbidden")
            result.append(ContextFile(path=relative.as_posix(), content=redact(content, self.secrets)))
        return result
