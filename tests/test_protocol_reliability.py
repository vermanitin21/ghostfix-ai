"""Synthetic protocol contracts only. No frozen fixture or reference solutions."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent.orchestrator import GhostFixOrchestrator
from app.agent.patches import patched_contents, validate_patch
from app.agent.prompts import PATCH_SYSTEM, CORRECTION_SYSTEM, FORMAT_RETRY_SYSTEM
from app.agent.protocol import format_retry_witness
from app.agent.recovery import recover_representation
from app.agent.repair import RepairLoop
from app.agent.state import (
    ContextFile, Diagnosis, ConciseDiagnosis, DiagnosisAttempt, DiagnosisInput, PatchLimits,
    PatchProposal, PatchVerification, VerificationTests,
)
from app.llm.nebius_client import CompletionResult, OutputBudgetExceeded, StructuredOutputError
from app.sandbox.models import SandboxResult
from app.tools.files import RepositoryContext

SOURCE = "def value():\n    return 1\n"
DIFF = "--- a/utility.py\n+++ b/utility.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2\n"
MISSING = DIFF.replace("\n def value():", "\ndef value():")
FENCED = "```diff\n" + DIFF + "```\n"
PAYLOAD = dict(failure_summary="Unexpected value", suspected_files=["utility.py"],
    suspected_symbols=["value"], root_cause_hypothesis="Unexpected returned value",
    supporting_evidence=["Observed 1, expected 2"], confidence=0.5, recommended_next_action="Inspect value")


def normalize_context_prefixes(candidate, context, root, limits):
    """The v3 pipeline seen through the v2 (candidate, validation, normalized) contract."""
    result = recover_representation(candidate, context, root, limits)
    return result.candidate, result.validation, bool(result.transformations)


def proposal(diff=DIFF, target="utility.py"):
    return PatchProposal(target_files=[target], summary="Return specified value", rationale="Observed mismatch",
        unified_diff=diff, expected_behavior_change="Return specified value", tests_to_run=["test_utility.py::test_value"],
        risk_notes=[], confidence=0.5)


@pytest.fixture
def setup(tmp_path):
    context = DiagnosisInput(exit_code=1, failed_tests=["test_utility.py::test_value"], stdout_excerpt="original failure",
        stderr_excerpt="", files=[ContextFile(path="utility.py", content=SOURCE),
        ContextFile(path="test_utility.py", content="def test_value():\n    assert value() == 2\n")])
    for file in context.files:
        (tmp_path / file.path).write_text(file.content, encoding="utf-8", newline="")
    sandbox = SandboxResult(exit_code=1, stdout="original failure", stderr="", duration_ms=1, command=["docker"])
    diagnosis = DiagnosisAttempt(sandbox=sandbox, context=context, diagnosis=Diagnosis(**PAYLOAD), status="diagnosed")
    return context, RepositoryContext(tmp_path, ("sample-private-value",)), diagnosis


def call(setup, responses, *, correction=None):
    context, repository, diagnosis = setup
    llm = Mock(complete_structured=AsyncMock(side_effect=[
        item if isinstance(item, Exception) else CompletionResult(model="synthetic", content=item.model_dump_json())
        for item in responses]))
    sandbox = Mock()
    agent = GhostFixOrchestrator(sandbox, llm)
    result = asyncio.run(agent.propose_patch(diagnosis, repository, correction=correction))
    sandbox.run.assert_not_called()
    return result, llm, agent


def test_exact_prefix_normalization_preserves_effect_and_provenance(setup, monkeypatch):
    import app.agent.patches as parser
    context, repository, _ = setup
    unchanged = copy.deepcopy(context.model_dump())
    strict = Mock(wraps=parser.validate_patch)
    monkeypatch.setattr(parser, "validate_patch", strict)
    candidate, valid, normalized = normalize_context_prefixes(proposal(MISSING), context, repository.root, PatchLimits())
    assert normalized and valid.valid and candidate.unified_diff == DIFF
    assert strict.call_count >= 2
    assert patched_contents(candidate, context, repository.root, PatchLimits()) == {"utility.py": "def value():\n    return 2\n"}
    assert context.model_dump() == unchanged
    result, llm, _ = call(setup, [proposal(MISSING)])
    assert result.proposal.unified_diff == DIFF and result.protocol_retry_count == 0
    event = result.protocol_events[0]
    assert event.raw_model_diff == MISSING and event.normalized_diff == DIFF
    assert event.normalization_reason == "source_proven_context_prefix"
    assert len(event.raw_response_sha256) == 64
    llm.complete_structured.assert_awaited_once()


@pytest.mark.parametrize("diff", [
    MISSING.replace("def value():", "def other():"),
    DIFF.replace(" def value():", " def other():"),
    MISSING.replace("-    return 1\n", ""),
    MISSING.replace("+    return 2\n", ""),
    MISSING.replace("@@ -1,2 +1,2 @@", "@@ -2,2 +2,2 @@"),
    MISSING.replace("    return 1", "    return 8"),
    MISSING.replace("+1,2", "+1,3"),
    MISSING + "\\ No newline at end of file\n",
    MISSING.replace("def value():", " def value():", 1).replace("-    return 1", "    return 1"),
])
def test_ambiguous_or_incomplete_context_rejected(setup, diff):
    context, repository, _ = setup
    candidate, valid, normalized = normalize_context_prefixes(proposal(diff), context, repository.root, PatchLimits())
    assert not valid.valid and not normalized and candidate.unified_diff == diff
    assert format_retry_witness(proposal(diff), context, repository.root, PatchLimits()) is None
    result, llm, _ = call(setup, [proposal(diff)])
    assert not result.validation_result.valid and result.protocol_retry_count == 0
    llm.complete_structured.assert_awaited_once()


def test_duplicate_lines_use_exact_coordinates_not_search(setup):
    context, repository, _ = setup
    context.files[0].content = "x = 1\nx = 1\ny = 3\n"
    diff = "--- a/utility.py\n+++ b/utility.py\n@@ -2,2 +2,2 @@\nx = 1\n-y = 3\n+y = 4\n"
    candidate, valid, normalized = normalize_context_prefixes(proposal(diff), context, repository.root, PatchLimits())
    assert valid.valid and normalized
    assert patched_contents(candidate, context, repository.root, PatchLimits())["utility.py"] == "x = 1\nx = 1\ny = 4\n"
    wrong = diff.replace("-2,2 +2,2", "-1,2 +1,2")
    assert not normalize_context_prefixes(proposal(wrong), context, repository.root, PatchLimits())[1].valid


def test_normalization_obeys_limits_and_paths(setup):
    context, repository, _ = setup
    for limits in (PatchLimits(max_changed_lines=1), PatchLimits(max_diff_bytes=10)):
        assert not normalize_context_prefixes(proposal(MISSING), context, repository.root, limits)[1].valid


@pytest.mark.parametrize("kind", ["traversal", "absolute", "secret", "test", "unsupported", "creation", "deletion", "rename", "binary"])
def test_policy_violations_never_retried(setup, kind):
    targets = {"traversal": "../utility.py", "absolute": "/utility.py", "secret": ".env",
               "test": "test_utility.py", "unsupported": "setup.py", "rename": "utility.py"}
    target = targets.get(kind, "utility.py")
    diff = DIFF.replace("utility.py", target) if target != "utility.py" else DIFF
    if kind == "creation": diff = diff.replace("--- a/utility.py", "--- /dev/null")
    if kind == "deletion": diff = diff.replace("+++ b/utility.py", "+++ /dev/null")
    if kind == "rename": diff = diff.replace("+++ b/utility.py", "+++ b/other.py")
    if kind == "binary": diff += "\x00"
    # An outer format defect must never hide a security violation later in the diff.
    result, llm, _ = call(setup, [proposal("```diff\n" + diff + "```\n", target)])
    assert not result.validation_result.valid and result.protocol_retry_count == 0
    llm.complete_structured.assert_awaited_once()


def test_missing_final_lf_recovered_without_model_retry(setup):
    result, llm, _ = call(setup, [proposal(DIFF.rstrip("\n"))])
    assert result.validation_result.valid and result.protocol_retry_count == 0
    assert result.proposal.unified_diff == DIFF
    llm.complete_structured.assert_awaited_once()
    event = result.protocol_events[0]
    assert event.raw_model_diff == DIFF.rstrip("\n") and event.normalized_diff == DIFF
    assert [t.category for t in event.transformations] == ["TERMINAL_NEWLINE_CANONICALIZATION"]
    assert event.normalization_reason == "deterministic_representation_recovery"


@pytest.mark.parametrize("diff", [FENCED])
def test_one_format_retry_and_same_intent(setup, diff):
    result, llm, _ = call(setup, [proposal(diff), proposal()])
    assert result.validation_result.valid and result.protocol_retry_count == 1
    assert llm.complete_structured.await_count == 2
    assert [e.raw_model_diff for e in result.protocol_events] == [diff, DIFF]
    assert result.protocol_events[0].validation.failure_category == "RETRYABLE_FORMAT_ERROR"
    retry = json.loads(llm.complete_structured.call_args.args[0][1]["content"])
    assert set(retry) == {"previous_proposal", "validation_feedback", "original_files", "limits"}
    assert len(json.dumps(retry).encode()) < 60000
    assert len(retry["validation_feedback"][0]) < 200
    assert "correction_evidence" not in retry and "evidence_and_context" not in retry


def test_second_bad_format_exhausts_protocol_budget(setup):
    result, llm, _ = call(setup, [proposal(FENCED), proposal(FENCED)])
    assert not result.validation_result.valid and result.protocol_retry_count == 1
    assert llm.complete_structured.await_count == 2
    assert len(result.protocol_events) == 2


def test_format_retry_cannot_change_semantics_or_tests(setup):
    for second in (proposal(DIFF.replace("return 2", "return 9")),
                   proposal().model_copy(update={"tests_to_run": ["test_utility.py"]})):
        result, llm, _ = call(setup, [proposal(FENCED), second])
        assert not result.validation_result.valid
        assert result.validation_result.failure_category == "SEMANTICALLY_AMBIGUOUS_PATCH"
        assert llm.complete_structured.await_count == 2


def test_valid_first_response_unchanged(setup):
    result, llm, _ = call(setup, [proposal()])
    assert result.proposal == proposal() and result.validation_result.valid
    assert result.protocol_retry_count == 0 and result.protocol_events[0].normalized_diff is None


def test_no_retry_for_unrelated_or_schema_errors(setup):
    for error in (RuntimeError("private detail"), StructuredOutputError("Not a budget error")):
        result, llm, _ = call(setup, [error])
        assert not result.validation_result.valid and result.protocol_retry_count == 0
        llm.complete_structured.assert_awaited_once()


def test_protocol_provenance_redacts_secrets_and_paths(setup):
    candidate = proposal(DIFF.replace("return 2", "return 'sample-private-value'"))
    result, llm, _ = call(setup, [candidate])
    assert result.validation_result.failure_category == "SECURITY_POLICY_ERROR"
    assert "sample-private-value" not in result.model_dump_json()
    llm.complete_structured.assert_awaited_once()
    result, _, _ = call(setup, [proposal(DIFF.replace("utility.py", "C:/Users/private.py"), "C:/Users/private.py")])
    assert "C:/Users" not in result.model_dump_json()


@pytest.mark.parametrize("errors,count,success", [
    ([], 1, True),
    ([OutputBudgetExceeded("Structured completion exceeded the output token budget")], 2, True),
    ([OutputBudgetExceeded("budget"), OutputBudgetExceeded("budget")], 2, False),
    ([StructuredOutputError("Structured completion exceeded the output token budget")], 1, False),
    ([RuntimeError("sample-private-value")], 1, False),
])
def test_diagnosis_retry_only_explicit_budget(setup, errors, count, success):
    context, repository, diagnosis = setup
    llm = Mock(complete_structured=AsyncMock(side_effect=[*errors, CompletionResult(model="synthetic", content=json.dumps(PAYLOAD))]))
    sandbox = Mock(run=Mock(return_value=diagnosis.sandbox))
    result = asyncio.run(GhostFixOrchestrator(sandbox, llm).analyze(repository, ["utility.py", "test_utility.py"]))
    assert (result.status == "diagnosed") == success
    assert llm.complete_structured.await_count == count
    assert result.protocol_retry_count == count - 1
    assert len(result.protocol_events) == count
    sandbox.run.assert_called_once()
    if count == 2:
        first, second = [c.args[0] for c in llm.complete_structured.call_args_list]
        assert first[1]["content"] == second[1]["content"]
        assert "SAME evidence" in second[0]["content"]
        assert "chain-of-thought" not in second[0]["content"]
    assert "sample-private-value" not in result.model_dump_json()


def test_wire_diagnosis_is_concise_but_archival_schema_compatible():
    with pytest.raises(ValueError):
        ConciseDiagnosis(**{**PAYLOAD, "root_cause_hypothesis": "x" * 401})
    assert Diagnosis(**{**PAYLOAD, "root_cause_hypothesis": "x" * 401})


@pytest.mark.parametrize("diagnosis_retry", [False, True])
def test_protocol_retry_stays_inside_one_repair_attempt(setup, diagnosis_retry):
    context, repository, diagnosis = setup
    responses = [CompletionResult(model="synthetic", content=p.model_dump_json()) for p in (proposal(FENCED), proposal())]
    llm = Mock(complete_structured=AsyncMock(side_effect=responses))
    agent = GhostFixOrchestrator(Mock(run=Mock(return_value=diagnosis.sandbox)), llm)
    if diagnosis_retry:
        llm.complete_structured.side_effect = [
            OutputBudgetExceeded("Structured completion exceeded the output token budget"),
            CompletionResult(model="synthetic", content=json.dumps(PAYLOAD)), *responses]
        diagnosis = asyncio.run(agent.analyze(repository, ["utility.py", "test_utility.py"]))
        assert diagnosis.protocol_retry_count == 1
    def verify(attempt, repo):
        raw = SandboxResult(exit_code=0, stdout="passed", stderr="", duration_ms=1, command=["docker"])
        test = VerificationTests(command=raw.command, exit_code=0, passed=True, failed_tests=[],
            stdout_excerpt="passed", stderr_excerpt="", duration_ms=1, evidence=raw)
        return PatchVerification(proposal_attempt=attempt, applied=True, verified=True, status="verified",
            targeted_tests=test, regression_tests=test, original_unchanged=True, workspace_cleaned=True,
            changed_files=["utility.py"], diff_after_apply=DIFF)
    verifier = Mock(verify=Mock(side_effect=verify))
    run = asyncio.run(RepairLoop(agent, verifier).run(diagnosis, repository))
    assert run.verified and len(run.attempts) == 1
    assert run.attempts[0].proposal_attempt.protocol_retry_count == 1
    verifier.verify.assert_called_once()
    assert llm.complete_structured.await_count == (4 if diagnosis_retry else 2)


def test_later_unsafe_header_cannot_hide_behind_missing_prefix(setup):
    diff = MISSING + DIFF.replace("utility.py", "../escape.py")
    result, llm, _ = call(setup, [proposal(diff)])
    assert not result.validation_result.valid and result.protocol_retry_count == 0
    assert result.protocol_events[0].normalization_reason is None
    llm.complete_structured.assert_awaited_once()


def test_multiple_hunks_normalize_at_each_original_cursor(setup):
    context, repository, _ = setup
    context.files[0].content = "a = 1\nb = 2\nc = 3\nd = 4\n"
    diff = ("--- a/utility.py\n+++ b/utility.py\n@@ -1,2 +1,2 @@\na = 1\n-b = 2\n+b = 5\n"
            "@@ -3,2 +3,2 @@\nc = 3\n-d = 4\n+d = 6\n")
    candidate, validation, normalized = normalize_context_prefixes(proposal(diff), context, repository.root, PatchLimits())
    assert normalized and validation.valid
    assert patched_contents(candidate, context, repository.root, PatchLimits())["utility.py"] == "a = 1\nb = 5\nc = 3\nd = 6\n"


@pytest.mark.parametrize("prompt", [PATCH_SYSTEM, CORRECTION_SYSTEM, FORMAT_RETRY_SYSTEM])
def test_generic_diff_grammar_contract(prompt):
    assert '" " unchanged context' in prompt and '"+" addition' in prompt and '"-" removal' in prompt
    assert "\n def value():\n" in prompt and "REQUIRED" in prompt
    assert "No test modifications" in prompt and "Markdown fences" in prompt
    assert all(word not in prompt for word in ("calculator.py", "active_users", "reference_fixes", "order_total"))
