"""Pure evidence projection. A supplied report can never override fresh evidence."""
from app.agent.state import RepairRun
from app.reporting import build_proof_of_fix
from app.reporting.models import ProofOfFix

from .models import BenchmarkCase, BenchmarkCaseResult, FailureCategory as F
from .safety import safe_text


def evaluate_case(case: BenchmarkCase, repair_run: RepairRun, proof: ProofOfFix | None = None,
                  *, secrets: tuple[str, ...] = ()) -> BenchmarkCaseResult:
    # Deliberately rebuild even if a caller supplies a stale/forged ProofOfFix.
    normalized = build_proof_of_fix(repair_run, secrets=secrets, generated_at=repair_run.created_at)
    run = repair_run
    observations = [a.verification for a in run.attempts if a.verification is not None]
    raw_tests = [t.evidence for v in observations for t in (v.targeted_tests, v.regression_tests) if t]
    original = run.initial_diagnosis.sandbox
    issues = list(normalized.verification.evidence_issues)
    # Missing final verification is normal for failures before candidate execution.
    early = not observations and not run.verified and run.status != "verified"
    expected_issues = set()
    last = run.attempts[-1] if run.attempts else None
    # RepairLoop retains the previous executed verification if correction generation fails.
    invalid_last = bool(last and last.verification is None and last.outcome == "proposal_failure"
        and (last.proposal_attempt.proposal is None or last.proposal_attempt.error
             or not last.proposal_attempt.validation_result or not last.proposal_attempt.validation_result.valid))
    retained_previous = bool(invalid_last and len(run.attempts) > 1 and run.final_verification is not None
                             and run.final_verification == run.attempts[-2].verification)
    if retained_previous and not run.verified and run.status == "proposal_failure":
        expected_issues.add("Final verification is not linked to the final attempt's candidate.")
    if early:
        expected_issues.add("Final verification evidence is unavailable.")
    if not normalized.reproduction.reproduced and early:
        expected_issues.add("Original test failure was not reproduced successfully.")
    contradictions = [i for i in issues if i not in expected_issues
                      and "integrity or cleanup did not pass" not in i]
    if normalized.reproduction.reproduced and not set(case.expected_failing_tests).issubset(original.failed_tests):
        contradictions.append("Reproduction evidence does not identify the expected failing tests for this case.")
    if run.initial_diagnosis.diagnosis is None or run.initial_diagnosis.status != "diagnosed":
        if run.attempts:
            contradictions.append("Candidate attempts lack a successful diagnosis.")
    if run.attempts and not observations and any(a.outcome != "proposal_failure" for a in run.attempts):
        contradictions.append("Attempt outcome lacks verification evidence.")
    final = run.final_verification
    if final and not run.verified:
        stage = final.targeted_tests if final.status == "targeted_test_failure" else final.regression_tests
        if final.status in {"targeted_test_failure", "regression_failure"}:
            if (not final.applied or stage is None or stage.passed or stage.evidence.exit_code != 1
                    or (final.status == "regression_failure" and
                        (final.targeted_tests is None or not final.targeted_tests.passed))):
                contradictions.append("Failure status contradicts executed test evidence.")
    integrity = normalized.integrity.original_repository_unchanged
    cleanup = normalized.integrity.workspace_cleaned
    # Specific safety failures outrank generic contradictions. See docs/evaluation.md.
    if integrity is False:
        category = F.INTEGRITY_FAILURE
    elif cleanup is False:
        category = F.CLEANUP_FAILURE
    elif original.timed_out or any(t.timed_out for t in raw_tests) or run.status == "timeout":
        category = F.TIMEOUT
    elif original.infrastructure_error or any(t.infrastructure_error for t in raw_tests) or run.status == "infrastructure_failure":
        category = F.INFRASTRUCTURE_FAILURE
    elif contradictions:
        category = F.INCOMPLETE_EVIDENCE
    elif not normalized.reproduction.reproduced:
        category = F.REPRODUCTION_FAILURE
    elif run.initial_diagnosis.status != "diagnosed" or run.initial_diagnosis.diagnosis is None:
        category = F.DIAGNOSIS_FAILURE
    elif normalized.verified:
        category = F.NONE
    elif run.attempts and (run.attempts[-1].proposal_attempt.proposal is None
            or run.attempts[-1].proposal_attempt.error
            or not run.attempts[-1].proposal_attempt.validation_result
            or not run.attempts[-1].proposal_attempt.validation_result.valid):
        category = F.INVALID_PROPOSAL
    elif final:
        category = {"application_failure": F.PATCH_APPLICATION_FAILURE,
                    "targeted_test_failure": F.TARGETED_TEST_FAILURE,
                    "regression_failure": F.REGRESSION_FAILURE,
                    "timeout": F.TIMEOUT, "infrastructure_failure": F.INFRASTRUCTURE_FAILURE}.get(final.status, F.UNKNOWN)
    elif run.status == "retry_exhausted":
        category = F.RETRY_BUDGET_EXHAUSTED
    else:
        category = F.INCOMPLETE_EVIDENCE
    verified = normalized.verified and category == F.NONE
    status = "INCOMPLETE" if category in {F.INCOMPLETE_EVIDENCE, F.UNKNOWN} or run.completed_at is None else "COMPLETED"
    if status == "INCOMPLETE":
        verified = False
    def passed(test):
        return test.passed if test and test.consistent else None
    def duration(attribute):
        values = [getattr(v, attribute).evidence.duration_ms for v in observations if getattr(v, attribute)]
        return sum(values) if values else None
    target_ms, regression_ms = duration("targeted_tests"), duration("regression_tests")
    patch = normalized.final_patch if verified else None
    count = len(run.attempts)
    current_final = bool(last and final and last.verification == final)
    return BenchmarkCaseResult(case_id=safe_text(case.id, secrets), case_name=safe_text(case.name, secrets), status=status,
        verified=verified, attempt_count=count, first_attempt_success=verified and count == 1,
        self_correction_used=count > 1, self_correction_recovered=verified and count > 1,
        targeted_passed=passed(normalized.verification.targeted) if current_final else None,
        regression_passed=passed(normalized.verification.regression) if current_final else None,
        integrity_passed=integrity, cleanup_passed=cleanup, failure_category=category,
        retry_budget_exhausted=run.status == "retry_exhausted",
        changed_file_count=len(patch.changed_files) if patch else None,
        additions=patch.additions if patch else None, deletions=patch.deletions if patch else None,
        patch_size=patch.additions + patch.deletions if patch else None,
        reproduction_duration_ms=original.duration_ms, targeted_duration_ms=target_ms,
        regression_duration_ms=regression_ms,
        total_verification_duration_ms=sum(t.duration_ms for t in raw_tests) if raw_tests else None,
        runtime_models=sorted({safe_text(m, secrets) for m in normalized.provenance.runtime_models}),
        evidence_run_id=safe_text(run.run_id, secrets), proof_status=normalized.status,
        attempt_statuses=[a.verification_status for a in normalized.repair_trajectory],
        evidence_issues=list(dict.fromkeys(issues + contradictions)))
