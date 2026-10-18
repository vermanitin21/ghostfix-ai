"""Real proposal check. Never write to the fixture or execute a candidate patch."""

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from app.agent.orchestrator import GhostFixOrchestrator
from app.agent.state import PatchLimits
from app.core.config import get_settings
from app.llm.nebius_client import NebiusClient
from app.sandbox.runner import DockerPytestRunner
from app.tools.files import RepositoryContext, redact


def snapshot(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


async def verify() -> int:
    settings = get_settings()
    if settings.nebius_model != "nvidia/Nemotron-3_5-Lightning":
        raise ValueError("Configure the confirmed model")
    root = Path(__file__).resolve().parents[1] / "fixtures" / "broken_math"
    before = snapshot(root)
    client = NebiusClient(settings)
    try:
        secrets = (settings.nebius_api_key.get_secret_value(),)
        repository = RepositoryContext(root, secrets)
        agent = GhostFixOrchestrator(
            DockerPytestRunner(settings.sandbox_image, settings.sandbox_timeout_seconds), client,
        )
        diagnosis = await agent.analyze(repository, ["calculator.py", "test_calculator.py"])
        attempt = await agent.propose_patch(diagnosis, repository, PatchLimits(
            max_files=settings.patch_max_files, max_changed_lines=settings.patch_max_changed_lines,
            max_diff_bytes=settings.patch_max_diff_bytes,
        ))
    finally:
        await client.close()
        unchanged = before == snapshot(root)
    verified = bool(
        unchanged and attempt.proposal and attempt.validation_result and attempt.validation_result.valid
        and "calculator.py" in attempt.proposal.target_files
    )
    print(json.dumps({
        "verified": verified, "fixture_unchanged": unchanged, "attempt_id": attempt.attempt_id,
        "model": redact(attempt.model or "", secrets),
        "error": attempt.error or diagnosis.error,
        "validation": attempt.validation_result.model_dump() if attempt.validation_result else None,
        "summary": redact(attempt.proposal.summary, secrets) if attempt.proposal else None,
    }, indent=2))
    if attempt.proposal:
        print(redact(attempt.proposal.unified_diff, secrets))
    return 0 if verified else 1


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        return asyncio.run(verify())
    except Exception:
        print(json.dumps({"verified": False, "error": "Proposal verification failed; check configuration and services"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
