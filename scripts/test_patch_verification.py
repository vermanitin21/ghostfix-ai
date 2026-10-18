"""Real, single-candidate verification in a disposable workspace."""
import asyncio
import json
import logging
from pathlib import Path

from app.agent.orchestrator import GhostFixOrchestrator
from app.agent.state import PatchLimits
from app.agent.verification import PatchVerifier
from app.core.config import get_settings
from app.llm.nebius_client import NebiusClient
from app.sandbox.runner import DockerPytestRunner
from app.tools.files import RepositoryContext, redact
from scripts.test_patch_proposal import snapshot


async def verify() -> int:
    settings = get_settings()
    root = Path(__file__).resolve().parents[1] / "fixtures" / "broken_math"
    before = snapshot(root)
    client = NebiusClient(settings)
    try:
        secrets = (settings.nebius_api_key.get_secret_value(),)
        repository = RepositoryContext(root, secrets)
        sandbox = DockerPytestRunner(settings.sandbox_image, settings.sandbox_timeout_seconds)
        agent = GhostFixOrchestrator(sandbox, client)
        diagnosis = await agent.analyze(repository, ["calculator.py", "test_calculator.py"])
        proposal = await agent.propose_patch(diagnosis, repository, PatchLimits(
            max_files=settings.patch_max_files, max_changed_lines=settings.patch_max_changed_lines,
            max_diff_bytes=settings.patch_max_diff_bytes,
        ))
        result = await asyncio.to_thread(PatchVerifier(sandbox).verify, proposal, repository)
        summary = result.model_dump(exclude={"proposal_attempt", "targeted_tests", "regression_tests"})
        for stage in ("targeted_tests", "regression_tests"):
            tests = getattr(result, stage)
            summary[stage] = tests.model_dump(exclude={"evidence"}) if tests else None
        summary["proposal_error"] = proposal.error
        summary["validation_errors"] = proposal.validation_result.errors if proposal.validation_result else []
        summary["original_hashes_unchanged"] = before == snapshot(root)
        def safe(value):
            if isinstance(value, str):
                return redact(value, secrets)
            if isinstance(value, list):
                return [safe(item) for item in value]
            if isinstance(value, dict):
                return {key: safe(item) for key, item in value.items()}
            return value
        print(json.dumps(safe(summary), indent=2))
        return 0 if result.verified and summary["original_hashes_unchanged"] else 1
    finally:
        await client.close()


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        return asyncio.run(verify())
    except Exception:
        print(json.dumps({"verified": False, "error": "E2E verification failed; check configuration and services"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
