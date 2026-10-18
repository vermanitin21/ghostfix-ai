"""Controlled demo: inject one valid incorrect patch, then use real Nemotron correction."""

import asyncio
import difflib
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.agent.orchestrator import GhostFixOrchestrator
from app.agent.patches import validate_patch
from app.agent.repair import RepairLoop
from app.agent.state import PatchLimits, PatchProposal, PatchProposalAttempt, RepairRun
from app.agent.verification import PatchVerifier
from app.core.config import get_settings
from app.llm.nebius_client import NebiusClient
from app.sandbox.runner import DockerPytestRunner
from app.tools.files import RepositoryContext
from scripts.test_patch_proposal import snapshot


class DemoAgent(GhostFixOrchestrator):
    """Script-only injection; production RepairLoop has no bad-candidate switch."""
    async def propose_patch(self, diagnosis_attempt, repository, limits=None, *, correction=None):
        if correction is not None:
            return await super().propose_patch(diagnosis_attempt, repository, limits, correction=correction)
        limits = limits or PatchLimits()
        context = diagnosis_attempt.context
        source = next(file.content for file in context.files if file.path == "calculator.py").replace("\r\n", "\n")
        if source.count("return left - right") != 1:
            raise ValueError("Demo requires the known unchanged broken_math fixture")
        incorrect = source.replace("return left - right", "return left * right")
        proposal = PatchProposal(
            target_files=["calculator.py"], summary="DEMO: deliberately injected valid multiplication candidate; not model-generated",
            rationale="Controlled safe first candidate to demonstrate evidence-driven correction",
            unified_diff="".join(difflib.unified_diff(source.splitlines(keepends=True),
                incorrect.splitlines(keepends=True), fromfile="a/calculator.py", tofile="b/calculator.py")),
            expected_behavior_change="Multiplication returns 6 rather than the expected 5 for add(2, 3)",
            tests_to_run=["test_calculator.py::test_adds_two_numbers"],
            risk_notes=["Deliberately incorrect demo candidate; must fail verification"], confidence=0.0,
        )
        return PatchProposalAttempt(
            diagnosis_attempt=diagnosis_attempt, proposal=proposal, model="demo:injected-not-LLM",
            validation_result=validate_patch(proposal, context, repository.root, limits),
            completed_at=datetime.now(timezone.utc),
        )


class AuditedNebiusClient(NebiusClient):
    """Record a digest at the real client boundary, never print the request payload."""
    correction_request_sha256: str | None = None
    correction_response_model: str | None = None

    async def complete_structured(self, messages, schema, *, multiline=False):
        payload = json.loads(messages[-1]["content"])
        is_correction = "correction_evidence" in payload
        if is_correction:
            self.correction_request_sha256 = hashlib.sha256(messages[-1]["content"].encode()).hexdigest()
        response = await super().complete_structured(messages, schema, multiline=multiline)
        if is_correction:
            self.correction_response_model = response.model
        return response


async def run_demo() -> tuple[RepairRun, dict]:
    """Return the actual in-memory run and audit summary for integration callers."""
    settings = get_settings()
    if settings.nebius_model != "nvidia/Nemotron-3_5-Lightning":
        raise ValueError("Use the confirmed Nemotron model")
    root = Path(__file__).resolve().parents[1] / "fixtures" / "broken_math"
    before = snapshot(root)
    client = AuditedNebiusClient(settings)
    try:
        repository = RepositoryContext(root, (settings.nebius_api_key.get_secret_value(),))
        sandbox = DockerPytestRunner(settings.sandbox_image, settings.sandbox_timeout_seconds)
        agent = DemoAgent(sandbox, client)
        diagnosis = await agent.analyze(repository, ["calculator.py", "test_calculator.py"])
        run = await RepairLoop(agent, PatchVerifier(sandbox)).run(diagnosis, repository, PatchLimits(
            max_files=settings.patch_max_files, max_changed_lines=settings.patch_max_changed_lines,
            max_diff_bytes=settings.patch_max_diff_bytes,
        ))
        unchanged = before == snapshot(root)
        trajectory = len(run.attempts) == 2 and [item.outcome for item in run.attempts] == ["targeted_test_failure", "verified"]
        all_clean = all(item.verification and item.verification.workspace_cleaned
                        and item.verification.original_unchanged for item in run.attempts)
        final = run.final_verification
        verified = bool(run.verified and trajectory and unchanged and all_clean
                        and final and final.targeted_tests and final.targeted_tests.passed
                        and final.regression_tests and final.regression_tests.passed
                        and client.correction_response_model and client.correction_request_sha256)
        report = {
            "demo_first_candidate": "deliberately injected valid multiplication patch; not model-generated",
            "verified": verified, "attempt_count": len(run.attempts), "status": run.status,
            "attempts": [{
                "attempt": item.attempt_number, "verification_status": item.outcome,
                "verified": bool(item.verification and item.verification.verified),
                "targeted_exit_code": item.verification.targeted_tests.exit_code
                    if item.verification and item.verification.targeted_tests else None,
                "targeted_passed": item.verification.targeted_tests.passed
                    if item.verification and item.verification.targeted_tests else None,
                "regression_passed": item.verification.regression_tests.passed
                    if item.verification and item.verification.regression_tests else None,
                "validation_passed": bool(item.proposal_attempt.validation_result and item.proposal_attempt.validation_result.valid),
            } for item in run.attempts],
            "stop_reason": run.stop_reason, "original_hashes_unchanged": unchanged,
            "workspaces_cleaned": bool(all_clean),
            "correction_request_sha256": client.correction_request_sha256,
            "real_correction_model": client.correction_response_model,
        }
        return run, report
    finally:
        await client.close()


async def verify() -> int:
    _, report = await run_demo()
    key = get_settings().nebius_api_key.get_secret_value()
    print(json.dumps(report, indent=2).replace(key, "[REDACTED]"))
    return 0 if report["verified"] else 1


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        return asyncio.run(verify())
    except Exception:
        print(json.dumps({"verified": False, "error": "Self-correction demo failed; inspect configuration and services"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
