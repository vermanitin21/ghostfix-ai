"""Coordinates reproduction, diagnosis and candidate proposals; never applies patches."""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from openai import APIStatusError

from app.agent.prompts import DIAGNOSIS_SYSTEM
from app.agent.prompts import PATCH_SYSTEM
from app.agent.prompts import CORRECTION_SYSTEM
from app.agent.prompts import DIAGNOSIS_CONCISE_RETRY, FORMAT_RETRY_SYSTEM
from app.agent.state import CorrectionEvidence
from app.agent.patches import normalize_context_prefixes, patched_contents
from app.agent.protocol import format_retry_witness, safe_protocol_record
from app.agent.state import PatchLimits, PatchProposal, PatchProposalAttempt, PatchValidationResult
from app.agent.state import (Diagnosis, ConciseDiagnosis, DiagnosisAttempt, DiagnosisInput,
                             DiagnosisProtocolEvent, PatchProtocolEvent)
from app.llm.nebius_client import StructuredLLMClient, StructuredOutputError, OutputBudgetExceeded
from app.tools.files import RepositoryContext, redact

from app.sandbox.models import SandboxResult
from app.sandbox.runner import DockerPytestRunner


class GhostFixOrchestrator:
    def __init__(self, sandbox: DockerPytestRunner, llm: StructuredLLMClient | None = None) -> None:
        self._sandbox = sandbox
        self._llm = llm

    def reproduce(self, project_dir: Path) -> SandboxResult:
        """Run a repository's tests and return the observed result."""
        return self._sandbox.run(project_dir)

    def reproduce_failure(self, repository: RepositoryContext) -> SandboxResult:
        return self.reproduce(repository.root)

    def gather_context(
        self, repository: RepositoryContext, evidence: SandboxResult, paths: list[str],
    ) -> DiagnosisInput:
        def excerpt(text: str) -> str:
            safe = redact(text, repository.secrets)
            return safe if len(safe) <= 2000 else safe[:1000] + "\n[truncated]\n" + safe[-980:]

        return DiagnosisInput(
            exit_code=evidence.exit_code,
            failed_tests=[excerpt(test)[:300] for test in evidence.failed_tests[:10]],
            stdout_excerpt=excerpt(evidence.stdout), stderr_excerpt=excerpt(evidence.stderr),
            files=repository.read_files(paths),
        )

    async def diagnose_failure(self, context: DiagnosisInput, *,
                               protocol: DiagnosisAttempt | None = None) -> tuple[str, Diagnosis]:
        if self._llm is None:
            raise ValueError("A structured LLM client is required")
        evidence = context.model_dump_json()
        for number in (1, 2):
            event = DiagnosisProtocolEvent(call_number=number, outcome="started")
            if protocol is not None:
                protocol.protocol_events.append(event)
            try:
                async with asyncio.timeout(65):
                    response = await self._llm.complete_structured([
                        {"role": "system", "content": DIAGNOSIS_SYSTEM + (DIAGNOSIS_CONCISE_RETRY if number == 2 else "")},
                        {"role": "user", "content": evidence},
                    ], ConciseDiagnosis)
                diagnosis = Diagnosis.model_validate(ConciseDiagnosis.model_validate_json(response.content).model_dump())
                allowed = {file.path for file in context.files}
                if not set(diagnosis.suspected_files).issubset(allowed):
                    raise ValueError("Diagnosis cited a file outside the supplied context")
                event.outcome, event.model = "accepted", response.model
                break
            except OutputBudgetExceeded:
                event.outcome = "output_budget"
                if number == 2:
                    raise
                if protocol is not None:
                    protocol.protocol_retry_count = 1
            except Exception:
                event.outcome = "error"
                raise
        return response.model, diagnosis

    async def analyze(self, repository: RepositoryContext, paths: list[str]) -> DiagnosisAttempt:
        evidence = await asyncio.to_thread(self.reproduce_failure, repository)
        attempt = DiagnosisAttempt(sandbox=evidence)
        if evidence.exit_code != 1 or evidence.timed_out or evidence.infrastructure_error:
            attempt.status, attempt.error = "failed", "Sandbox did not reproduce a test failure"
            return attempt
        try:
            attempt.context = self.gather_context(repository, evidence, paths)
            attempt.model, attempt.diagnosis = await self.diagnose_failure(attempt.context, protocol=attempt)
            attempt.status = "diagnosed"
        except StructuredOutputError as error:
            attempt.status, attempt.error = "failed", str(error)
        except APIStatusError as error:
            attempt.status, attempt.error = "failed", f"Nebius API error (HTTP {error.status_code})"
        except Exception as error:
            # Never retain validation inputs or API error bodies containing possible secrets.
            attempt.status, attempt.error = "failed", f"Context collection or structured diagnosis failed ({type(error).__name__})"
        return attempt

    async def propose_patch(
        self, diagnosis_attempt: DiagnosisAttempt, repository: RepositoryContext,
        limits: PatchLimits | None = None,
        *, correction: CorrectionEvidence | None = None,
    ) -> PatchProposalAttempt:
        limits = limits or PatchLimits()
        attempt = PatchProposalAttempt(diagnosis_attempt=diagnosis_attempt)
        try:
            if (diagnosis_attempt.status != "diagnosed" or diagnosis_attempt.diagnosis is None
                    or diagnosis_attempt.context is None or self._llm is None):
                raise ValueError("A validated diagnosis and context are required")
            context = diagnosis_attempt.context
            current = repository.read_files([file.path for file in context.files])
            if current != context.files:
                raise ValueError("Repository context changed since diagnosis")
            payload_data = {
                "evidence_and_context": context.model_dump(),
                "diagnosis": diagnosis_attempt.diagnosis.model_dump(), "limits": limits.model_dump(),
                "numbered_lines": {file.path: [f"{number}: {line}" for number, line
                                               in enumerate(file.content.splitlines(), 1)]
                                   for file in context.files},
            }
            if correction is not None:
                payload_data["correction_evidence"] = correction.model_dump()
            # Redact all strings, including prior model output, before serialization.
            def sanitize(value):
                if isinstance(value, str):
                    return redact(value, repository.secrets)
                if isinstance(value, list):
                    return [sanitize(item) for item in value]
                if isinstance(value, dict):
                    return {key: sanitize(item) for key, item in value.items()}
                return value
            payload = json.dumps(sanitize(payload_data))
            if len(payload.encode("utf-8")) > 60000:
                raise ValueError("Proposal input exceeds bounded request budget")
            system = CORRECTION_SYSTEM if correction else PATCH_SYSTEM
            expected_intent = None
            for number in (1, 2):
                if repository.read_files([file.path for file in context.files]) != context.files:
                    raise ValueError("Repository context changed before proposal call")
                attempt.protocol_retry_count = number - 1
                async with asyncio.timeout(65):
                    response = await self._llm.complete_structured([
                        {"role": "system", "content": system},
                        {"role": "user", "content": payload},
                    ], PatchProposal, multiline=True)
                raw = PatchProposal.model_validate_json(response.content)
                candidate, validation, normalized = normalize_context_prefixes(raw, context, repository.root, limits)
                safe = safe_protocol_record(candidate.model_dump(), repository.secrets)
                if safe != candidate.model_dump():
                    validation = PatchValidationResult(valid=False, errors=["Sensitive or absolute-path response content"],
                        limits=limits, failure_category="SECURITY_POLICY_ERROR")
                if repository.read_files([file.path for file in context.files]) != context.files:
                    raise ValueError("Repository context changed during proposal generation")
                witness = None
                if not validation.valid and safe == candidate.model_dump():
                    witness = format_retry_witness(raw, context, repository.root, limits)
                    if witness is not None:
                        validation = validation.model_copy(update={"failure_category": "RETRYABLE_FORMAT_ERROR"})
                if expected_intent is not None and validation.valid:
                    if (set(candidate.target_files) != set(expected_intent.target_files)
                            or candidate.tests_to_run != expected_intent.tests_to_run
                            or patched_contents(candidate, context, repository.root, limits)
                            != patched_contents(expected_intent, context, repository.root, limits)):
                        validation = PatchValidationResult(valid=False, errors=["Protocol retry changed repair intent"],
                            limits=limits, failure_category="SEMANTICALLY_AMBIGUOUS_PATCH")
                attempt.model = safe_protocol_record(response.model, repository.secrets)
                attempt.proposal = PatchProposal.model_validate(safe)
                attempt.validation_result = validation
                attempt.protocol_events.append(PatchProtocolEvent(
                    call_number=number, model=attempt.model,
                    raw_response_sha256=hashlib.sha256(response.content.encode("utf-8")).hexdigest(),
                    raw_model_diff=safe_protocol_record(raw.unified_diff, repository.secrets),
                    normalized_diff=safe_protocol_record(candidate.unified_diff, repository.secrets) if normalized else None,
                    normalization_reason="source_proven_context_prefix" if normalized else None,
                    validation=validation))
                if validation.valid or witness is None or number == 2:
                    break
                expected_intent = witness
                # No diagnosis, sandbox logs or correction/test-verification evidence
                # enters the format retry. Intent is proven locally before retrying.
                payload = json.dumps(safe_protocol_record({
                    "previous_proposal": raw.model_dump(),
                    "validation_feedback": ["RETRYABLE_FORMAT_ERROR", *[error[:200] for error in validation.errors[:2]]],
                    "original_files": [file.model_dump() for file in context.files],
                    "limits": limits.model_dump(),
                }, repository.secrets))
                if len(payload.encode("utf-8")) > 60000:
                    raise ValueError("Protocol retry input exceeds bounded request budget")
                system = FORMAT_RETRY_SYSTEM
        except Exception as error:
            attempt.error = (str(error) if isinstance(error, StructuredOutputError)
                             else f"Patch proposal failed ({type(error).__name__})")
            attempt.validation_result = PatchValidationResult(valid=False, errors=[attempt.error], limits=limits)
        finally:
            attempt.completed_at = datetime.now(timezone.utc)
        return attempt
