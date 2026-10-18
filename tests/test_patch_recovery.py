"""Synthetic v3 representation-recovery contracts. No frozen fixtures or reference solutions."""
import asyncio
import re
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent import patches
from app.agent.orchestrator import GhostFixOrchestrator
from app.agent.patches import patched_contents, validate_patch
from app.agent.recovery import recover_representation
from app.agent.repair import RepairLoop
from app.agent.state import (
    ContextFile, Diagnosis, DiagnosisAttempt, DiagnosisInput, PatchLimits, PatchProposal,
    PatchVerification, VerificationTests,
)
from app.llm.nebius_client import CompletionResult
from app.sandbox.models import SandboxResult
from app.tools.files import RepositoryContext

MOD = "def first():\n    return 1\n\n\ndef second():\n    return 2\n"
OTHER = "x = 1\nx = 1\ny = 3\n"
HEAD = "--- a/mod.py\n+++ b/mod.py\n"
OTHER_HEAD = "--- a/other.py\n+++ b/other.py\n"
# Body is fixed; only the header counts vary between the correct and wrong forms.
BODY = " def first():\n-    return 1\n+    value = 1\n+    return value\n"
CORRECT = HEAD + "@@ -1,2 +1,3 @@\n" + BODY
WRONG_NEW = HEAD + "@@ -1,2 +1,2 @@\n" + BODY
WRONG_BOTH = HEAD + "@@ -1,1 +1,2 @@\n" + BODY
SECOND = " def second():\n-    return 2\n+    v = 2\n+    return v\n"
NEWLINE, PREFIX, COUNTS = ("TERMINAL_NEWLINE_CANONICALIZATION", "MISSING_CONTEXT_PREFIX_RECOVERY",
                           "HUNK_COUNT_METADATA_RECOVERY")


@pytest.fixture
def context():
    return DiagnosisInput(exit_code=1, failed_tests=["test_mod.py::test_x"], stdout_excerpt="failure",
        stderr_excerpt="", files=[ContextFile(path="mod.py", content=MOD), ContextFile(path="other.py", content=OTHER),
        ContextFile(path="test_mod.py", content="from mod import first\n\n\ndef test_x():\n    assert first() == 1\n")])


@pytest.fixture
def root(tmp_path):
    # Recovery and validation are lexical; this directory is never created or read.
    return tmp_path / "never-created"


def proposal(diff, targets=("mod.py",), tests=("test_mod.py::test_x",)):
    return PatchProposal(target_files=list(targets), summary="s", rationale="r", unified_diff=diff,
        expected_behavior_change="e", tests_to_run=list(tests), risk_notes=[], confidence=0.5)


def recover(diff, context, root, targets=("mod.py",), limits=None):
    return recover_representation(proposal(diff, targets), context, root, limits)


def assert_representation_only(raw: str, candidate: str) -> None:
    """Only counts, one missing context prefix space per line, and one final LF may differ."""
    if not raw.endswith("\n"):
        assert candidate == raw + "\n" or candidate.endswith("\n") and not candidate.endswith("\n\n")
        raw += "\n"
    raw_lines, new_lines = raw.splitlines(keepends=True), candidate.splitlines(keepends=True)
    assert len(raw_lines) == len(new_lines)
    pattern = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)", re.S)
    for before, after in zip(raw_lines, new_lines):
        if before == after:
            continue
        if before.startswith("@@ "):
            assert pattern.fullmatch(before).group(1, 2, 3) == pattern.fullmatch(after).group(1, 2, 3)
        else:
            assert before[0] not in " +-" and after == " " + before


def assert_rejected(result, raw):
    assert not result.validation.valid and not result.transformations
    assert result.candidate.unified_diff == raw and result.rejection


# A, X: header counts recovered from a complete, source-aligned body; strict authority is unchanged.
@pytest.mark.parametrize("raw", [WRONG_NEW, WRONG_BOTH])
def test_count_metadata_recovered(context, root, raw, monkeypatch):
    strict = Mock(wraps=patches.validate_patch)
    monkeypatch.setattr(patches, "validate_patch", strict)
    result = recover(raw, context, root)
    assert result.validation.valid and result.candidate.unified_diff == CORRECT
    assert not result.raw_validation.valid
    assert [t.category for t in result.transformations] == [COUNTS]
    assert result.transformations[0].file == "mod.py" and result.transformations[0].hunk == 1
    assert strict.call_count == 2  # raw, then the recovered candidate
    assert validate_patch(result.candidate, context, root).valid
    assert_representation_only(raw, result.candidate.unified_diff)
    assert patched_contents(result.candidate, context, root, PatchLimits())["mod.py"].startswith(
        "def first():\n    value = 1\n    return value\n\n")


# B, K, Z: an already-valid raw patch is returned byte-for-byte unchanged.
def test_valid_raw_unchanged(context, root):
    result = recover(CORRECT, context, root)
    assert result.validation.valid and result.candidate.unified_diff == CORRECT
    assert not result.transformations and result.rejection is None


# C: counts wrong and an explicit removal contradicts source at the cursor.
def test_alignment_mismatch_rejected(context, root):
    raw = WRONG_NEW.replace("-    return 1", "-    return 9")
    assert_rejected(recover(raw, context, root), raw)


# D: removal omitted at the end or in the middle of a hunk.
@pytest.mark.parametrize("raw", [
    HEAD + "@@ -1,2 +1,2 @@\n def first():\n+    return 5\n",
    HEAD + "@@ -1,3 +1,4 @@\n def first():\n+    return 5\n \n",
])
def test_missing_removal_rejected(context, root, raw):
    assert_rejected(recover(raw, context, root), raw)


# E: the header claims a new-side line the body lacks; never shrink it into a deletion.
def test_missing_addition_rejected(context, root):
    raw = HEAD + "@@ -1,2 +1,2 @@\n def first():\n-    return 1\n"
    result = recover(raw, context, root)
    assert_rejected(result, raw)
    assert "absent" in result.rejection


# F: malformed bodies, including a prefix-recovered line that the counts mark as a removal.
@pytest.mark.parametrize("raw", [
    HEAD + "@@ -1,2 +1,2 @@\n def first():\n    return 5\n+    return 5\n",
    HEAD + "@@ -1,2 +1,2 @@\n def first():\n    return 1\n+    return 5\n",
    HEAD + "@@ -1,2 +1,4 @@\n def first():\n*    return 1\n+    return 5\n",
])
def test_ambiguous_malformed_body_rejected(context, root, raw):
    assert_rejected(recover(raw, context, root), raw)


# G: a matching body exists elsewhere, but the declared start governs and is never moved.
def test_wrong_start_never_relocated(context, root):
    raw = HEAD + "@@ -4,2 +4,2 @@\n" + SECOND
    result = recover(raw, context, root)
    assert_rejected(result, raw)
    assert result.attempted == []


# H: multiple hunks; metadata changes only where required.
def test_multiple_hunks_only_wrong_one_recovered(context, root):
    first = "@@ -1,2 +1,2 @@\n def first():\n-    return 1\n+    return 10\n"
    raw = HEAD + first + "@@ -5,2 +5,2 @@\n" + SECOND
    result = recover(raw, context, root)
    assert result.validation.valid
    assert result.candidate.unified_diff == HEAD + first + "@@ -5,2 +5,3 @@\n" + SECOND
    assert [(t.category, t.hunk) for t in result.transformations] == [(COUNTS, 2)]
    assert_representation_only(raw, result.candidate.unified_diff)


def test_later_hunk_start_inconsistent_with_recovered_counts_rejected(context, root):
    # Hunk 1 grows the file; hunk 2's new start ignores that. Never rewrite starts.
    raw = WRONG_NEW + "@@ -5,2 +5,2 @@\n def second():\n-    return 2\n+    return 20\n"
    result = recover(raw, context, root)
    assert_rejected(result, raw)
    assert "strict" in result.rejection and "coordinates" in result.rejection


# I: two files recovered independently.
def test_multiple_files_recovered_independently(context, root):
    other = "@@ -2,2 +2,2 @@\n x = 1\n-y = 3\n+y = 4\n+z = 5\n"
    raw = WRONG_NEW + OTHER_HEAD + other
    result = recover(raw, context, root, targets=("mod.py", "other.py"))
    assert result.validation.valid
    assert [(t.file, t.category) for t in result.transformations] == [("mod.py", COUNTS), ("other.py", COUNTS)]
    assert result.candidate.unified_diff == CORRECT + OTHER_HEAD + other.replace("+2,2", "+2,3")
    assert_representation_only(raw, result.candidate.unified_diff)


# J: a missing transport LF gets exactly one LF.
def test_missing_final_newline_appended_once(context, root):
    raw = CORRECT.rstrip("\n")
    result = recover(raw, context, root)
    assert result.validation.valid and result.candidate.unified_diff == raw + "\n"
    assert [t.category for t in result.transformations] == [NEWLINE]


# L: the semantic no-newline marker is preserved verbatim and never treated as transport.
@pytest.mark.parametrize("suffix", ["\\ No newline at end of file\n", "\\ No newline at end of file"])
def test_no_newline_marker_preserved_and_rejected(context, root, suffix):
    raw = CORRECT + suffix
    result = recover(raw, context, root)
    assert_rejected(result, raw)
    assert result.validation.failure_category == "UNSUPPORTED_OPERATION" and result.attempted == []


# M: a trailing blank context line is kept when source proves it at the cursor.
def test_trailing_blank_context_proven_preserved(context, root):
    valid = HEAD + "@@ -1,3 +1,4 @@\n" + BODY + " \n"
    assert recover(valid, context, root).candidate.unified_diff == valid
    raw = valid.replace("@@ -1,3 +1,4 @@", "@@ -1,2 +1,3 @@")
    result = recover(raw, context, root)
    assert result.validation.valid and result.candidate.unified_diff == valid
    assert result.candidate.unified_diff.endswith(" \n")


# N: an unproven trailing blank context line is rejected, never stripped.
@pytest.mark.parametrize("raw", [
    OTHER_HEAD + "@@ -3,1 +3,1 @@\n-y = 3\n+y = 4\n \n",
    OTHER_HEAD + "@@ -3,1 +3,1 @@\n-y = 3\n+y = 4\n ",
])
def test_trailing_blank_context_unproven_rejected(context, root, raw):
    result = recover(raw, context, root, targets=("other.py",))
    assert_rejected(result, raw)
    if not raw.endswith("\n"):
        assert [t.category for t in result.attempted] == [NEWLINE]


# O: a missing context prefix proven at the exact cursor is restored.
def test_missing_prefix_recovered_at_cursor(context, root):
    raw = CORRECT.replace(" def first():", "def first():")
    result = recover(raw, context, root)
    assert result.validation.valid and result.candidate.unified_diff == CORRECT
    assert [(t.category, t.diff_line) for t in result.transformations] == [(PREFIX, 4)]


# P: duplicate source text; the exact cursor governs and no search occurs.
def test_duplicate_text_exact_cursor_governs(context, root):
    good = OTHER_HEAD + "@@ -2,2 +2,2 @@\nx = 1\n-y = 3\n+y = 4\n"
    assert recover(good, context, root, targets=("other.py",)).validation.valid
    shifted = good.replace("@@ -2,2 +2,2 @@", "@@ -1,2 +1,2 @@")
    assert_rejected(recover(shifted, context, root, targets=("other.py",)), shifted)


# Q: an unprefixed line that looks like Python, or matches source elsewhere, is not context.
@pytest.mark.parametrize("line", ["    return 10\n", "    return 2\n", "def second():\n"])
def test_python_looking_line_not_inferred(context, root, line):
    raw = HEAD + "@@ -1,2 +1,2 @@\n def first():\n" + line + "+    return 10\n"
    assert_rejected(recover(raw, context, root), raw)


# R, S: added and removed text are never rewritten, even with odd whitespace.
def test_added_and_removed_text_byte_preserved(context, root):
    raw = HEAD + "@@ -1,1 +1,1 @@\n def first():\n-    return 1\n+    return 1  \t\n+\t# kept\n"
    result = recover(raw, context, root)
    assert result.validation.valid
    old = [l for l in raw.splitlines(keepends=True) if l[0] in "+-" and not l.startswith(("---", "+++"))]
    new = [l for l in result.candidate.unified_diff.splitlines(keepends=True)
           if l[0] in "+-" and not l.startswith(("---", "+++"))]
    assert old == new
    assert_representation_only(raw, result.candidate.unified_diff)


# T, U, V: policy failures reject before any transformation is attempted.
@pytest.mark.parametrize("raw,targets,category", [
    (WRONG_NEW.replace("mod.py", "../mod.py"), ("../mod.py",), "DISALLOWED_PATH"),
    (WRONG_NEW.rstrip("\n") + "\n" + OTHER_HEAD.replace("other.py", "../other.py") + "@@ -1,1 +1,1 @@\n-x = 1\n+x = 2",
     ("mod.py",), "DISALLOWED_PATH"),
    (WRONG_NEW.replace("mod.py", "test_mod.py"), ("test_mod.py",), "TEST_MODIFICATION"),
    (WRONG_NEW + "--- a/test_mod.py\n+++ b/test_mod.py\n@@ -1,1 +1,1 @@\n-from mod import first\n+x\n",
     ("mod.py", "test_mod.py"), "TEST_MODIFICATION"),
    (WRONG_NEW.replace("--- a/mod.py", "--- /dev/null"), ("mod.py",), "UNSUPPORTED_OPERATION"),
    ("diff --git a/mod.py b/mod.py\nnew file mode 100644\n" + WRONG_NEW, ("mod.py",), "UNSUPPORTED_OPERATION"),
    (WRONG_NEW.replace("+++ b/mod.py", "+++ b/other.py"), ("mod.py",), "DISALLOWED_PATH"),
    (WRONG_NEW + "Binary files a/mod.py and b/mod.py differ\n", ("mod.py",), "UNSUPPORTED_OPERATION"),
    (WRONG_NEW.replace("mod.py", ".env"), (".env",), "DISALLOWED_PATH"),
])
def test_policy_rejected_before_recovery(context, root, raw, targets, category):
    result = recover(raw, context, root, targets=targets)
    assert_rejected(result, raw)
    assert result.attempted == [] and result.validation.failure_category == category


def test_rename_with_recoverable_counts_rejected(context, root):
    raw = WRONG_NEW.replace("+++ b/mod.py", "+++ b/other.py")
    assert_rejected(recover(raw, context, root, targets=("mod.py", "other.py")), raw)


# W: ordered provenance across several transformation kinds.
def test_multiple_transformations_ordered(context, root):
    raw = (HEAD + "@@ -1,2 +1,2 @@\ndef first():\n-    return 1\n+    return 10\n"
           + "@@ -5,2 +5,2 @@\n" + SECOND.rstrip("\n"))
    result = recover(raw, context, root)
    assert result.validation.valid
    steps = result.transformations
    assert [(t.order, t.category, t.hunk) for t in steps] == [(1, NEWLINE, None), (2, PREFIX, 1), (3, COUNTS, 2)]
    assert result.candidate.unified_diff == (HEAD + "@@ -1,2 +1,2 @@\n def first():\n-    return 1\n+    return 10\n"
                                             + "@@ -5,2 +5,3 @@\n" + SECOND)
    assert_representation_only(raw, result.candidate.unified_diff)


# Y: recovery is idempotent.
@pytest.mark.parametrize("raw", [WRONG_NEW, WRONG_BOTH, CORRECT.rstrip("\n"), CORRECT.replace(" def", "def", 1)])
def test_recovery_idempotent(context, root, raw):
    once = recover(raw, context, root)
    twice = recover_representation(once.candidate, context, root)
    assert twice.candidate == once.candidate and not twice.transformations and twice.validation.valid


# --- Adversarial ---------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    WRONG_NEW + "Explanation: this fixes the bug.\n",   # suspicious trailing prose
    WRONG_NEW + "\n",                                   # unproven trailing blank line
    WRONG_NEW + "+++ b/mod.py\n",                       # stray header-like line
    WRONG_NEW + "@@ -5,2 +5,2 @@\n",                    # header with no body
    HEAD + "@@ -1,2 +1,2 @@\n",                         # incomplete body
    WRONG_NEW.replace("@@ -1,2 +1,2 @@", "@@ -1,2 +1,2"),  # malformed header
])
def test_suspicious_or_incomplete_text_never_dropped(context, root, raw):
    assert_rejected(recover(raw, context, root), raw)


def test_overstated_old_count_never_removes_or_adds_lines(context, root):
    # Claims three old lines; body explicitly covers two. Absent lines are not invented.
    raw = HEAD + "@@ -1,3 +1,3 @@\n" + BODY
    assert_rejected(recover(raw, context, root), raw)


def test_ambiguous_boundary_with_source_text_rejected(context, root):
    tricky = DiagnosisInput(**{**context.model_dump(), "files": [
        ContextFile(path="mod.py", content="x = 1\n-- a/mod.py\n"), *context.files[1:]]})
    raw = HEAD + "@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n+y = 3\n--- a/mod.py\n"
    assert_rejected(recover(raw, tricky, root), raw)


def test_prefix_and_count_recovery_never_combine_in_one_hunk(context, root):
    # An unprefixed source line could be context or a removal missing "-"; only
    # accurate header counts can tell, so a prefix-recovered hunk keeps its counts.
    for raw, targets in ((OTHER_HEAD + "@@ -2,2 +2,2 @@\n x = 1\ny = 3\n+y = 4\n", ("other.py",)),
                         (WRONG_NEW.replace(" def first():", "def first():"), ("mod.py",))):
        result = recover(raw, context, root, targets=targets)
        assert_rejected(result, raw)
        assert result.rejection == "Hunk line counts do not match a prefix-recovered body"
        assert [t.category for t in result.attempted] == [PREFIX]
    # An unprefixed indented line reads as context with one space lost: never aligned.
    raw = WRONG_NEW.replace("-    return 1", "    return 1")
    result = recover(raw, context, root)
    assert_rejected(result, raw)
    assert result.rejection == "Hunk does not match supplied source" and result.attempted == []


def test_limits_still_enforced_after_recovery(context, root):
    for limits in (PatchLimits(max_changed_lines=2), PatchLimits(max_diff_bytes=len(WRONG_NEW) - 1)):
        assert not recover(WRONG_NEW, context, root, limits=limits).validation.valid


def test_secret_in_added_text_still_rejected_by_orchestrator(context, tmp_path):
    for file in context.files:
        (tmp_path / file.path).write_text(file.content, encoding="utf-8", newline="")
    raw = WRONG_NEW.replace("value = 1", "value = 'sample-private-value'")
    result, llm, _ = propose(context, tmp_path, [proposal(raw)])
    assert not result.validation_result.valid
    assert result.validation_result.failure_category == "SECURITY_POLICY_ERROR"
    assert "sample-private-value" not in result.model_dump_json()
    llm.complete_structured.assert_awaited_once()


# --- Orchestrator interaction ----------------------------------------------------

def propose(context, repo_root, responses):
    sandbox = SandboxResult(exit_code=1, stdout="failure", stderr="", duration_ms=1, command=["docker"])
    diagnosis = DiagnosisAttempt(sandbox=sandbox, context=context, status="diagnosed", diagnosis=Diagnosis(
        failure_summary="f", suspected_files=["mod.py"], suspected_symbols=["first"], root_cause_hypothesis="h",
        supporting_evidence=["e"], confidence=0.5, recommended_next_action="n"))
    llm = Mock(complete_structured=AsyncMock(side_effect=[
        CompletionResult(model="synthetic", content=item.model_dump_json()) for item in responses]))
    runner = Mock()
    agent = GhostFixOrchestrator(runner, llm)
    repository = RepositoryContext(repo_root, ("sample-private-value",))
    result = asyncio.run(agent.propose_patch(diagnosis, repository))
    runner.run.assert_not_called()
    return result, llm, (agent, diagnosis, repository)


@pytest.fixture
def repo(context, tmp_path):
    for file in context.files:
        (tmp_path / file.path).write_text(file.content, encoding="utf-8", newline="")
    return tmp_path


def test_recovery_spends_no_model_call_and_records_provenance(context, repo):
    result, llm, _ = propose(context, repo, [proposal(WRONG_NEW)])
    llm.complete_structured.assert_awaited_once()
    assert result.validation_result.valid and result.protocol_retry_count == 0
    assert result.proposal.unified_diff == CORRECT
    event = result.protocol_events[0]
    assert event.raw_model_diff == WRONG_NEW and event.normalized_diff == CORRECT
    assert event.normalization_reason == "deterministic_representation_recovery"
    assert [t.category for t in event.transformations] == [COUNTS]
    # Strict stops at the declared counts, so the surplus line surfaces as a header error.
    assert not event.raw_validation.valid and event.raw_validation.errors
    assert event.raw_validation == validate_patch(proposal(WRONG_NEW), context, repo)
    assert event.validation.valid and event.recovery_rejection is None


@pytest.mark.parametrize("raw", [
    HEAD + "@@ -1,2 +1,2 @@\n def first():\n-    return 1\n",   # missing addition
    "```diff\n" + WRONG_NEW + "```\n",                            # envelope around a count error
])
def test_failed_recovery_does_not_broaden_retry(context, repo, raw):
    result, llm, _ = propose(context, repo, [proposal(raw), proposal(CORRECT)])
    llm.complete_structured.assert_awaited_once()
    assert not result.validation_result.valid and result.protocol_retry_count == 0
    event = result.protocol_events[0]
    assert event.raw_model_diff == raw and event.normalized_diff is None
    assert not event.transformations and event.recovery_rejection


def test_existing_envelope_retry_still_allowed_once(context, repo):
    result, llm, _ = propose(context, repo, [proposal("```diff\n" + CORRECT + "```\n"), proposal(WRONG_NEW)])
    assert llm.complete_structured.await_count == 2 and result.protocol_retry_count == 1
    # The retried response is itself recovered deterministically, with the same intent.
    assert result.validation_result.valid and result.proposal.unified_diff == CORRECT
    assert [t.category for t in result.protocol_events[1].transformations] == [COUNTS]


def test_recovery_is_not_a_repair_attempt(context, repo):
    _, _, (agent, diagnosis, repository) = propose(context, repo, [proposal(CORRECT)])
    agent._llm.complete_structured.side_effect = [
        CompletionResult(model="synthetic", content=proposal(WRONG_NEW).model_dump_json())]
    agent._llm.complete_structured.reset_mock()

    def verify(attempt, _):
        raw = SandboxResult(exit_code=0, stdout="passed", stderr="", duration_ms=1, command=["docker"])
        test = VerificationTests(command=raw.command, exit_code=0, passed=True, failed_tests=[],
            stdout_excerpt="passed", stderr_excerpt="", duration_ms=1, evidence=raw)
        return PatchVerification(proposal_attempt=attempt, applied=True, verified=True, status="verified",
            targeted_tests=test, regression_tests=test, original_unchanged=True, workspace_cleaned=True,
            changed_files=["mod.py"], diff_after_apply=attempt.proposal.unified_diff)

    verifier = Mock(verify=Mock(side_effect=verify))
    run = asyncio.run(RepairLoop(agent, verifier).run(diagnosis, repository))
    assert run.verified and len(run.attempts) == 1 and run.attempts[0].attempt_number == 1
    assert run.attempts[0].proposal_attempt.protocol_retry_count == 0
    assert run.attempts[0].proposal_attempt.proposal.unified_diff == CORRECT
    verifier.verify.assert_called_once()
    agent._llm.complete_structured.assert_awaited_once()


def test_historical_event_without_v3_fields_still_loads():
    from app.agent.state import PatchProtocolEvent
    event = PatchProtocolEvent.model_validate({"call_number": 1, "raw_response_sha256": "0" * 64,
        "raw_model_diff": "x", "normalization_reason": "source_proven_context_prefix",
        "validation": {"valid": True, "limits": {}}})
    assert event.transformations == [] and event.raw_validation is None and event.recovery_rejection is None
