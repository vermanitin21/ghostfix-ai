import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.evaluation import (
    BenchmarkCase, BenchmarkManifest, BenchmarkRun, aggregate_metrics, evaluate_case, render_json, render_markdown,
)
from app.evaluation.mock import NOW, SELECTOR, synthetic_run
from app.evaluation.models import FailureCategory as F
from app.evaluation.runner import fixture_repository, pipeline_repair, run_benchmark
from app.reporting import build_proof_of_fix

ROOT = Path(__file__).resolve().parents[1]


def case(identifier="calculator-001", **changes):
    return BenchmarkCase(id=identifier, name="Arithmetic", description="Fixture metadata, not evidence",
                         fixture_path="broken_math", expected_failing_tests=[SELECTOR], **changes)


def test_verified_and_patch_statistics():
    result = evaluate_case(case(), synthetic_run())
    assert result.verified and result.status == "COMPLETED" and result.failure_category == F.NONE
    assert result.first_attempt_success and not result.self_correction_used
    assert result.targeted_passed and result.regression_passed and result.integrity_passed and result.cleanup_passed
    assert (result.changed_file_count, result.additions, result.deletions, result.patch_size) == (1, 1, 1, 2)
    assert (result.reproduction_duration_ms, result.targeted_duration_ms,
            result.regression_duration_ms, result.total_verification_duration_ms) == (10, 20, 30, 50)
    assert result.end_to_end_duration_ms is None


@pytest.mark.parametrize("outcome,category", [("targeted_test_failure", F.TARGETED_TEST_FAILURE),
                                                ("regression_failure", F.REGRESSION_FAILURE)])
def test_test_failure(outcome, category):
    result = evaluate_case(case(), synthetic_run((outcome,)))
    assert result.failure_category == category and not result.verified and result.status == "COMPLETED"
    assert result.patch_size is None and result.retry_budget_exhausted


@pytest.mark.parametrize("outcome,category", [("application_failure", F.PATCH_APPLICATION_FAILURE),
    ("infrastructure_failure", F.INFRASTRUCTURE_FAILURE), ("timeout", F.TIMEOUT)])
def test_terminal_failures(outcome, category):
    run = synthetic_run(("targeted_test_failure",))
    run.status = outcome
    final = run.final_verification
    final.status = run.attempts[0].outcome = outcome
    final.applied = False
    final.targeted_tests = None
    assert evaluate_case(case(), run).failure_category == category


@pytest.mark.parametrize("field,category", [("original_unchanged", F.INTEGRITY_FAILURE), ("workspace_cleaned", F.CLEANUP_FAILURE)])
def test_safety_failures_outrank_success(field, category):
    run = synthetic_run()
    setattr(run.final_verification, field, False)
    result = evaluate_case(case(), run)
    assert not result.verified and result.failure_category == category


@pytest.mark.parametrize("mutation", ["missing_final", "missing_completion", "wrong_diff", "raw_failed",
    "missing_regression", "wrong_order", "flag", "diagnosis", "wrong_final_link", "failed_status_passed_test"])
def test_incomplete_and_contradictory_evidence(mutation):
    run = synthetic_run()
    final = run.final_verification
    if mutation == "missing_final": run.final_verification = None
    elif mutation == "missing_completion": run.completed_at = None
    elif mutation == "wrong_diff": final.diff_after_apply = "not a diff"
    elif mutation == "raw_failed": final.targeted_tests.evidence = final.targeted_tests.evidence.model_copy(update={"exit_code": 1})
    elif mutation == "missing_regression": final.regression_tests = None
    elif mutation == "wrong_order": run.attempts[0].attempt_number = 2
    elif mutation == "flag": run.verified = False
    elif mutation == "diagnosis": run.initial_diagnosis.diagnosis = None
    elif mutation == "wrong_final_link":
        run.final_verification = final.model_copy(deep=True)
        run.final_verification.proposal_attempt.attempt_id = "different"
    elif mutation == "failed_status_passed_test":
        run.verified = final.verified = False
        run.status = "retry_exhausted"
        final.status = run.attempts[0].outcome = "targeted_test_failure"
    result = evaluate_case(case(), run)
    assert not result.verified and result.status == "INCOMPLETE" and result.failure_category == F.INCOMPLETE_EVIDENCE


@pytest.mark.parametrize("phase,category", [("reproduction", F.REPRODUCTION_FAILURE),
    ("diagnosis", F.DIAGNOSIS_FAILURE), ("proposal", F.INVALID_PROPOSAL)])
def test_early_failure(phase, category):
    run = synthetic_run()
    run.verified = False
    run.status = "proposal_failure"
    run.final_verification = None
    if phase == "proposal":
        run.attempts[0].verification = None
        run.attempts[0].outcome = "proposal_failure"
        run.attempts[0].proposal_attempt.validation_result.valid = False
    else:
        run.attempts = []
        run.initial_diagnosis.status = "failed"
        run.initial_diagnosis.diagnosis = None
        if phase == "reproduction":
            run.initial_diagnosis.sandbox = run.initial_diagnosis.sandbox.model_copy(update={"exit_code": 0})
    result = evaluate_case(case(), run)
    assert result.status == "COMPLETED" and result.failure_category == category


def test_retry_recovery_and_exhaustion():
    recovered = evaluate_case(case(), synthetic_run(("targeted_test_failure", "verified")))
    failed = evaluate_case(case(), synthetic_run(("targeted_test_failure", "targeted_test_failure")))
    assert recovered.self_correction_used and recovered.self_correction_recovered and not recovered.first_attempt_success
    assert recovered.total_verification_duration_ms == 70
    assert failed.self_correction_used and not failed.self_correction_recovered and failed.retry_budget_exhausted
    assert failed.failure_category == F.TARGETED_TEST_FAILURE


def mixed_results():
    outcomes = [("verified",), ("verified",), ("targeted_test_failure", "verified"),
                ("targeted_test_failure", "targeted_test_failure"), ("regression_failure",)]
    return [evaluate_case(case(f"case-{i}"), synthetic_run(outcome)) for i, outcome in enumerate(outcomes)]


def test_exact_mixed_metrics():
    metrics = aggregate_metrics(mixed_results())
    assert (metrics.total_cases, metrics.completed_cases, metrics.verified_cases, metrics.failed_cases) == (5, 5, 3, 2)
    assert metrics.repair_success_rate == 3 / 5
    assert metrics.first_attempt_success_rate == 2 / 5
    assert metrics.self_correction_usage_rate == 2 / 5
    assert metrics.self_correction_recovery_rate == 1 / 2
    assert metrics.targeted_pass_rate == 4 / 5
    assert metrics.regression_pass_rate == 3 / 4
    assert metrics.integrity_pass_rate == metrics.cleanup_pass_rate == 1
    assert (metrics.total_attempts, metrics.mean_attempts, metrics.median_attempts, metrics.max_attempts) == (7, 1.4, 1, 2)
    assert (metrics.mean_changed_files, metrics.mean_additions, metrics.mean_deletions, metrics.mean_patch_size) == (1, 1, 1, 2)
    assert (metrics.mean_reproduction_duration_ms, metrics.mean_targeted_duration_ms,
            metrics.mean_regression_duration_ms, metrics.mean_verification_duration_ms) == (10, 28, 30, 52)
    assert metrics.mean_sample_counts["mean_regression_duration_ms"] == 4
    assert metrics.failure_counts_by_category[F.TARGETED_TEST_FAILURE] == 1
    assert metrics.failure_counts_by_category[F.REGRESSION_FAILURE] == 1
    assert sum(metrics.failure_counts_by_category.values()) == 2


def test_zero_denominators_and_excluded_cases():
    metrics = aggregate_metrics([])
    assert metrics.total_cases == 0 and metrics.total_attempts == 0
    assert all(getattr(metrics, name) is None for name in metrics.rate_denominators)
    assert all(getattr(metrics, name) is None for name in metrics.mean_sample_counts)
    assert metrics.median_attempts is metrics.max_attempts is None
    incomplete = synthetic_run()
    incomplete.completed_at = None
    valid = evaluate_case(case(), synthetic_run())
    metrics = aggregate_metrics([valid, evaluate_case(case("incomplete"), incomplete),
        valid.model_copy(update={"case_id": "skipped", "status": "SKIPPED", "verified": False})])
    assert metrics.total_cases == 3 and metrics.completed_cases == 1 and metrics.incomplete_cases == metrics.skipped_cases == 1
    assert metrics.repair_success_rate == 1 and metrics.self_correction_recovery_rate is None


@pytest.mark.parametrize("changes", [{"fixture_path": "../escape"}, {"fixture_path": "C:/Users/private"},
    {"fixture_path": "/tmp/outside"}, {"fixture_path": "broken_math/../other"}, {"fixture_path": "broken_math\\other"},
    {"expected_failing_tests": ["python -m pytest"]}, {"expected_failing_tests": ["test_x.py;whoami"]},
    {"expected_failing_tests": ["../test_x.py"]}, {"enabled": "false"}, {"command": "whoami"}, {"language": "javascript"}])
def test_strict_case_validation(changes):
    with pytest.raises(ValidationError):
        BenchmarkCase.model_validate({**case().model_dump(), **changes})


def test_duplicate_ids_rejected():
    with pytest.raises(ValidationError):
        BenchmarkManifest(name="duplicate", cases=[case(), case()])


def execute(cases, repair=None, **kwargs):
    async def fake(item, repository):
        return synthetic_run(case_id=item.id)
    return asyncio.run(run_benchmark(BenchmarkManifest(name="test", cases=cases), ROOT / "fixtures", repair or fake,
                                    mode="mock", run_id="test", generated_at=NOW, **kwargs))


def test_runner_order_selection_disabled_and_maximum():
    report = execute([case("c"), case("b"), case("a"), case("d", enabled=False)], selected_ids=["b", "c", "d"], max_cases=1)
    assert [r.case_id for r in report.case_results] == ["a", "b", "c", "d"]
    assert [r.skip_reason for r in report.case_results] == ["not_selected", None, "max_cases", "disabled"]
    assert report.metrics.completed_cases == 1 and report.metrics.skipped_cases == 3


@pytest.mark.parametrize("kwargs", [{"max_cases": 0}, {"max_cases": True}, {"selected_ids": ["unknown"]},
                                      {"selected_ids": ["a", "a"]}, {"baseline_commit": "bad"}])
def test_invalid_runner_controls_rejected_before_execution(kwargs):
    repair = AsyncMock()
    with pytest.raises(ValueError):
        execute([case("a")], repair, **kwargs)
    repair.assert_not_called()


def test_case_exception_preserves_previous_results_and_stops_safely():
    async def repair(item, repository):
        if item.id == "b":
            raise RuntimeError("NEBIUS_API_KEY=private C:\\Users\\private /tmp/ghostfix-verify-abc")
        return synthetic_run(case_id=item.id)
    report = execute([case("a"), case("b"), case("c")], repair)
    assert [r.status for r in report.case_results] == ["COMPLETED", "INCOMPLETE", "SKIPPED"]
    assert report.case_results[-1].skip_reason == "safety_stop"
    assert report.metrics.verified_cases == 1
    assert "private" not in render_json(report) and "NEBIUS_API_KEY" not in render_json(report)


@pytest.mark.parametrize("safety_failure", [False, True])
def test_continue_on_known_failure_only_when_safe(safety_failure):
    async def repair(item, repository):
        run = synthetic_run(("targeted_test_failure",))
        if safety_failure:
            run.final_verification.workspace_cleaned = False
        return run
    report = execute([case("a"), case("b")], repair)
    assert report.case_results[1].status == ("SKIPPED" if safety_failure else "COMPLETED")


def test_invalid_correction_retains_prior_evidence_without_misattribution():
    run = synthetic_run(("targeted_test_failure", "targeted_test_failure"))
    run.status = "proposal_failure"
    run.final_verification = run.attempts[0].verification
    last = run.attempts[-1]
    last.verification = None
    last.outcome = "proposal_failure"
    last.proposal_attempt.validation_result.valid = False
    result = evaluate_case(case(), run)
    assert result.status == "COMPLETED" and result.failure_category == F.INVALID_PROPOSAL
    assert result.targeted_passed is None and result.regression_passed is None
    assert result.total_verification_duration_ms == 20


def test_wrong_reproduction_cannot_verify_another_case():
    other = case().model_copy(update={"expected_failing_tests": ["test_other.py::test_other"]})
    result = evaluate_case(other, synthetic_run())
    assert not result.verified and result.failure_category == F.INCOMPLETE_EVIDENCE


def test_configured_secret_redacted_from_case_identifier():
    result = evaluate_case(case("known-secret"), synthetic_run(), secrets=("known-secret",))
    assert "known-secret" not in result.model_dump_json()


def test_fixture_link_escape_rejected(tmp_path, monkeypatch):
    # Simulate Windows reparse metadata without requiring symlink privileges.
    import stat
    linked = tmp_path / "broken_math"
    linked.mkdir()
    original = Path.lstat
    def lstat(path):
        if path == linked:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return original(path)
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="Linked fixture"):
        fixture_repository(tmp_path, case(), ())


def test_pipeline_adapter_calls_existing_components():
    run = synthetic_run()
    agent = SimpleNamespace(analyze=AsyncMock(return_value=run.initial_diagnosis))
    loop = SimpleNamespace(run=AsyncMock(return_value=run))
    repository = fixture_repository(ROOT / "fixtures", case(), ())
    assert asyncio.run(pipeline_repair(agent, loop)(case(), repository)) is run
    agent.analyze.assert_awaited_once_with(repository, ["calculator.py", "test_calculator.py"])
    loop.run.assert_awaited_once_with(run.initial_diagnosis, repository)


def test_json_round_trip_determinism_and_markdown_metrics():
    report = execute([case()])
    encoded = render_json(report)
    assert BenchmarkRun.model_validate_json(encoded) == report
    assert render_markdown(BenchmarkRun.model_validate_json(encoded)) == render_markdown(report)
    assert render_json(execute([case()])) == encoded
    md = render_markdown(report)
    for name, denominator in report.metrics.rate_denominators.items():
        value = getattr(report.metrics, name)
        assert f"| {name} | {'N/A' if value is None else f'{value:.2%}'} | {denominator} |" in md
    for name, count in report.metrics.mean_sample_counts.items():
        value = getattr(report.metrics, name)
        assert f"| {name} | {'N/A' if value is None else value} | {count} |" in md


def test_secrets_host_paths_and_metadata_do_not_decide_success():
    run = synthetic_run()
    run.run_id = "known-secret C:\\Users\\private /tmp/ghostfix-verify-abc"
    run.initial_diagnosis.model = "NEBIUS_API_KEY=credential"
    run.initial_diagnosis.diagnosis.root_cause_hypothesis = "unverified"
    poisoned = case().model_copy(update={"name": "known-secret C:\\Users\\private", "description": "VERIFIED"})
    result = evaluate_case(poisoned, run, secrets=("known-secret",))
    encoded = result.model_dump_json()
    assert result.verified
    assert all(value not in encoded for value in ("known-secret", "C:", "private", "/tmp/", "NEBIUS_API_KEY", "credential"))


def test_evaluator_is_pure_ignores_forged_proof_and_does_not_mutate(monkeypatch):
    import builtins
    import socket
    import subprocess
    from app.llm.nebius_client import NebiusClient
    run = synthetic_run(("targeted_test_failure",))
    proof = build_proof_of_fix(synthetic_run(), generated_at=NOW)
    before = copy.deepcopy((run.model_dump(), proof.model_dump()))
    blocked = Mock(side_effect=AssertionError("unexpected execution"))
    monkeypatch.setattr(builtins, "open", blocked)
    monkeypatch.setattr(Path, "open", blocked)
    monkeypatch.setattr(subprocess, "run", blocked)
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(NebiusClient, "__init__", blocked)
    first = evaluate_case(case(), run, proof)
    assert first == evaluate_case(case(), run, proof)
    assert not first.verified
    assert (run.model_dump(), proof.model_dump()) == before
    blocked.assert_not_called()


def test_mock_cli_artifacts_and_fixture_integrity(tmp_path, monkeypatch, capsys):
    from scripts import run_benchmark as script
    from app.agent.verification import fixture_snapshot
    repository = fixture_repository(ROOT / "fixtures", case(), ())
    before = fixture_snapshot(repository)
    # Keep fixture root actual while redirecting only output writes into pytest temporary directory.
    original_write = Path.write_text
    def write(path, data, **kwargs):
        return original_write(tmp_path / path.name, data, **kwargs)
    monkeypatch.setattr(Path, "write_text", write)
    monkeypatch.setattr("sys.argv", ["run_benchmark", "--manifest", str(ROOT / "benchmarks/manifest.json"),
                                   "--mode", "mock", "--case", "python-calculator-add-001"])
    assert script.main() == 0
    report = BenchmarkRun.model_validate_json((tmp_path / "benchmark-results.json").read_text(encoding="utf-8"))
    assert report.mode == "mock" and report.metrics.verified_cases == 1
    assert "MOCK" in (tmp_path / "benchmark-results.md").read_text(encoding="utf-8")
    assert before == fixture_snapshot(repository)
    assert "NEBIUS_API_KEY" not in capsys.readouterr().out
