"""Versioned benchmark contracts. Metadata is never verification evidence."""

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def relative_path(value: str) -> str:
    if (not re.fullmatch(r"[A-Za-z0-9_/-]+(?:\.py)?", value)
            or any(part in {"", ".", ".."} or part.startswith(".") for part in value.split("/"))):
        raise ValueError("Expected a safe relative fixture path")
    return value


class BenchmarkCase(EvaluationModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=1000)
    fixture_path: str = Field(max_length=200)
    language: Literal["python"] = "python"
    framework: Literal["pytest"] = "pytest"
    expected_failing_tests: list[str] = Field(min_length=1, max_length=8)
    tags: list[Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[a-z0-9-]+$")]] = Field(default_factory=list, max_length=16)
    enabled: bool = True
    difficulty: str | None = Field(default=None, max_length=80)
    bug_category: str | None = Field(default=None, max_length=80)

    _path = field_validator("fixture_path")(relative_path)

    @field_validator("expected_failing_tests")
    @classmethod
    def selectors(cls, values: list[str]) -> list[str]:
        for value in values:
            if not re.fullmatch(r"[A-Za-z0-9_/-]+\.py(?:::[A-Za-z_][A-Za-z0-9_]*){0,2}", value):
                raise ValueError("Expected pytest node IDs, not commands")
            path = relative_path(value.split("::")[0])
            if not path.split("/")[-1].startswith("test_"):
                raise ValueError("Expected test_*.py selectors")
        if len(set(values)) != len(values):
            raise ValueError("Duplicate test selectors")
        return values


class BenchmarkManifest(EvaluationModel):
    schema_version: Literal["1.0"] = "1.0"
    name: str = Field(min_length=1, max_length=160)
    cases: list[BenchmarkCase] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Duplicate benchmark case IDs")
        return self


class FailureCategory(StrEnum):
    NONE = "NONE"
    REPRODUCTION_FAILURE = "REPRODUCTION_FAILURE"
    DIAGNOSIS_FAILURE = "DIAGNOSIS_FAILURE"
    INVALID_PROPOSAL = "INVALID_PROPOSAL"
    PATCH_APPLICATION_FAILURE = "PATCH_APPLICATION_FAILURE"
    TARGETED_TEST_FAILURE = "TARGETED_TEST_FAILURE"
    REGRESSION_FAILURE = "REGRESSION_FAILURE"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
    TIMEOUT = "TIMEOUT"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    CLEANUP_FAILURE = "CLEANUP_FAILURE"
    RETRY_BUDGET_EXHAUSTED = "RETRY_BUDGET_EXHAUSTED"
    INCOMPLETE_EVIDENCE = "INCOMPLETE_EVIDENCE"
    UNKNOWN = "UNKNOWN"


class BenchmarkCaseResult(EvaluationModel):
    case_id: str
    case_name: str
    status: Literal["COMPLETED", "INCOMPLETE", "SKIPPED"]
    verified: bool = False
    attempt_count: int = Field(default=0, ge=0)
    first_attempt_success: bool = False
    self_correction_used: bool = False
    self_correction_recovered: bool = False
    targeted_passed: bool | None = None
    regression_passed: bool | None = None
    integrity_passed: bool | None = None
    cleanup_passed: bool | None = None
    failure_category: FailureCategory = FailureCategory.NONE
    retry_budget_exhausted: bool = False
    changed_file_count: int | None = None
    additions: int | None = None
    deletions: int | None = None
    patch_size: int | None = None
    reproduction_duration_ms: int | None = None
    targeted_duration_ms: int | None = None
    regression_duration_ms: int | None = None
    total_verification_duration_ms: int | None = None
    end_to_end_duration_ms: int | None = None
    runtime_models: list[str] = Field(default_factory=list)
    evidence_run_id: str | None = None
    proof_status: str | None = None
    attempt_statuses: list[str] = Field(default_factory=list)
    evidence_issues: list[str] = Field(default_factory=list)
    skip_reason: Literal["disabled", "not_selected", "max_cases", "safety_stop"] | None = None


class BenchmarkMetrics(EvaluationModel):
    total_cases: int
    completed_cases: int
    verified_cases: int
    failed_cases: int
    incomplete_cases: int
    skipped_cases: int
    repair_success_rate: float | None
    first_attempt_success_rate: float | None
    self_correction_usage_rate: float | None
    self_correction_recovery_rate: float | None
    targeted_pass_rate: float | None
    regression_pass_rate: float | None
    integrity_pass_rate: float | None
    cleanup_pass_rate: float | None
    rate_denominators: dict[str, int]
    total_attempts: int
    mean_attempts: float | None
    median_attempts: float | None
    max_attempts: int | None
    mean_changed_files: float | None
    mean_additions: float | None
    mean_deletions: float | None
    mean_patch_size: float | None
    mean_reproduction_duration_ms: float | None
    mean_targeted_duration_ms: float | None
    mean_regression_duration_ms: float | None
    mean_verification_duration_ms: float | None
    mean_sample_counts: dict[str, int]
    failure_counts_by_category: dict[str, int]


class BenchmarkRun(EvaluationModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    generated_at: datetime
    benchmark_name: str
    baseline_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    mode: Literal["mock", "pipeline"]
    case_results: list[BenchmarkCaseResult]
    metrics: BenchmarkMetrics
    runtime_models: list[str]
    limitations: list[str]
