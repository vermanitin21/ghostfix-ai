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


class PatchProposalAttempt(BaseModel):
    attempt_id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    diagnosis_attempt: DiagnosisAttempt
    proposal: PatchProposal | None = None
    validation_result: PatchValidationResult | None = None
    model: str | None = None
    error: str | None = None
