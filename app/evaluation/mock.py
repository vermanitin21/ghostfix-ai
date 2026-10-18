"""Explicitly synthetic evidence for infrastructure demos, never a real repair claim."""
from datetime import datetime, timezone

from app.agent.state import (
    ContextFile, Diagnosis, DiagnosisAttempt, DiagnosisInput, PatchLimits, PatchProposal,
    PatchProposalAttempt, PatchValidationResult, PatchVerification, RepairAttempt, RepairRun, VerificationTests,
)
from app.sandbox.models import SandboxResult

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)
SELECTOR = "test_calculator.py::test_adds_two_numbers"
DIFF = "--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,2 @@\n def add(left, right):\n-    return left - right\n+    return left + right\n"


def synthetic_run(outcomes: tuple[str, ...] = ("verified",), *, case_id: str = "synthetic") -> RepairRun:
    """Build deterministic in-memory evidence; no repository reads or execution."""
    original = SandboxResult(exit_code=1, stdout="assert -1 == 5", stderr="", duration_ms=10,
                             command=["python", "-m", "pytest", "-q", "."], failed_tests=[SELECTOR])
    context = DiagnosisInput(exit_code=1, failed_tests=[SELECTOR], stdout_excerpt=original.stdout, stderr_excerpt="",
        files=[ContextFile(path="calculator.py", content="def add(left, right):\n    return left - right\n"),
               ContextFile(path="test_calculator.py", content="from calculator import add\ndef test_adds_two_numbers():\n    assert add(2, 3) == 5\n")])
    diagnosis = DiagnosisAttempt(attempt_id="mock-diagnosis", created_at=NOW, sandbox=original, context=context,
        status="diagnosed", model="mock:synthetic-not-a-model-call", diagnosis=Diagnosis(
            failure_summary="Synthetic arithmetic failure", suspected_files=["calculator.py"], suspected_symbols=["add"],
            root_cause_hypothesis="Subtraction instead of addition", supporting_evidence=["-1 != 5"],
            confidence=0.5, recommended_next_action="Validate candidate"))
    def test(selector, passed, duration):
        raw = SandboxResult(exit_code=0 if passed else 1, stdout="synthetic test evidence", stderr="", duration_ms=duration,
            command=["python", "-m", "pytest", "-q", selector], failed_tests=[] if passed else [SELECTOR])
        return VerificationTests(command=raw.command, exit_code=raw.exit_code, passed=passed, failed_tests=raw.failed_tests,
            stdout_excerpt=raw.stdout, stderr_excerpt="", duration_ms=duration, evidence=raw)
    attempts = []
    for number, outcome in enumerate(outcomes, 1):
        diff = DIFF if outcome == "verified" else DIFF.replace("left + right", "left * right")
        proposal = PatchProposal(target_files=["calculator.py"], summary="Synthetic candidate", rationale="Infrastructure test only",
            unified_diff=diff, expected_behavior_change="Arithmetic correction", tests_to_run=[SELECTOR], risk_notes=[], confidence=0.5)
        candidate = PatchProposalAttempt(attempt_id=f"mock-proposal-{number}", created_at=NOW, completed_at=NOW,
            diagnosis_attempt=diagnosis, proposal=proposal, model="mock:synthetic-not-a-model-call",
            validation_result=PatchValidationResult(valid=True, limits=PatchLimits(), modified_files=["calculator.py"], changed_lines=2))
        verification = PatchVerification(proposal_attempt=candidate, applied=True, verified=outcome == "verified", status=outcome,
            targeted_tests=test(SELECTOR, outcome != "targeted_test_failure", 20),
            regression_tests=None if outcome == "targeted_test_failure" else test(".", outcome == "verified", 30),
            changed_files=["calculator.py"], diff_after_apply=diff, original_unchanged=True, workspace_cleaned=True)
        attempts.append(RepairAttempt(attempt_number=number, proposal_attempt=candidate, verification=verification, outcome=outcome))
    return RepairRun(run_id=f"mock-{case_id}", created_at=NOW, completed_at=NOW, initial_diagnosis=diagnosis,
        attempts=attempts, final_verification=attempts[-1].verification, verified=outcomes[-1] == "verified",
        status="verified" if outcomes[-1] == "verified" else "retry_exhausted", max_attempts=len(outcomes))
