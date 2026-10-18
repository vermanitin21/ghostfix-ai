"""Public report schema; descriptive model text never supplies verification status."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProofReproduction(ReportModel):
    reproduced: bool
    failed_tests: list[str]
    exit_code: int
    stdout_excerpt: str
    stderr_excerpt: str
    duration_ms: int


class ProofDiagnosis(ReportModel):
    summary: str
    root_cause_hypothesis: str
    suspected_files: list[str]
    supporting_evidence: list[str]
    confidence: float | None
    model: str | None


class ProofTest(ReportModel):
    passed: bool
    consistent: bool
    pytest_argv: list[str] | None
    exit_code: int
    failed_tests: list[str]
    stdout_excerpt: str
    stderr_excerpt: str
    duration_ms: int
    timed_out: bool
    infrastructure_error: str | None


class ProofAttempt(ReportModel):
    attempt_number: int
    proposal_valid: bool
    changed_files: list[str]
    summary: str | None
    candidate_diff: str | None
    verification_status: str
    targeted: ProofTest | None
    regression: ProofTest | None
    failure_reason: str | None
    risk_notes: list[str]


class ProofPatch(ReportModel):
    attempt_number: int
    changed_files: list[str]
    unified_diff: str
    additions: int
    deletions: int


class ProofVerification(ReportModel):
    targeted: ProofTest | None
    regression: ProofTest | None
    final_status: str | None
    evidence_issues: list[str]


class ProofIntegrity(ReportModel):
    original_repository_unchanged: bool | None
    original_hashes_unchanged: bool | None = None
    workspace_cleaned: bool | None


class ProofProvenance(ReportModel):
    run_id: str
    runtime_models: list[str]
    attempt_count: int
    correction_request_sha256: str | None = None
    generation_method: Literal["deterministic-evidence-builder/v1"] = "deterministic-evidence-builder/v1"
    evidence_derived_fields: list[str] = Field(default_factory=lambda: [
        "status", "verified", "reproduction.reproduced", "verification", "integrity", "final_patch",
    ])


class ProofOfFix(ReportModel):
    schema_version: Literal["1.0"] = "1.0"
    generated_at: datetime
    status: Literal["VERIFIED", "NOT_VERIFIED", "VERIFICATION_FAILED", "INCOMPLETE_EVIDENCE"]
    verified: bool
    reproduction: ProofReproduction
    diagnosis: ProofDiagnosis | None
    repair_trajectory: list[ProofAttempt]
    final_patch: ProofPatch | None
    verification: ProofVerification
    integrity: ProofIntegrity
    risks: list[str]
    provenance: ProofProvenance
