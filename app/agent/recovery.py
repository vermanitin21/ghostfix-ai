"""Deterministic, source-grounded recovery of diff REPRESENTATION. Pure; no IO or execution.

Recovery never repairs semantics: it never invents, deletes, reorders or edits body
text, never changes paths or hunk starts, and never searches source. Each change is
recorded, and the strict validator in app.agent.patches remains the final authority.
"""
import re
from pathlib import Path, PurePosixPath

from pydantic import BaseModel

from app.agent import patches
from app.agent.state import DiagnosisInput, PatchLimits, PatchProposal, PatchTransformation, PatchValidationResult

_METADATA = ("new file mode", "deleted file mode", "old mode", "new mode", "rename from", "rename to",
             "similarity index", "dissimilarity index", "copy from", "copy to", "Binary files", "GIT binary patch")
_BOUNDARIES = ("@@ ", "diff --git ", "--- ", "+++ ")
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@([^\n]*)\n")


class RepresentationRecovery(BaseModel):
    candidate: PatchProposal
    raw_validation: PatchValidationResult
    validation: PatchValidationResult
    transformations: list[PatchTransformation]
    # Steps computed before a rejection; discarded, reported for audit only.
    attempted: list[PatchTransformation]
    rejection: str | None = None


class _Gate(ValueError):
    """Policy failure found before any transformation is attempted."""


def _gate(proposal: PatchProposal, supplied: dict[str, str], root: Path, limits: PatchLimits) -> None:
    """Fail closed on policy hazards anywhere in the text before recovering anything."""
    diff = proposal.unified_diff
    if len(diff.encode("utf-8")) > limits.max_diff_bytes:
        raise _Gate("Diff byte limit exceeded")
    if any(ord(char) < 32 and char not in "\n\t" for char in diff) or "\x7f" in diff:
        raise _Gate("Binary or control content is forbidden; use LF newlines")
    if any(char in diff for char in "\x85\u2028\u2029"):
        raise _Gate("Unicode line separators are unsupported")
    targets = proposal.target_files
    if len(targets) > limits.max_files or len(set(targets)) != len(targets):
        raise _Gate("Too many or duplicate target files")
    try:
        for name in targets:
            patches._path(name, root, supplied)
        for line in diff.split("\n"):
            if line.startswith(("--- ", "+++ ")):
                match = re.fullmatch(r"(?:--- a|\+\+\+ b)/(\S+)", line)
                if not match:
                    raise ValueError("Expected existing-file headers; creation/deletion/metadata unsupported")
                patches._path(match.group(1), root, supplied)
                if match.group(1) not in targets:
                    raise ValueError("Duplicate or undeclared diff target")
            elif line.startswith("diff --git "):
                match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", line)
                if not match:
                    raise ValueError("Malformed git diff header")
                for name in match.groups():
                    patches._path(name, root, supplied)
            elif line.startswith(_METADATA):
                raise ValueError("Unsupported file metadata operation")
            elif line.startswith("\\"):
                raise ValueError("No-newline markers are unsupported by the strict subset")
    except ValueError as error:
        raise _Gate(str(error)) from None
    for test in proposal.tests_to_run:
        path = test.split("::", 1)[0]
        if path not in supplied or not PurePosixPath(path).name.startswith("test_"):
            raise _Gate("Test reference must identify a supplied test file")


def _recover(diff: str, supplied: dict[str, str], steps: list[PatchTransformation]) -> str:
    def record(category, detail, file=None, hunk=None, diff_line=None):
        steps.append(PatchTransformation(order=len(steps) + 1, category=category, file=file,
                                         hunk=hunk, diff_line=diff_line, detail=detail))

    if not diff.endswith("\n"):
        # Transport only: exactly one LF; a backslash marker was already rejected.
        diff += "\n"
        record("TERMINAL_NEWLINE_CANONICALIZATION", "Appended exactly one final LF",
               diff_line=len(diff.splitlines()))
    lines = diff.splitlines(keepends=True)
    out: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(lines):
        if lines[i].startswith("diff --git "):
            out.append(lines[i])
            i += 1
            if i < len(lines) and lines[i].startswith("index "):
                out.append(lines[i])
                i += 1
        if i + 1 >= len(lines) or not lines[i].startswith("--- a/") or not lines[i + 1].startswith("+++ b/"):
            raise ValueError("Expected existing-file headers; creation/deletion/metadata unsupported")
        name = lines[i][6:-1]
        if name != lines[i + 1][6:-1] or name in seen:
            raise ValueError("Renames, duplicate or inconsistent headers are forbidden")
        seen.add(name)
        source = supplied[name].replace("\r\n", "\n").splitlines(keepends=True)
        out.extend(lines[i:i + 2])
        i += 2
        hunk = 0
        if i >= len(lines) or not lines[i].startswith("@@ "):
            raise ValueError("Empty file section")
        while i < len(lines) and lines[i].startswith("@@ "):
            match = _HUNK.fullmatch(lines[i])
            if not match:
                raise ValueError("Malformed hunk header")
            hunk += 1
            start, old, new_start, new, section = match.groups()
            declared_old, declared_new = int(old or 1), int(new or 1)
            header_at = len(out)
            out.append(lines[i])
            i += 1
            # Cursor starts at the declared position; it is never moved to find a match.
            cursor = int(start) - 1 if declared_old else int(start)
            body_old = body_new = 0
            prefix_recovered = False
            while i < len(lines):
                line = lines[i]
                proven = cursor < len(source)
                if line.startswith(_BOUNDARIES):
                    # A boundary that could also be read as body text is ambiguous.
                    if proven and source[cursor] in (line, line[1:]):
                        raise ValueError("Ambiguous hunk body boundary")
                    break
                if line[0] not in " +-":
                    # Complete-line equality at the exact cursor only; no search.
                    if not proven or line != source[cursor]:
                        raise ValueError("Malformed or incomplete hunk body")
                    line = " " + line
                    prefix_recovered = True
                    record("MISSING_CONTEXT_PREFIX_RECOVERY",
                           "Restored context prefix proven by source line %d" % (cursor + 1),
                           file=name, hunk=hunk, diff_line=i + 1)
                if line[0] in " -":
                    if not proven or line[1:] != source[cursor]:
                        raise ValueError("Hunk does not match supplied source")
                    cursor += 1
                    body_old += 1
                if line[0] in " +":
                    body_new += 1
                out.append(line)
                i += 1
            if not body_old and not body_new:
                raise ValueError("Malformed or incomplete hunk body")
            if (body_old, body_new) == (declared_old, declared_new):
                continue
            # Counts are the only evidence corroborating a recovered prefix, and a
            # header claiming lines the body lacks may hide an omitted edit.
            if prefix_recovered:
                raise ValueError("Hunk line counts do not match a prefix-recovered body")
            if body_old < declared_old or body_new < declared_new:
                raise ValueError("Hunk header claims lines absent from the body")
            if (declared_old == 0) != (body_old == 0) or (declared_new == 0) != (body_new == 0):
                raise ValueError("Count recovery would change hunk coordinate meaning")
            out[header_at] = f"@@ -{start},{body_old} +{new_start},{body_new} @@{section}\n"
            record("HUNK_COUNT_METADATA_RECOVERY",
                   f"Header counts -{declared_old} +{declared_new} -> -{body_old} +{body_new} from aligned body",
                   file=name, hunk=hunk, diff_line=header_at + 1)
    return "".join(out)


def recover_representation(proposal: PatchProposal, context: DiagnosisInput, root: Path,
                           limits: PatchLimits | None = None) -> RepresentationRecovery:
    """Strict first; recover only provable representation defects; strict again."""
    limits = limits or PatchLimits()
    raw = patches.validate_patch(proposal, context, root, limits)
    result = dict(candidate=proposal, raw_validation=raw, validation=raw, transformations=[], attempted=[])
    if raw.valid:
        return RepresentationRecovery(**result)
    supplied = {file.path: file.content for file in context.files}
    steps: list[PatchTransformation] = []
    try:
        if len(supplied) != len(context.files):
            raise _Gate("Duplicate context paths")
        _gate(proposal, supplied, root, limits)
        recovered = _recover(proposal.unified_diff, supplied, steps)
    except _Gate as error:
        # Policy outranks any earlier format error in the strict report.
        gate = PatchValidationResult(valid=False, errors=[str(error)], limits=limits,
                                     failure_category=patches._category(str(error)))
        return RepresentationRecovery(**{**result, "validation": gate, "rejection": str(error)})
    except (ValueError, KeyError, IndexError) as error:
        reason = str(error) if isinstance(error, ValueError) else "Malformed or incomplete hunk body"
        return RepresentationRecovery(**{**result, "attempted": steps, "rejection": reason})
    candidate = proposal.model_copy(update={"unified_diff": recovered})
    strict = patches.validate_patch(candidate, context, root, limits)
    if not strict.valid:
        return RepresentationRecovery(**{**result, "attempted": steps,
                                         "rejection": "Recovered text failed strict validation: " + strict.errors[0]})
    return RepresentationRecovery(**{**result, "candidate": candidate, "validation": strict, "transformations": steps})
