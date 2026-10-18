"""Explicit observed-sample denominators. Incomplete and skipped cases are excluded."""
from statistics import mean, median

from .models import BenchmarkCaseResult, BenchmarkMetrics, FailureCategory


def aggregate_metrics(results: list[BenchmarkCaseResult]) -> BenchmarkMetrics:
    completed = [r for r in results if r.status == "COMPLETED"]
    successful = [r for r in completed if r.verified]
    retried = [r for r in completed if r.self_correction_used]
    denominators, rates, counts, means = {}, {}, {}, {}
    samples = {
        "repair_success_rate": (completed, "verified"),
        "first_attempt_success_rate": (completed, "first_attempt_success"),
        "self_correction_usage_rate": (completed, "self_correction_used"),
        "self_correction_recovery_rate": (retried, "self_correction_recovered"),
        **{f"{stage}_pass_rate": ([r for r in completed if getattr(r, f"{stage}_passed") is not None], f"{stage}_passed")
           for stage in ("targeted", "regression", "integrity", "cleanup")},
    }
    for name, (rows, attribute) in samples.items():
        denominators[name] = len(rows)
        rates[name] = sum(getattr(r, attribute) is True for r in rows) / len(rows) if rows else None
    for name, attribute, rows in [
        ("mean_attempts", "attempt_count", completed),
        ("mean_changed_files", "changed_file_count", successful),
        ("mean_additions", "additions", successful), ("mean_deletions", "deletions", successful),
        ("mean_patch_size", "patch_size", successful),
        ("mean_reproduction_duration_ms", "reproduction_duration_ms", completed),
        ("mean_targeted_duration_ms", "targeted_duration_ms", completed),
        ("mean_regression_duration_ms", "regression_duration_ms", completed),
        ("mean_verification_duration_ms", "total_verification_duration_ms", completed),
    ]:
        values = [getattr(r, attribute) for r in rows if getattr(r, attribute) is not None]
        counts[name], means[name] = len(values), float(mean(values)) if values else None
    attempts = [r.attempt_count for r in completed]
    failures = {category.value: sum(r.failure_category == category for r in results if r.status != "SKIPPED" and not r.verified)
                for category in FailureCategory if category != FailureCategory.NONE}
    return BenchmarkMetrics(total_cases=len(results), completed_cases=len(completed), verified_cases=len(successful),
        failed_cases=len(completed) - len(successful), incomplete_cases=sum(r.status == "INCOMPLETE" for r in results),
        skipped_cases=sum(r.status == "SKIPPED" for r in results), **rates, rate_denominators=denominators,
        total_attempts=sum(attempts), median_attempts=float(median(attempts)) if attempts else None,
        max_attempts=max(attempts) if attempts else None, **means, mean_sample_counts=counts,
        failure_counts_by_category=failures)
