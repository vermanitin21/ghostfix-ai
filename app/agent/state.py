"""Typed evidence and diagnosis records for a future Proof of Fix report."""

from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.sandbox.models import SandboxResult

ShortText = Annotated[str, Field(min_length=1, max_length=2000)]


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    failure_summary: ShortText
    suspected_files: list[ShortText] = Field(min_length=1, max_length=8)
    suspected_symbols: list[ShortText] = Field(max_length=16)
    root_cause_hypothesis: ShortText
    supporting_evidence: list[ShortText] = Field(min_length=1, max_length=8)
    confidence: float = Field(ge=0, le=1)
    recommended_next_action: ShortText


class ConciseDiagnosis(Diagnosis):
    """Wire-only v2 limits; archived Diagnosis evidence remains readable."""
    failure_summary: str = Field(min_length=1, max_length=240)
    root_cause_hypothesis: str = Field(min_length=1, max_length=400)
    supporting_evidence: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(min_length=1, max_length=3)
    suspected_files: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(min_length=1, max_length=4)
    suspected_symbols: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(max_length=4)
    recommended_next_action: str = Field(min_length=1, max_length=240)


class DiagnosisProtocolEvent(BaseModel):
    call_number: int = Field(ge=1, le=2)
    outcome: Literal["started", "accepted", "output_budget", "error"]
    model: str | None = None


class ContextFile(BaseModel):
    path: str
    content: str


class DiagnosisInput(BaseModel):
    exit_code: int
    failed_tests: list[str]
    stdout_excerpt: str
    stderr_excerpt: str
    files: list[ContextFile]


class DiagnosisAttempt(BaseModel):
    attempt_id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    sandbox: SandboxResult
    context: DiagnosisInput | None = None
    diagnosis: Diagnosis | None = None
    model: str | None = None
    status: Literal["reproduced", "diagnosed", "failed"] = "reproduced"
    error: str | None = None
    protocol_retry_count: int = Field(default=0, ge=0, le=1)
    protocol_events: list[DiagnosisProtocolEvent] = Field(default_factory=list, max_length=2)


class PatchProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    target_files: list[ShortText] = Field(min_length=1, max_length=8)
    summary: ShortText
    rationale: ShortText
    unified_diff: str = Field(min_length=1, max_length=32000)
    expected_behavior_change: ShortText
    tests_to_run: list[ShortText] = Field(min_length=1, max_length=8)
    risk_notes: list[ShortText] = Field(max_length=8)
    confidence: float = Field(ge=0, le=1)


class PatchLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_files: int = Field(default=2, ge=1, le=4)
    max_changed_lines: int = Field(default=100, ge=1, le=200)
    max_diff_bytes: int = Field(default=12000, ge=1, le=32000)


class PatchValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)
    changed_lines: int = 0
    limits: PatchLimits
    failure_category: Literal["RETRYABLE_FORMAT_ERROR", "SECURITY_POLICY_ERROR", "DISALLOWED_PATH",
        "TEST_MODIFICATION", "SEMANTICALLY_AMBIGUOUS_PATCH", "UNSUPPORTED_OPERATION", "OTHER_INVALID_PROPOSAL"] | None = None


class PatchProtocolEvent(BaseModel):
    call_number: int = Field(ge=1, le=2)
    model: str | None = None
    raw_response_sha256: str
    raw_model_diff: str
    normalized_diff: str | None = None
    normalization_reason: Literal["source_proven_context_prefix"] | None = None
    validation: PatchValidationResult


class PatchProposalAttempt(BaseModel):
    attempt_id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    diagnosis_attempt: DiagnosisAttempt
    proposal: PatchProposal | None = None
    validation_result: PatchValidationResult | None = None
    model: str | None = None
    error: str | None = None
    protocol_retry_count: int = Field(default=0, ge=0, le=1)
    protocol_events: list[PatchProtocolEvent] = Field(default_factory=list, max_length=2)


class VerificationTests(BaseModel):
    command: list[str]
    exit_code: int
    passed: bool
    failed_tests: list[str]
    stdout_excerpt: str
    stderr_excerpt: str
    duration_ms: int
    evidence: SandboxResult


class PatchVerification(BaseModel):
    proposal_attempt: PatchProposalAttempt
    applied: bool = False
    targeted_tests: VerificationTests | None = None
    regression_tests: VerificationTests | None = None
    changed_files: list[str] = Field(default_factory=list)
    diff_after_apply: str = ""
    verified: bool = False
    status: Literal["application_failure", "targeted_test_failure", "regression_failure",
                    "infrastructure_failure", "timeout", "verified"] = "application_failure"
    failure_reason: str | None = None
    risk_notes: list[str] = Field(default_factory=list)
    workspace_cleaned: bool = False
    original_unchanged: bool = False


class CorrectionTestEvidence(BaseModel):
    exit_code: int
    passed: bool
    failed_tests: list[str] = Field(max_length=10)
    stdout_excerpt: str = Field(max_length=2000)
    stderr_excerpt: str = Field(max_length=2000)


class CorrectionEvidence(BaseModel):
    """Only bounded observations; intentionally excludes command and host paths."""
    previous_proposal: PatchProposal
    verification_status: Literal["targeted_test_failure", "regression_failure"]
    targeted_tests: CorrectionTestEvidence | None
    regression_tests: CorrectionTestEvidence | None
    changed_files: list[str] = Field(max_length=4)


class RepairAttempt(BaseModel):
    attempt_number: int = Field(ge=1, le=2)
    proposal_attempt: PatchProposalAttempt
    verification: PatchVerification | None = None
    outcome: Literal["verified", "proposal_failure", "application_failure", "targeted_test_failure",
                     "regression_failure", "infrastructure_failure", "timeout"] = "proposal_failure"
    correction_evidence: CorrectionEvidence | None = None


class RepairRun(BaseModel):
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    initial_diagnosis: DiagnosisAttempt
    attempts: list[RepairAttempt] = Field(default_factory=list, max_length=2)
    verified: bool = False
    final_verification: PatchVerification | None = None
    status: Literal["verified", "retry_exhausted", "proposal_failure", "application_failure",
                    "infrastructure_failure", "timeout"] = "proposal_failure"
    stop_reason: str = ""
    max_attempts: int = Field(default=2, ge=1, le=2, strict=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
