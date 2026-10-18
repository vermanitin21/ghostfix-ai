"""Validate human-authored corpus references in Docker. Never instantiate an LLM."""
import json
from pathlib import Path

from app.core.config import get_settings
from app.evaluation.corpus import validate_corpus
from app.evaluation.models import BenchmarkManifest
from app.sandbox.runner import DockerPytestRunner


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        manifest = BenchmarkManifest.model_validate_json((root / "benchmarks/manifest.json").read_text(encoding="utf-8"))
        settings = get_settings()
        sandbox = DockerPytestRunner(settings.sandbox_image, settings.sandbox_timeout_seconds)
        result = validate_corpus(manifest, root / "fixtures", root / "benchmarks/reference_fixes", sandbox)
        artifacts = root / "artifacts"
        artifacts.mkdir(exist_ok=True)
        (artifacts / "corpus-validation.json").write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        for case in result.cases:
            print(case.model_dump_json())
        print(json.dumps({"cases": result.case_count, "valid": result.valid_count, "invalid": result.invalid_count,
            "originals_unchanged": all(c.original_unchanged for c in result.cases),
            "workspaces_cleaned": all(c.workspace_cleaned for c in result.cases),
            "corpus_fingerprint_sha256": result.corpus_fingerprint_sha256}, indent=2))
        return 0 if result.case_count and not result.invalid_count and result.corpus_fingerprint_sha256 else 1
    except (ValueError, OSError):
        print('{"error": "Corpus input or artifact operation failed; sensitive details omitted"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
