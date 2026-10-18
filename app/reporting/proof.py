"""Pure report projection: no network, shell, file IO, code execution or test reruns."""
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from app.agent.patches import patched_contents
from app.agent.state import RepairRun, VerificationTests
from app.reporting.models import (
    ProofAttempt, ProofDiagnosis, ProofIntegrity, ProofOfFix, ProofPatch,
    ProofProvenance, ProofReproduction, ProofTest, ProofVerification,
)
from app.tools.files import redact


def build_proof_of_fix(run: RepairRun, *, secrets: tuple[str, ...] = (),
                       generated_at: datetime | None = None) -> ProofOfFix:
    """Build from a trusted internal RepairRun, never model-supplied report fields.

    Pass configured credentials for redaction. Unknown secrets cannot be detected
    universally. External demo hash checks/digests are not in RepairRun and stay null.
    """
    def clean(text: str | None, limit: int = 2000) -> str | None:
        if text is None:
            return None
        text = redact(text, secrets)
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        text = re.sub(r"[A-Za-z]:[\\/][^\s\"'<>]+|\\\\[^\s\"'<>]+", "[host-path]", text)
        text = re.sub(r"(?<![\w.])/(?!/)[A-Za-z0-9_.-]+(?:/[^\s\"'<>]*)?", "[host-path]", text)
        text = "".join(char for char in text if char in "\n\t" or (ord(char) >= 32 and ord(char) != 127))
        return text if len(text) <= limit else text[:limit] + "\n[truncated]"

    def strings(values: list[str], count: int = 16) -> list[str]:
        return [clean(value) for value in values[:count]]

    def test(value: VerificationTests | None) -> ProofTest | None:
        if value is None:
            return None
        raw = value.evidence
        consistent = (value.exit_code == raw.exit_code and value.passed == raw.passed
                      and value.failed_tests == raw.failed_tests and value.duration_ms == raw.duration_ms
                      and value.command == raw.command and not (raw.passed and raw.failed_tests))
        # Expose only the pytest tail if every argument is safe and relative.
        argv = None
        if "-m" in raw.command:
            index = raw.command.index("-m")
            tail = raw.command[index + 1:]
            if tail and tail[0] == "pytest":
                safe = [arg for arg in tail[1:] if not arg.startswith("--basetemp=")]
                if all(re.fullmatch(r"[A-Za-z0-9_.:/=-]+", arg) and not arg.startswith("/")
                       and not re.match(r"[A-Za-z]:", arg) and ".." not in arg for arg in safe):
                    argv = ["python", "-m", "pytest", *[clean(arg) for arg in safe]]
        return ProofTest(passed=consistent and raw.passed, consistent=consistent, pytest_argv=argv,
            exit_code=raw.exit_code, failed_tests=strings(raw.failed_tests, 10),
            stdout_excerpt=clean(raw.stdout), stderr_excerpt=clean(raw.stderr), duration_ms=raw.duration_ms,
            timed_out=raw.timed_out, infrastructure_error=clean(raw.infrastructure_error))

    reproduction = run.initial_diagnosis.sandbox
    reproduced = reproduction.exit_code == 1 and not reproduction.timed_out and not reproduction.infrastructure_error
    trajectory = []
    models = []
    if run.initial_diagnosis.model:
        models.append(clean(run.initial_diagnosis.model))
    for item in run.attempts:
        proposal_attempt = item.proposal_attempt
        candidate = proposal_attempt.proposal
        observed = item.verification
        if proposal_attempt.model:
            models.append(clean(proposal_attempt.model))
        trajectory.append(ProofAttempt(
            attempt_number=item.attempt_number,
            proposal_valid=bool(candidate and proposal_attempt.validation_result
                                and proposal_attempt.validation_result.valid and not proposal_attempt.error),
            changed_files=strings(observed.changed_files if observed else candidate.target_files if candidate else []),
            summary=clean(candidate.summary) if candidate else None,
            candidate_diff=clean(candidate.unified_diff, 32000) if candidate else None,
            verification_status=observed.status if observed else item.outcome,
            targeted=test(observed.targeted_tests) if observed else None,
            regression=test(observed.regression_tests) if observed else None,
            failure_reason=clean((observed.failure_reason if observed else None) or proposal_attempt.error
                or ("; ".join(proposal_attempt.validation_result.errors) if proposal_attempt.validation_result else None)),
            risk_notes=strings((candidate.risk_notes if candidate else []) + (observed.risk_notes if observed else [])),
        ))

    final = run.final_verification
    last = run.attempts[-1] if run.attempts else None
    targeted = test(final.targeted_tests) if final else None
    regression = test(final.regression_tests) if final else None
    issues = []
    if not reproduced:
        issues.append("Original test failure was not reproduced successfully.")
    if run.completed_at is None:
        issues.append("Repair run has no completion timestamp.")
    if [item.attempt_number for item in run.attempts] != list(range(1, len(run.attempts) + 1)):
        issues.append("Attempt ordering is inconsistent.")
    if len(run.attempts) > run.max_attempts:
        issues.append("Attempt budget was exceeded.")
    if final is None:
        issues.append("Final verification evidence is unavailable.")
    elif last is None or last.verification != final or last.proposal_attempt != final.proposal_attempt:
        issues.append("Final verification is not linked to the final attempt's candidate.")
    for item in run.attempts:
        observed = item.verification
        if run.verified or run.status == "verified":
            if observed is None:
                issues.append(f"Attempt {item.attempt_number} verification is missing from a successful run.")
            if item is not last and (observed is None or observed.status not in {"targeted_test_failure", "regression_failure"}):
                issues.append(f"Attempt {item.attempt_number} does not support the recorded repair trajectory.")
        if item.proposal_attempt.diagnosis_attempt != run.initial_diagnosis:
            issues.append(f"Attempt {item.attempt_number} diagnosis linkage is inconsistent.")
        if observed:
            valid = item.proposal_attempt.validation_result
            if not valid or not valid.valid or item.proposal_attempt.error or item.proposal_attempt.proposal is None:
                issues.append(f"Attempt {item.attempt_number} was verified without a valid candidate.")
            if observed.proposal_attempt != item.proposal_attempt or observed.status != item.outcome:
                issues.append(f"Attempt {item.attempt_number} verification linkage is inconsistent.")
            if not observed.original_unchanged or not observed.workspace_cleaned:
                issues.append(f"Attempt {item.attempt_number} integrity or cleanup did not pass.")
            if any(value is not None and not test(value).consistent for value in (observed.targeted_tests, observed.regression_tests)):
                issues.append(f"Attempt {item.attempt_number} test summaries contradict raw evidence.")
    verified_claim = run.verified or run.status == "verified" or bool(final and final.verified)
    final_checks = bool(final and final.verified and final.status == "verified" and final.applied
                        and targeted and targeted.passed and regression and regression.passed
                        and final.original_unchanged and final.workspace_cleaned)
    if verified_claim and not (run.verified and run.status == "verified" and final_checks):
        issues.append("Success flags are incomplete or contradict verification evidence.")
    patch = None
    if final_checks and last and trajectory[-1].proposal_valid:
        proposal = last.proposal_attempt.proposal
        context = last.proposal_attempt.diagnosis_attempt.context
        limits = last.proposal_attempt.validation_result.limits
        try:
            # These existing validators are pure; the root is lexical only, never read.
            root = Path("C:/approved-proof-root") if os.name == "nt" else Path("/approved-proof-root")
            expected = patched_contents(proposal, context, root, limits)
            actual = patched_contents(proposal.model_copy(update={"unified_diff": final.diff_after_apply}), context, root, limits)
            if expected != actual or set(final.changed_files) != set(proposal.target_files):
                raise ValueError("Patch effect mismatch")
            diff = proposal.unified_diff
            if clean(diff, 32000) != diff:
                raise ValueError("Verified diff cannot be disclosed without sanitization")
            additions = deletions = old_remaining = new_remaining = 0
            for line in diff.splitlines():
                if old_remaining or new_remaining:
                    if line.startswith("+"):
                        additions += 1
                        new_remaining -= 1
                    elif line.startswith("-"):
                        deletions += 1
                        old_remaining -= 1
                    else:
                        old_remaining -= 1
                        new_remaining -= 1
                elif line.startswith("@@ "):
                    header = re.match(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)
                    old_remaining, new_remaining = (int(value or 1) for value in header.groups())
            patch = ProofPatch(attempt_number=last.attempt_number, changed_files=list(final.changed_files),
                unified_diff=diff, additions=additions, deletions=deletions)
        except (ValueError, TypeError, AttributeError):
            issues.append("Final patch is missing, inconsistent, or unsafe to disclose exactly.")
    elif verified_claim:
        issues.append("No valid final candidate supports the success claim.")
    verified = bool(run.verified and final_checks and patch and not issues)
    status = "VERIFIED" if verified else "INCOMPLETE_EVIDENCE" if issues else "VERIFICATION_FAILED" if final else "NOT_VERIFIED"
    if not verified:
        patch = None
        # Never display a standalone VERIFIED attempt badge from an untrusted flag.
        trajectory = [item.model_copy(update={"verification_status": "incomplete_evidence"})
                      if item.verification_status == "verified" else item for item in trajectory]
    observations = [item.verification for item in run.attempts if item.verification]
    diagnosis = run.initial_diagnosis.diagnosis
    risks = ["Passing supplied tests is not mathematical proof of program correctness.",
             "Coverage is limited to tests actually executed; the current fixture adapter is Python-only."]
    if regression and regression.passed:
        risks.append("No regressions were detected by the executed regression suite.")
    for item in trajectory:
        risks.extend(item.risk_notes)
    return ProofOfFix(generated_at=generated_at or datetime.now(timezone.utc), status=status, verified=verified,
        reproduction=ProofReproduction(reproduced=reproduced, failed_tests=strings(reproduction.failed_tests, 10),
            exit_code=reproduction.exit_code, stdout_excerpt=clean(reproduction.stdout), stderr_excerpt=clean(reproduction.stderr),
            duration_ms=reproduction.duration_ms),
        diagnosis=ProofDiagnosis(summary=clean(diagnosis.failure_summary), root_cause_hypothesis=clean(diagnosis.root_cause_hypothesis),
            suspected_files=strings(diagnosis.suspected_files), supporting_evidence=strings(diagnosis.supporting_evidence),
            confidence=diagnosis.confidence, model=clean(run.initial_diagnosis.model)) if diagnosis else None,
        repair_trajectory=trajectory, final_patch=patch,
        verification=ProofVerification(targeted=targeted, regression=regression, final_status=final.status if final else None,
                                      evidence_issues=issues),
        integrity=ProofIntegrity(original_repository_unchanged=all(item.original_unchanged for item in observations) if observations else None,
                                 workspace_cleaned=all(item.workspace_cleaned for item in observations) if observations else None),
        risks=list(dict.fromkeys(risks)), provenance=ProofProvenance(run_id=clean(run.run_id),
            runtime_models=list(dict.fromkeys(models)), attempt_count=len(run.attempts)))
