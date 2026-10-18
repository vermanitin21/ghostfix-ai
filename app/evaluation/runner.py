"""Bounded case orchestration, separate from pure evaluation and repair policy."""
import stat
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from app.agent.state import RepairRun
from app.sandbox.test_ids import validate_test_ids
from app.tools.files import RepositoryContext

from .evaluator import evaluate_case
from .metrics import aggregate_metrics
from .models import BenchmarkCase, BenchmarkCaseResult, BenchmarkManifest, BenchmarkRun, FailureCategory
from .safety import safe_text

RepairFunction = Callable[[BenchmarkCase, RepositoryContext], Awaitable[RepairRun]]
LIMITATIONS = [
    "Benchmark success means GhostFix produced a candidate that satisfied its executed verification policy for that benchmark case.",
    "Passing executed tests is not formal correctness; untested behavior remains unknown.",
    "A one-case manifest validates infrastructure, not broad repair reliability.",
    "End-to-end duration is unmeasured; verification durations sum recorded sandbox executions only.",
    "Unknown credentials cannot be detected universally; pass known secrets for redaction.",
]


def fixture_repository(root: Path, case: BenchmarkCase, secrets: tuple[str, ...]) -> RepositoryContext:
    approved = root.resolve(strict=True)
    candidate = approved
    for component in case.fixture_path.split("/"):
        candidate = candidate / component
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Linked fixture paths are not supported")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(approved) or not resolved.is_dir():
        raise ValueError("Fixture must remain within the approved fixture root")
    repository = RepositoryContext(resolved, secrets=secrets)
    validate_test_ids(repository.root, case.expected_failing_tests)
    # Reuse context checks to reject selector links, hardlinks, binary or oversized files.
    for selector in case.expected_failing_tests:
        repository.read_files([selector.split("::")[0]])
    return repository


def pipeline_repair(agent, repair_loop) -> RepairFunction:
    """Inject configured existing components; owns neither credentials nor their lifetime."""
    async def repair(case: BenchmarkCase, repository: RepositoryContext) -> RepairRun:
        from app.agent.verification import fixture_snapshot

        paths = sorted(fixture_snapshot(repository))
        diagnosis = await agent.analyze(repository, paths)
        return await repair_loop.run(diagnosis, repository)
    return repair


async def run_benchmark(manifest: BenchmarkManifest, approved_fixture_root: Path,
                        repair: RepairFunction, *, selected_ids: list[str] | None = None,
                        max_cases: int = 10, mode: Literal["mock", "pipeline"] = "pipeline",
                        baseline_commit: str | None = None, secrets: tuple[str, ...] = (),
                        run_id: str | None = None, generated_at: datetime | None = None) -> BenchmarkRun:
    if type(max_cases) is not int or not 1 <= max_cases <= 100:
        raise ValueError("max_cases must be an integer from 1 to 100")
    known = {case.id for case in manifest.cases}
    if selected_ids is not None and (len(set(selected_ids)) != len(selected_ids) or set(selected_ids) - known):
        raise ValueError("Unknown or duplicate selected case IDs")
    # Validate metadata before executing any repair.
    metadata = dict(run_id=safe_text(run_id or str(uuid4()), secrets), generated_at=generated_at or datetime.now(timezone.utc),
                    benchmark_name=safe_text(manifest.name, secrets), mode=mode, baseline_commit=baseline_commit)
    BenchmarkRun(**metadata, case_results=[], metrics=aggregate_metrics([]), runtime_models=[], limitations=[])
    results, executed, stopped = [], 0, False
    for case in sorted(manifest.cases, key=lambda item: item.id):
        reason = ("disabled" if not case.enabled else "not_selected" if selected_ids is not None and case.id not in selected_ids
                  else "safety_stop" if stopped else "max_cases" if executed >= max_cases else None)
        if reason:
            results.append(BenchmarkCaseResult(case_id=safe_text(case.id, secrets), case_name=safe_text(case.name, secrets),
                                              status="SKIPPED", skip_reason=reason))
            continue
        executed += 1
        try:
            repository = fixture_repository(approved_fixture_root, case, secrets)
            evidence = await repair(case, repository)
            result = evaluate_case(case, evidence, secrets=secrets)
            results.append(result)
            stopped = result.integrity_passed is False or result.cleanup_passed is False
        except Exception:
            # Do not serialize exception text: it can contain secrets/host paths.
            # Cancellation/SystemExit/KeyboardInterrupt propagate; repair owns cleanup.
            results.append(BenchmarkCaseResult(case_id=safe_text(case.id, secrets), case_name=safe_text(case.name, secrets),
                status="INCOMPLETE", failure_category=FailureCategory.INFRASTRUCTURE_FAILURE,
                evidence_issues=["Case execution failed before a complete RepairRun could be evaluated."]))
            # With no returned evidence we cannot establish cleanup/integrity for further runs.
            stopped = True
    return BenchmarkRun(**metadata, case_results=results, metrics=aggregate_metrics(results),
        runtime_models=sorted({model for result in results for model in result.runtime_models}),
        limitations=LIMITATIONS + (["MOCK: all repair evidence is synthetic; no Docker or model calls were made."] if mode == "mock" else []))
