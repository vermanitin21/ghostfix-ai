"""Pure validation of a deliberately narrow unified-diff subset. No IO or execution."""

import re
from pathlib import Path, PurePosixPath

from app.agent.state import DiagnosisInput, PatchLimits, PatchProposal, PatchValidationResult


def _path(name: str, root: Path, supplied: dict[str, str]) -> str:
    parts = name.split("/")
    if not name or any(part in ("", ".", "..") for part in parts):
        raise ValueError("Invalid or traversing target path")
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", name) or PurePosixPath(name).is_absolute():
        raise ValueError("Only relative canonical paths are supported")
    if not root.is_absolute() or not (root / name).is_relative_to(root):
        raise ValueError("Target is outside the approved root")
    if any(part.startswith(".") or re.search(
        r"env|secret|credential|password|token|private|lock|requirements|depend|pipeline", part, re.I,
    ) for part in parts):
        raise ValueError("Blocked sensitive, metadata, or dependency path")
    if any(part.lower() in {"generated", "build", "dist", "vendor", "node_modules", "__pycache__"}
           for part in parts):
        raise ValueError("Generated and vendored paths are forbidden")
    if parts[-1].lower() in {"setup.py", "conftest.py"} or PurePosixPath(name).suffix != ".py":
        raise ValueError("Only ordinary Python source files are supported")
    if name not in supplied:
        raise ValueError("Target was not supplied in repository context")
    if "[REDACTED" in supplied[name]:
        raise ValueError("Redacted source is not eligible for patching")
    return name


def validate_patch(
    proposal: PatchProposal, context: DiagnosisInput, approved_root: Path,
    limits: PatchLimits | None = None,
) -> PatchValidationResult:
    """Match hunks against the approved context snapshot, without applying them.

    Context must originate from RepositoryContext. Live link/containment and stale
    snapshot checks belong to the orchestrator before this pure function is called.
    """
    limits = limits or PatchLimits()
    modified: list[str] = []
    changed = 0
    try:
        supplied = {file.path: file.content for file in context.files}
        if len(supplied) != len(context.files):
            raise ValueError("Duplicate context paths")
        targets = proposal.target_files
        if len(targets) > limits.max_files or len(set(targets)) != len(targets):
            raise ValueError("Too many or duplicate target files")
        for name in targets:
            _path(name, approved_root, supplied)
        for test in proposal.tests_to_run:
            path = test.split("::", 1)[0]
            if path not in supplied or not PurePosixPath(path).name.startswith("test_"):
                raise ValueError("Test reference must identify a supplied test file")
        diff = proposal.unified_diff
        if len(diff.encode("utf-8")) > limits.max_diff_bytes:
            raise ValueError("Diff byte limit exceeded")
        if any(ord(char) < 32 and char not in "\n\t" for char in diff) or "\x7f" in diff:
            raise ValueError("Binary or control content is forbidden; use LF newlines")
        if not diff.endswith("\n"):
            raise ValueError("Unified diff must end with a newline")
        lines = diff.splitlines(keepends=True)
        i = 0
        while i < len(lines):
            git_paths = None
            if lines[i].startswith("diff --git "):
                match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)\n", lines[i])
                if not match:
                    raise ValueError("Malformed git diff header")
                git_paths = match.groups()
                i += 1
                if i < len(lines) and lines[i].startswith("index "):
                    if not re.fullmatch(r"index [0-9a-f]+\.\.[0-9a-f]+(?: 100644)?\n", lines[i]):
                        raise ValueError("Unsupported index or file mode")
                    i += 1
            if i + 1 >= len(lines) or not lines[i].startswith("--- a/") or not lines[i + 1].startswith("+++ b/"):
                raise ValueError("Expected existing-file headers; creation/deletion/metadata unsupported")
            old = lines[i][6:-1]
            new = lines[i + 1][6:-1]
            _path(old, approved_root, supplied)
            _path(new, approved_root, supplied)
            if old != new or (git_paths is not None and git_paths != (old, new)):
                raise ValueError("Renames and inconsistent headers are forbidden")
            if old in modified or old not in targets:
                raise ValueError("Duplicate or undeclared diff target")
            modified.append(old)
            if len(modified) > limits.max_files:
                raise ValueError("Too many modified files")
            source = supplied[old].replace("\r\n", "\n").splitlines(keepends=True)
            i += 2
            previous_end = 0
            delta = 0
            hunks = 0
            file_changes = 0
            while i < len(lines) and lines[i].startswith("@@ "):
                match = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n", lines[i])
                if not match:
                    raise ValueError("Malformed hunk header")
                start, count, new_start, new_count = match.groups()
                start, new_start = int(start), int(new_start)
                count, new_count = int(count or 1), int(new_count or 1)
                position = start - 1 if count else start
                new_position = new_start - 1 if new_count else new_start
                if position < previous_end or position < 0 or position + count > len(source):
                    raise ValueError("Hunks overlap or exceed source bounds")
                if new_position != position + delta:
                    raise ValueError("Inconsistent new-file hunk coordinates")
                i += 1
                before, after = [], []
                while len(before) < count or len(after) < new_count:
                    if i >= len(lines) or lines[i][0] not in " +-":
                        raise ValueError("Malformed or incomplete hunk body")
                    line = lines[i]
                    if line[0] in " -":
                        before.append(line[1:])
                    if line[0] in " +":
                        after.append(line[1:])
                    if line[0] in "+-":
                        changed += 1
                        file_changes += 1
                    i += 1
                    if len(before) > count or len(after) > new_count:
                        raise ValueError("Hunk line counts do not match")
                    if changed > limits.max_changed_lines:
                        raise ValueError("Changed-line limit exceeded")
                if before != source[position:position + count]:
                    raise ValueError("Hunk does not match supplied source")
                if before == after:
                    raise ValueError("No-op hunks are forbidden")
                previous_end = position + count
                delta += new_count - count
                hunks += 1
            if not hunks or not file_changes or len(source) + delta <= 0:
                raise ValueError("Empty patch or whole-file deletion is forbidden")
        if set(modified) != set(targets):
            raise ValueError("Declared targets differ from parsed diff targets")
    except ValueError as error:
        return PatchValidationResult(valid=False, errors=[str(error)], modified_files=modified,
                                     changed_lines=changed, limits=limits)
    return PatchValidationResult(valid=True, modified_files=modified, changed_lines=changed, limits=limits)


def patched_contents(proposal: PatchProposal, context: DiagnosisInput, root: Path,
                     limits: PatchLimits) -> dict[str, str]:
    """Compute validated replacement text in memory; never write or execute it."""
    validation = validate_patch(proposal, context, root, limits)
    if not validation.valid:
        raise ValueError("Candidate failed application-boundary validation")
    sources = {file.path: file.content.replace("\r\n", "\n").splitlines(keepends=True)
               for file in context.files}
    lines = proposal.unified_diff.splitlines(keepends=True)
    outputs: dict[str, str] = {}
    i = 0
    while i < len(lines):
        if not lines[i].startswith("--- a/"):
            i += 1
            continue
        name = lines[i][6:-1]
        source, output, cursor = sources[name], [], 0
        i += 2
        while i < len(lines) and lines[i].startswith("@@ "):
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", lines[i])
            start, count, _, new_count = match.groups()
            count, new_count = int(count or 1), int(new_count or 1)
            position = int(start) - 1 if count else int(start)
            output.extend(source[cursor:position])
            old_seen = new_seen = 0
            i += 1
            while old_seen < count or new_seen < new_count:
                line = lines[i]
                if line[0] in " -":
                    old_seen += 1
                if line[0] in " +":
                    output.append(line[1:])
                    new_seen += 1
                i += 1
            cursor = position + count
        output.extend(source[cursor:])
        outputs[name] = "".join(output)
    return outputs
