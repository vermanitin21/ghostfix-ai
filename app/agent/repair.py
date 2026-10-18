"""Bounded evidence-driven orchestration above the existing proposal/verifier boundary."""

import asyncio
import re
from datetime import datetime, timezone

from app.agent.orchestrator import GhostFixOrchestrator
from app.agent.state import (
    CorrectionEvidence, CorrectionTestEvidence, DiagnosisAttempt, PatchLimits,
    PatchVerification, RepairAttempt, RepairRun, VerificationTests,
)
from app.agent.verification import PatchVerifier, fixture_snapshot
from app.tools.files import RepositoryContext, redact


def correction_evidence(verification: PatchVerification, repository: RepositoryContext) -> CorrectionEvidence:
    """Project verification onto an explicit, bounded allowlist of observations."""
    def clean(text: str, limit: int) -> str:
        text = redact(text, repository.secrets).replace(str(repository.root), "[repository]")
        text = re.sub(r"[A-Za-z]:[\\/][^\s\"'<>]+", "[host-path]", text)
        text = re.sub(r"(?<![\w])/(?:tmp|home|Users|var|workspace)/[^\s\"'<>]+", "[host-path]", text)
        return text[:limit]

    def test(result: VerificationTests | None) -> CorrectionTestEvidence | None:
        if result is None:
            return None
        return CorrectionTestEvidence(
            exit_code=result.exit_code, passed=result.passed,
            failed_tests=[clean(name, 300) for name in result.failed_tests[:10]],
            stdout_excerpt=clean(result.stdout_excerpt, 2000),
            stderr_excerpt=clean(result.stderr_excerpt, 2000),
        )

    def sanitize(value):
        if isinstance(value, str):
            return clean(value, 32000)
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        if isinstance(value, dict):
            return {key: sanitize(item) for key, item in value.items()}
        return value

    proposal = verification.proposal_attempt.proposal
    if proposal is None:
        raise ValueError("Correction requires a previous proposal")
    return CorrectionEvidence(
        previous_proposal=type(proposal).model_validate(sanitize(proposal.model_dump())),
        verification_status=verification.status,
        targeted_tests=test(verification.targeted_tests), regression_tests=test(verification.regression_tests),
        changed_files=[clean(name, 300) for name in verification.changed_files[:4]],
    )


class RepairLoop:
    def __init__(self, agent: GhostFixOrchestrator, verifier: PatchVerifier) -> None:
        self.agent = agent
        self.verifier = verifier

    async def run(
        self, diagnosis: DiagnosisAttempt, repository: RepositoryContext,
        limits: PatchLimits | None = None, *, max_attempts: int = 2,
    ) -> RepairRun:
        # Strict Pydantic budget validation occurs before any model or Docker call.
        run = RepairRun(initial_diagnosis=diagnosis, max_attempts=max_attempts)
        limits = limits or PatchLimits()
        correction = None
        try:
            if diagnosis.status != "diagnosed" or diagnosis.context is None or diagnosis.diagnosis is None:
                if diagnosis.sandbox.timed_out:
                    run.status = "timeout"
                elif diagnosis.sandbox.infrastructure_error:
                    run.status = "infrastructure_failure"
                run.stop_reason = "A successful initial diagnosis is required"
                return run
            baseline = fixture_snapshot(repository)
            for number in range(1, run.max_attempts + 1):
                if fixture_snapshot(repository) != baseline:
                    run.status, run.stop_reason = "infrastructure_failure", "Original repository changed"
                    break
                if correction is None:
                    proposal = await self.agent.propose_patch(diagnosis, repository, limits)
                else:
                    proposal = await self.agent.propose_patch(diagnosis, repository, limits, correction=correction)
                attempt = RepairAttempt(attempt_number=number, proposal_attempt=proposal,
                                        correction_evidence=correction)
                run.attempts.append(attempt)
                if (proposal.error or proposal.proposal is None or proposal.validation_result is None
                        or not proposal.validation_result.valid):
                    run.status, run.stop_reason = "proposal_failure", "Proposal generation or safety validation failed"
                    break
                verification = await asyncio.to_thread(self.verifier.verify, proposal, repository)
                attempt.verification = verification
                attempt.outcome = verification.status
                run.final_verification = verification
                if (not verification.workspace_cleaned or not verification.original_unchanged
                        or fixture_snapshot(repository) != baseline):
                    run.status, run.stop_reason = "infrastructure_failure", "Cleanup or original integrity check failed"
                    break
                if (verification.verified and verification.status == "verified"
                        and verification.targeted_tests and verification.targeted_tests.passed
                        and verification.regression_tests and verification.regression_tests.passed):
                    run.verified, run.status, run.stop_reason = True, "verified", "Candidate passed targeted and regression verification"
                    break
                if verification.status not in {"targeted_test_failure", "regression_failure"}:
                    run.status = verification.status if verification.status != "verified" else "infrastructure_failure"
                    run.stop_reason = "Non-retryable verification outcome"
                    break
                failed_stage = (verification.targeted_tests if verification.status == "targeted_test_failure"
                                else verification.regression_tests)
                if (not verification.applied or failed_stage is None or failed_stage.passed
                        or failed_stage.exit_code != 1 or failed_stage.evidence.timed_out
                        or failed_stage.evidence.infrastructure_error):
                    run.status, run.stop_reason = "infrastructure_failure", "Inconsistent retry evidence"
                    break
                if number == run.max_attempts:
                    run.status, run.stop_reason = "retry_exhausted", "Verification failures exhausted the attempt budget"
                    break
                correction = correction_evidence(verification, repository)
        except TimeoutError:
            run.status, run.stop_reason = "timeout", "Repair stage timed out"
        except Exception:
            run.status, run.stop_reason = "infrastructure_failure", "Repair orchestration failed; no automatic retry"
        finally:
            run.completed_at = datetime.now(timezone.utc)
        return run
