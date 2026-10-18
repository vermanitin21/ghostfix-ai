"""Human-reference corpus QA, separate from repair performance evaluation.

No model calls and no host-side fixture execution. PatchVerifier owns temporary
copies and Docker execution; reference material never enters a repair pipeline.
"""
import hashlib
import json
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.patches import validate_patch
from app.agent.state import DiagnosisAttempt, DiagnosisInput, PatchProposal, PatchProposalAttempt
from app.agent.verification import PatchVerifier, fixture_snapshot
from app.sandbox.models import SandboxResult
from app.sandbox.runner import DockerPytestRunner
from app.tools.files import RepositoryContext

from .models import BenchmarkCase, BenchmarkManifest
from .runner import fixture_repository


class CorpusCaseValidation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: str
    valid: bool = False
    broken_target_failed: bool = False
    broken_failure_exit_code: int | None = None
    reference_patch_applied: bool = False
    repaired_target_passed: bool = False
    repaired_regression_passed: bool = False
    original_unchanged: bool = False
    workspace_cleaned: bool = False
    errors: list[str] = Field(default_factory=list)


class CorpusValidationRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    purpose: Literal["human-reference-corpus-qa"] = "human-reference-corpus-qa"
    case_count: int
    valid_count: int
    invalid_count: int
    corpus_fingerprint_sha256: str | None
    cases: list[CorpusCaseValidation]


def fixture_hashes(repository: RepositoryContext) -> dict[str, str]:
    """Exact-byte hashes for integrity, including platform line endings."""
    return {name: hashlib.sha256(data).hexdigest()
            for name, data in sorted(fixture_snapshot(repository).items())}


def corpus_fingerprint(manifest: BenchmarkManifest, fixture_root: Path) -> str:
    """SHA256 of compact canonical UTF-8 JSON; text line endings normalized to LF."""
    metadata = manifest.model_dump(mode="json")
    metadata["cases"] = sorted(metadata["cases"], key=lambda item: item["id"])
    files = {}
    for case in sorted(manifest.cases, key=lambda item: item.id):
        repository = fixture_repository(fixture_root, case, ())
        files[case.id] = {name: data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                         for name, data in sorted(fixture_snapshot(repository).items())}
    encoded = json.dumps({"manifest": metadata, "files": files}, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_reference(reference_root: Path, fixture_root: Path, case: BenchmarkCase) -> str:
    root = reference_root.resolve(strict=True)
    fixtures = fixture_root.resolve(strict=True)
    if root.is_relative_to(fixtures) or fixtures.is_relative_to(root):
        raise ValueError("Reference directory must be disjoint from fixture root")
    # Reject links on the supplied root and on the individual reference.
    for path in (reference_root, root / f"{case.id}.diff"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked reference material is forbidden")
    path = root / f"{case.id}.diff"
    if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 12000:
        raise ValueError("Reference must be a bounded ordinary file")
    return path.read_text(encoding="utf-8")


def reference_attempt(case: BenchmarkCase, repository: RepositoryContext,
                      broken: SandboxResult, diff: str) -> PatchProposalAttempt:
    """Adapt a human diff to the existing validator/verifier, not an LLM proposal."""
    files = repository.read_files(sorted(fixture_snapshot(repository)))
    context = DiagnosisInput(exit_code=broken.exit_code, failed_tests=broken.failed_tests,
        stdout_excerpt=broken.stdout[:2000], stderr_excerpt=broken.stderr[:2000], files=files)
    diagnosis = DiagnosisAttempt(sandbox=broken, context=context, status="reproduced")
    targets = [line[6:] for line in diff.splitlines() if line.startswith("+++ b/")]
    proposal = PatchProposal(target_files=targets, summary="Human corpus reference",
        rationale="Validate fixture solvability only", unified_diff=diff,
        expected_behavior_change="Satisfy supplied tests", tests_to_run=case.expected_failing_tests,
        risk_notes=["Not a model-generated repair; not benchmark performance evidence"], confidence=1.0)
    validation = validate_patch(proposal, context, repository.root)
    return PatchProposalAttempt(diagnosis_attempt=diagnosis, proposal=proposal,
                                validation_result=validation, model=None)


def validate_case(case: BenchmarkCase, fixture_root: Path, reference_root: Path,
                  sandbox: DockerPytestRunner) -> CorpusCaseValidation:
    values = dict(case_id=case.id)
    errors = []
    repository = before = None
    try:
        if not case.difficulty or not case.bug_category:
            raise ValueError("Corpus metadata is incomplete")
        repository = fixture_repository(fixture_root, case, ())
        before = fixture_hashes(repository)
        diff = read_reference(reference_root, fixture_root, case)
        broken = sandbox.run(repository.root)
        reproduced = (broken.exit_code == 1 and not broken.timed_out and not broken.infrastructure_error
                      and set(broken.failed_tests) == set(case.expected_failing_tests))
        values.update(broken_target_failed=reproduced, broken_failure_exit_code=broken.exit_code)
        if not reproduced:
            errors.append("Broken suite did not fail exactly the expected targets normally")
        else:
            attempt = reference_attempt(case, repository, broken, diff)
            if not attempt.validation_result.valid:
                errors.append("Human reference failed local patch validation")
            else:
                verification = PatchVerifier(sandbox).verify(attempt, repository)
                values.update(reference_patch_applied=verification.applied,
                    repaired_target_passed=bool(verification.targeted_tests and verification.targeted_tests.passed),
                    repaired_regression_passed=bool(verification.regression_tests and verification.regression_tests.passed),
                    workspace_cleaned=verification.workspace_cleaned)
                if not (verification.verified and verification.applied
                        and values["repaired_target_passed"] and values["repaired_regression_passed"]):
                    errors.append("Reference verification did not pass: " + verification.status)
                if not verification.original_unchanged:
                    errors.append("Verifier could not confirm original integrity")
    except Exception:
        # Never expose arbitrary exception text, credentials or machine paths.
        errors.append("Corpus safety, reference or execution operation failed")
    finally:
        if repository is not None and before is not None:
            try:
                values["original_unchanged"] = before == fixture_hashes(repository)
            except Exception:
                values["original_unchanged"] = False
        if not values.get("original_unchanged"):
            errors.append("Original fixture integrity not confirmed")
        if not values.get("workspace_cleaned"):
            errors.append("Workspace cleanup not confirmed")
    return CorpusCaseValidation(**values, errors=errors, valid=not errors)


def validate_corpus(manifest: BenchmarkManifest, fixture_root: Path, reference_root: Path,
                    sandbox: DockerPytestRunner) -> CorpusValidationRun:
    try:
        fingerprint = corpus_fingerprint(manifest, fixture_root)
    except (ValueError, OSError):
        fingerprint = None
    results = [validate_case(case, fixture_root, reference_root, sandbox)
               for case in sorted(manifest.cases, key=lambda item: item.id) if case.enabled]
    # Fail closed if fingerprinting any registered fixture was unsafe.
    if fingerprint is None:
        results = [r.model_copy(update={"valid": False, "errors": r.errors + ["Corpus fingerprint unavailable"]})
                   for r in results]
    return CorpusValidationRun(case_count=len(results), valid_count=sum(r.valid for r in results),
        invalid_count=sum(not r.valid for r in results), cases=results, corpus_fingerprint_sha256=fingerprint)
