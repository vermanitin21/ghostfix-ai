"""Deterministic output from typed metrics; Markdown never recomputes rates."""
import html
import json

from .models import BenchmarkRun


def render_json(run: BenchmarkRun) -> str:
    return json.dumps(run.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_markdown(run: BenchmarkRun) -> str:
    def text(value):
        return html.escape(str(value)).replace("|", "&#124;").replace("`", "&#96;").replace("\n", " ")
    def number(value):
        return "N/A" if value is None else str(value)
    metrics = run.metrics
    lines = ["# GhostFix Benchmark Report", "", f"Mode: {run.mode}", "", "## Summary", "",
             f"Cases: {metrics.total_cases}; completed: {metrics.completed_cases}; verified: {metrics.verified_cases}; "
             f"failed: {metrics.failed_cases}; incomplete: {metrics.incomplete_cases}; skipped: {metrics.skipped_cases}",
             "", "## Rates", "", "| Metric | Rate | Denominator |", "| --- | ---: | ---: |"]
    for name, denominator in sorted(metrics.rate_denominators.items()):
        value = getattr(metrics, name)
        lines.append(f"| {name} | {'N/A' if value is None else f'{value:.2%}'} | {denominator} |")
    lines += ["", "## Repair Efficiency", "", f"Total attempts: {metrics.total_attempts}",
              f"Median attempts: {number(metrics.median_attempts)}; maximum: {number(metrics.max_attempts)}", "",
              "| Mean | Value | Samples |", "| --- | ---: | ---: |"]
    for name, count in sorted(metrics.mean_sample_counts.items()):
        lines.append(f"| {name} | {number(getattr(metrics, name))} | {count} |")
    lines += ["", "## Failure Breakdown", ""]
    lines += [f"- {category}: {count}" for category, count in sorted(metrics.failure_counts_by_category.items()) if count]
    if not any(metrics.failure_counts_by_category.values()):
        lines.append("None.")
    lines += ["", "## Cases", "", "| Case | Result | Verified | Attempts | Target | Regression | Failure |",
              "| --- | --- | --- | ---: | --- | --- | --- |"]
    for result in run.case_results:
        lines.append(f"| {text(result.case_id)} | {result.status} | {result.verified} | {result.attempt_count} | "
                     f"{number(result.targeted_passed)} | {number(result.regression_passed)} | {result.failure_category.value} |")
    lines += ["", "## Limitations", ""] + [f"- {text(item)}" for item in run.limitations]
    return "\n".join(lines) + "\n"
