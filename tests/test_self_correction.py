import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.agent.orchestrator import GhostFixOrchestrator
from app.agent.patches import validate_patch
from app.agent.repair import RepairLoop, correction_evidence
from app.agent.state import (
    ContextFile, Diagnosis, DiagnosisAttempt, DiagnosisInput, PatchLimits, PatchProposal,
    PatchProposalAttempt, PatchVerification, VerificationTests,
)
from app.agent.verification import PatchVerifier, fixture_snapshot
from app.llm.nebius_client import CompletionResult
from app.sandbox.models import SandboxResult
from app.tools.files import RepositoryContext


def execution(code=0):
    return SandboxResult(exit_code=code, stdout="assert 6 == 5", stderr="", command=["docker", "HOST_ONLY"], duration_ms=1)


@pytest.fixture
def case(tmp_path):
    files = [ContextFile(path="calculator.py", content="def add(a, b):\n    return a - b\n"),
             ContextFile(path="test_calculator.py", content="from calculator import add\ndef test_add(): assert add(2, 3) == 5\n")]
    for file in files:
        (tmp_path / file.path).write_bytes(file.content.encode())
    repository = RepositoryContext(tmp_path, ("test-secret",))
    diagnosis = DiagnosisAttempt(sandbox=execution(1), status="diagnosed",
        context=DiagnosisInput(exit_code=1, stdout_excerpt="assert -1 == 5", stderr_excerpt="", failed_tests=["test_add"], files=files),
        diagnosis=Diagnosis(failure_summary="wrong arithmetic", suspected_files=["calculator.py"],
            suspected_symbols=["add"], root_cause_hypothesis="subtracts", supporting_evidence=["-1 != 5"],
            confidence=0.9, recommended_next_action="inspect add"))
    def proposal(operator="+"):
        candidate = PatchProposal(target_files=["calculator.py"], summary="candidate", rationale="arithmetic",
            unified_diff="--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a " + operator + " b\n",
            expected_behavior_change="arithmetic", tests_to_run=["test_calculator.py::test_add"], risk_notes=[], confidence=0.5)
        return PatchProposalAttempt(diagnosis_attempt=diagnosis, proposal=candidate,
            validation_result=validate_patch(candidate, diagnosis.context, repository.root, PatchLimits()), model="mock")
    return repository, diagnosis, proposal


def verification(proposal, status):
    passed = status == "verified"
    targeted_passed = status in {"verified", "regression_failure"}
    def test(ok):
        return VerificationTests(command=["docker", "HOST_ONLY"], exit_code=0 if ok else 1,
            passed=ok, failed_tests=[] if ok else ["test_calculator.py::test_add"],
            stdout_excerpt="assert 6 == 5", stderr_excerpt="", duration_ms=1, evidence=execution(0 if ok else 1))
    return PatchVerification(proposal_attempt=proposal, applied=True, verified=passed, status=status,
        targeted_tests=test(targeted_passed), regression_tests=test(passed) if targeted_passed else None,
        workspace_cleaned=True, original_unchanged=True, changed_files=["calculator.py"])


@pytest.mark.parametrize("statuses,terminal", [
    (["verified"], "verified"),
    (["targeted_test_failure", "verified"], "verified"),
    (["regression_failure", "verified"], "verified"),
    (["targeted_test_failure", "targeted_test_failure"], "retry_exhausted"),
    (["regression_failure", "regression_failure"], "retry_exhausted"),
    (["infrastructure_failure"], "infrastructure_failure"),
    (["timeout"], "timeout"), (["application_failure"], "application_failure"),
])
def test_policy_and_attempt_chain(case, statuses, terminal):
    repository, diagnosis, candidate = case
    proposals = [candidate("*"), candidate("+")]
    agent = Mock(propose_patch=AsyncMock(side_effect=proposals))
    verifier = Mock(verify=Mock(side_effect=[verification(proposals[i], status) for i, status in enumerate(statuses)]))
    result = asyncio.run(RepairLoop(agent, verifier).run(diagnosis, repository))
    assert result.status == terminal and result.verified == (terminal == "verified")
    assert len(result.attempts) == len(statuses)
    assert agent.propose_patch.await_count == verifier.verify.call_count == len(statuses)
    assert agent.propose_patch.call_args_list[0].kwargs == {}
    agent.analyze.assert_not_called()
    for call in verifier.verify.call_args_list:
        assert call.args[1] is repository
    if len(statuses) == 2:
        correction = agent.propose_patch.call_args_list[1].kwargs["correction"]
        assert correction.verification_status == statuses[0]
        assert "6 == 5" in correction.targeted_tests.stdout_excerpt
        assert correction.previous_proposal == proposals[0].proposal
        assert result.attempts[1].correction_evidence == correction
    assert result.completed_at


def test_invalid_second_proposal_never_verified(case):
    repository, diagnosis, candidate = case
    first, second = candidate("*"), candidate("+")
    second.validation_result.valid = False
    second.validation_result.errors = ["Malformed diff"]
    agent = Mock(propose_patch=AsyncMock(side_effect=[first, second]))
    verifier = Mock(verify=Mock(return_value=verification(first, "targeted_test_failure")))
    result = asyncio.run(RepairLoop(agent, verifier).run(diagnosis, repository))
    assert result.status == "proposal_failure" and len(result.attempts) == 2
    assert result.attempts[1].proposal_attempt == second
    assert result.attempts[1].verification is None
    verifier.verify.assert_called_once()


@pytest.mark.parametrize("flag", ["workspace_cleaned", "original_unchanged"])
def test_cleanup_or_integrity_failure_is_terminal(case, flag):
    repository, diagnosis, candidate = case
    first = candidate()
    observed = verification(first, "targeted_test_failure")
    setattr(observed, flag, False)
    agent = Mock(propose_patch=AsyncMock(return_value=first))
    result = asyncio.run(RepairLoop(agent, Mock(verify=Mock(return_value=observed))).run(diagnosis, repository))
    assert result.status == "infrastructure_failure"
    agent.propose_patch.assert_awaited_once()


@pytest.mark.parametrize("budget", [0, -1, 3, 99999, True, "2", 1.5])
def test_budget_is_strictly_bounded(case, budget):
    repository, diagnosis, _ = case
    agent, verifier = Mock(), Mock()
    with pytest.raises(ValidationError):
        asyncio.run(RepairLoop(agent, verifier).run(diagnosis, repository, max_attempts=budget))
    agent.propose_patch.assert_not_called()
    verifier.verify.assert_not_called()


def test_one_attempt_budget(case):
    repository, diagnosis, candidate = case
    first = candidate()
    agent = Mock(propose_patch=AsyncMock(return_value=first))
    result = asyncio.run(RepairLoop(agent, Mock(verify=Mock(return_value=verification(first, "targeted_test_failure"))))
                         .run(diagnosis, repository, max_attempts=1))
    assert result.status == "retry_exhausted" and len(result.attempts) == 1


def test_correction_request_bounded_redacted_and_multiline(case):
    repository, diagnosis, candidate = case
    first = candidate("*")
    failed = verification(first, "targeted_test_failure")
    failed.targeted_tests.stdout_excerpt = "test-secret C:\\Users\\someone\\file /tmp/private/file " + "x" * 5000
    failed.targeted_tests.failed_tests = ["x" * 1000] * 30
    first.proposal.rationale = "test-secret"
    correction = correction_evidence(failed, repository)
    llm = Mock(complete_structured=AsyncMock(return_value=CompletionResult(model="mock", content=candidate().proposal.model_dump_json())))
    agent = GhostFixOrchestrator(Mock(), llm)
    result = asyncio.run(agent.propose_patch(diagnosis, repository, correction=correction))
    assert result.validation_result.valid
    messages = llm.complete_structured.call_args.args[0]
    assert "NOT verified" in messages[0]["content"]
    payload = messages[1]["content"]
    for forbidden in ("test-secret", "HOST_ONLY", "someone", "/tmp/private"):
        assert forbidden not in payload
    evidence = json.loads(payload)["correction_evidence"]
    assert len(evidence["targeted_tests"]["stdout_excerpt"]) <= 2000
    assert len(evidence["targeted_tests"]["failed_tests"]) == 10
    assert len(payload.encode()) <= 60000
    assert llm.complete_structured.call_args.kwargs == {"multiline": True}


def test_real_verifier_uses_fresh_copies_and_preserves_original(case):
    repository, diagnosis, candidate = case
    original = fixture_snapshot(repository)
    workspaces = []
    def sandbox_run(root, selectors):
        workspaces.append(root)
        assert root != repository.root
        content = (root / "calculator.py").read_text()
        # Observe text only, never execute fixture code on the host.
        return execution(1 if "a * b" in content else 0)
    sandbox = Mock(run=sandbox_run)
    agent = Mock(propose_patch=AsyncMock(side_effect=[candidate("*"), candidate("+")]))
    result = asyncio.run(RepairLoop(agent, PatchVerifier(sandbox)).run(diagnosis, repository))
    assert result.verified and len(result.attempts) == 2
    assert workspaces[0] != workspaces[1] and workspaces[1] == workspaces[2]
    assert all(not root.exists() for root in workspaces)
    assert fixture_snapshot(repository) == original


def test_invalid_model_json_on_correction_is_terminal(case):
    repository, diagnosis, candidate = case
    llm = Mock(complete_structured=AsyncMock(side_effect=[
        CompletionResult(model="mock", content=candidate("*").proposal.model_dump_json()),
        CompletionResult(model="mock", content="not JSON"),
    ]))
    agent = GhostFixOrchestrator(Mock(), llm)
    verifier = Mock(verify=Mock(side_effect=lambda proposal, root: verification(proposal, "targeted_test_failure")))
    result = asyncio.run(RepairLoop(agent, verifier).run(diagnosis, repository))
    assert result.status == "proposal_failure" and len(result.attempts) == 2
    assert result.attempts[1].proposal_attempt.error
    assert result.attempts[1].verification is None
    verifier.verify.assert_called_once()
    assert llm.complete_structured.await_count == 2


def test_detects_original_mutation_even_if_verifier_flags_are_wrong(case):
    repository, diagnosis, candidate = case
    first = candidate("*")
    def verify(proposal, root):
        # Simulate an external host writer, not real verifier behavior.
        (root.root / "calculator.py").write_bytes(b"externally changed\n")
        return verification(proposal, "targeted_test_failure")
    agent = Mock(propose_patch=AsyncMock(return_value=first))
    result = asyncio.run(RepairLoop(agent, Mock(verify=verify)).run(diagnosis, repository))
    assert result.status == "infrastructure_failure"
    agent.propose_patch.assert_awaited_once()


def test_inconsistent_retry_evidence_is_terminal(case):
    repository, diagnosis, candidate = case
    first = candidate("*")
    observed = verification(first, "targeted_test_failure")
    observed.targeted_tests.exit_code = 125
    agent = Mock(propose_patch=AsyncMock(return_value=first))
    result = asyncio.run(RepairLoop(agent, Mock(verify=Mock(return_value=observed))).run(diagnosis, repository))
    assert result.status == "infrastructure_failure"
    agent.propose_patch.assert_awaited_once()
