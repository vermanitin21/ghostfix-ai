"""Offline infrastructure smoke test. No implicit real/model execution mode."""
import argparse
import asyncio
from pathlib import Path

from app.evaluation.mock import NOW, synthetic_run
from app.evaluation.models import BenchmarkManifest
from app.evaluation.renderers import render_json, render_markdown
from app.evaluation.runner import run_benchmark


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("benchmarks/manifest.json"))
    parser.add_argument("--mode", choices=["mock"], default="mock")
    parser.add_argument("--case", action="append", dest="selected_ids")
    parser.add_argument("--max-cases", type=int, default=10)
    parser.add_argument("--baseline-commit")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    async def repair(case, repository):
        return synthetic_run(case_id=case.id)
    try:
        manifest = BenchmarkManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
        report = asyncio.run(run_benchmark(manifest, root / "fixtures", repair, mode="mock",
            selected_ids=args.selected_ids, max_cases=args.max_cases, baseline_commit=args.baseline_commit,
            run_id="mock-infrastructure", generated_at=NOW))
        artifacts = root / "artifacts"
        artifacts.mkdir(exist_ok=True)
        (artifacts / "benchmark-results.json").write_text(render_json(report), encoding="utf-8")
        (artifacts / "benchmark-results.md").write_text(render_markdown(report), encoding="utf-8")
        print(render_markdown(report))
        return 1 if report.metrics.incomplete_cases or report.metrics.failed_cases else 0
    except (ValueError, OSError):
        print("Benchmark input or artifact operation failed; sensitive details omitted.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
