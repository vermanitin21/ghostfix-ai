"""Real Docker-to-Nemotron diagnosis check; run from the project root."""

import asyncio
import json
import logging
from pathlib import Path

from app.agent.orchestrator import GhostFixOrchestrator
from app.core.config import get_settings
from app.llm.nebius_client import NebiusClient
from app.sandbox.runner import DockerPytestRunner
from app.tools.files import RepositoryContext, redact


async def verify() -> int:
    settings = get_settings()
    if settings.nebius_model != "nvidia/Nemotron-3_5-Lightning":
        raise ValueError("Configure the confirmed Nemotron model")
    client = NebiusClient(settings)
    secrets = (settings.nebius_api_key.get_secret_value(),)
    try:
        repository = RepositoryContext(
            Path(__file__).resolve().parents[1] / "fixtures" / "broken_math", secrets,
        )
        orchestrator = GhostFixOrchestrator(
            DockerPytestRunner(settings.sandbox_image, settings.sandbox_timeout_seconds), client,
        )
        attempt = await orchestrator.analyze(repository, ["calculator.py", "test_calculator.py"])
        verified = (attempt.status == "diagnosed" and attempt.diagnosis is not None
                    and "calculator.py" in attempt.diagnosis.suspected_files)
        summary = {
            "verified": verified, "attempt_id": attempt.attempt_id,
            "model": attempt.model, "status": attempt.status, "error": attempt.error,
            "sandbox_exit_code": attempt.sandbox.exit_code,
            "diagnosis": attempt.diagnosis.model_dump() if attempt.diagnosis else None,
        }
        # Redact strings before JSON serialization to preserve valid JSON.
        def safe(value):
            if isinstance(value, str):
                return redact(value, secrets)
            if isinstance(value, dict):
                return {key: safe(item) for key, item in value.items()}
            if isinstance(value, list):
                return [safe(item) for item in value]
            return value
        print(json.dumps(safe(summary), indent=2))
        return 0 if verified else 1
    finally:
        await client.close()


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        return asyncio.run(verify())
    except Exception:
        print(json.dumps({"verified": False, "error": "Diagnosis verification failed; check configuration and services"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
