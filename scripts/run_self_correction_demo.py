"""GhostFix AI hackathon demo: verified, evidence-driven self-correction.

DEMO-ONLY: candidate 1 is the script-only DemoAgent's deliberately injected, valid
but wrong patch. Docker execution must make it fail. Candidate 2 must come from a
real Nemotron correction request built from candidate 1's executed evidence and pass
normal verification. Production code has no injection switch; this is a demo, not
benchmark evidence.

    .\\.venv\\Scripts\\python.exe scripts/run_self_correction_demo.py
"""
import argparse
import asyncio
import hashlib
import json
import logging
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from app.agent.repair import RepairLoop
from app.agent.state import PatchLimits, RepairRun
from app.agent.verification import PatchVerifier, fixture_snapshot
from app.core.config import get_settings
from app.evaluation.corpus import corpus_fingerprint
from app.evaluation.models import BenchmarkManifest
from app.llm.nebius_client import NebiusClient
from app.reporting import build_proof_of_fix, render_json, render_markdown
from app.reporting.models import ProofOfFix, ProofTest
from app.sandbox.runner import DockerPytestRunner
from app.tools.files import RepositoryContext
from scripts.evidence_fidelity import raw_record, reasons, sanitize, sha
from scripts.test_self_correction import DemoAgent

MODEL = "nvidia/Nemotron-3_5-Lightning"
DEMO_MODEL = "demo:injected-not-LLM"  # set only by the script-only DemoAgent
FIXTURE = ROOT / "fixtures" / "broken_math"
DEFAULT_OUTPUT = ROOT / "artifacts" / "hackathon-demo"
PATHS = ["calculator.py", "test_calculator.py"]
# Locked official results; never recomputed or merged with this demo.
BENCHMARKS = [("v1", 1, 7), ("v2", 3, 7), ("v3", 4, 7)]


class _RequestProxy:
    """Observe exact SDK options and API metadata without changing requests or responses."""
    def __init__(self, sdk, audit):
        self.sdk, self.audit = sdk, audit

    def with_options(self, **options):
        copied = self.sdk.with_options(**options)
        event = self.audit.events[-1]
        event["sdk_options"] = options

        async def create(**kwargs):
            event["request_options"] = {k: v for k, v in kwargs.items() if k != "messages"}
            response = await copied.chat.completions.create(**kwargs)
            event["api_model"] = response.model
            event["api_choices"] = [{"finish_reason": c.finish_reason, "refusal": c.message.refusal}
                                    for c in response.choices]
            event["usage"] = response.usage.model_dump() if response.usage else None
            return response

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    async def close(self):
        await self.sdk.close()


class AuditedClient:
    """Record every model call at the client boundary using the benchmark fidelity policy."""
    def __init__(self, client, secrets: tuple[str, ...]):
        self.client, self.secrets, self.events = client, secrets, []
        sdk = getattr(client, "_client", None)
        if sdk is not None and hasattr(sdk, "with_options"):
            client._client = _RequestProxy(sdk, self)

    async def complete_structured(self, messages, schema, *, multiline=False):
        payload = json.loads(messages[-1]["content"])
        stage = ("diagnosis" if schema.__name__ == "ConciseDiagnosis" else
                 "format_retry" if "previous_proposal" in payload else
                 "correction" if "correction_evidence" in payload else "proposal")
        event = {"call_number": len(self.events) + 1, "stage": stage, "schema": schema.__name__,
                 "requested_model": MODEL, "request_user_content_sha256": sha(messages[-1]["content"]),
                 "messages": list(messages), "status": "started"}
        self.events.append(event)
        try:
            response = await self.client.complete_structured(messages, schema, multiline=multiline)
            event.update(status="returned", returned_model=response.model,
                         response_evidence=raw_record(response.content, self.secrets))
            return response
        except Exception as error:
            event.update(status="error", error_type=type(error).__name__)
            raise

    async def close(self):
        await self.client.close()


def reserve_output(output: Path) -> Path:
    """Fresh evidence only: refuse to reuse or overwrite any existing location."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(exist_ok=False)
    return output


def preflight(settings, output: Path, docker_ok) -> list[str]:
    problems = []
    if settings.nebius_model != MODEL:
        problems.append(f"NEBIUS_MODEL must be {MODEL}")
    if settings.nebius_api_key is None or not settings.nebius_api_key.get_secret_value().strip():
        problems.append("NEBIUS_API_KEY is not configured")
    if output.exists():
        problems.append("Output directory already exists; refusing to overwrite demo evidence")
    if not problems and not docker_ok(settings.sandbox_image):
        problems.append("Docker or the sandbox image is unavailable")
    return problems


def docker_available(image: str) -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", image], capture_output=True,
                              timeout=30, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


async def execute(llm, sandbox, fixture: Path, secrets: tuple[str, ...], limits: PatchLimits):
    """Existing components only: DemoAgent -> RepairLoop -> PatchVerifier."""
    repository = RepositoryContext(fixture, secrets)
    before = fixture_snapshot(repository)
    agent = DemoAgent(sandbox, llm)
    diagnosis = await agent.analyze(repository, PATHS)
    run = await RepairLoop(agent, PatchVerifier(sandbox)).run(diagnosis, repository, limits)
    after = fixture_snapshot(repository)
    hashes = lambda files: {k: hashlib.sha256(v).hexdigest() for k, v in sorted(files.items())}
    return run, hashes(before), hashes(after)


def _diff_lines(diff: str | None, prefix: str) -> set[str]:
    return {line[1:] for line in (diff or "").splitlines()
            if line.startswith(prefix) and not line.startswith(prefix * 3)}


def _key_lines(test: ProofTest | None, limit: int = 8) -> list[str]:
    if test is None:
        return []
    lines = [line for line in (test.stdout_excerpt + "\n" + test.stderr_excerpt).splitlines()
             if re.match(r"\s*(E\s|FAILED|assert|AssertionError|\w+Error)", line)]
    return lines[:limit]


def _test_view(test: ProofTest | None) -> dict | None:
    if test is None:
        return None
    return {"result": "PASS" if test.passed else "FAIL", "exit_code": test.exit_code,
            "pytest_argv": test.pytest_argv, "failed_tests": test.failed_tests, "key_output": _key_lines(test)}


def summarize(run: RepairRun, proof: ProofOfFix, events: list[dict], original_unchanged: bool) -> dict:
    """Pure projection of the actual run; every status is derived, none asserted."""
    diagnosis = run.initial_diagnosis
    original = {f.path: f.content.replace("\r\n", "\n") for f in (diagnosis.context.files if diagnosis.context else [])}
    attempts = []
    for item, shown in zip(run.attempts, proof.repair_trajectory):
        proposal = item.proposal_attempt
        protocol = proposal.protocol_events
        verification = item.verification
        injected = proposal.model == DEMO_MODEL
        attempts.append({
            "attempt": item.attempt_number,
            "candidate_source": ("DEMO-INJECTED: deliberately wrong, not model-generated" if injected
                                 else f"model: {proposal.model}"),
            "demo_injected": injected, "model": proposal.model,
            "generated_from_execution_evidence": item.correction_evidence is not None,
            "summary": shown.summary, "proposal_valid": shown.proposal_valid,
            "candidate_diff": shown.candidate_diff,
            "raw_model_diff": protocol[0].raw_model_diff if protocol else None,
            "raw_strictly_valid": protocol[0].raw_validation.valid if protocol and protocol[0].raw_validation else None,
            "recovery_transformations": [t.category for e in protocol for t in e.transformations],
            "normalized_diff": next((e.normalized_diff for e in protocol if e.normalized_diff), None),
            "protocol_retries": proposal.protocol_retry_count,
            "applied_diff_against_original": verification.diff_after_apply if verification else None,
            "same_original_context": bool(proposal.diagnosis_attempt.context == diagnosis.context),
            "verification_status": shown.verification_status,
            "targeted": _test_view(shown.targeted), "regression": _test_view(shown.regression),
            "workspace_cleaned": verification.workspace_cleaned if verification else None,
            "original_unchanged": verification.original_unchanged if verification else None,
        })
    first = attempts[0] if attempts else None
    second = attempts[1] if len(attempts) > 1 else None
    evidence = run.attempts[1].correction_evidence if len(run.attempts) > 1 else None
    calls = [e for e in events if e["stage"] == "correction"]
    request = json.loads(calls[0]["messages"][-1]["content"]) if calls else {}
    sent = request.get("correction_evidence") or {}
    correction = {
        "occurred": evidence is not None and bool(calls),
        "model_calls": len(calls),
        "model": calls[0].get("returned_model") if calls else None,
        "request_sha256": calls[0]["request_user_content_sha256"] if calls else None,
        "evidence_fields": sorted(sent) if sent else [],
        "previous_verification_status": evidence.verification_status if evidence else None,
        "targeted_exit_code": evidence.targeted_tests.exit_code if evidence and evidence.targeted_tests else None,
        "failed_tests": evidence.targeted_tests.failed_tests if evidence and evidence.targeted_tests else [],
        "request_contains_attempt_1_evidence": bool(
            evidence and sent and sent.get("verification_status") == evidence.verification_status
            and (sent.get("targeted_tests") or {}).get("exit_code") == evidence.targeted_tests.exit_code
            and sent.get("previous_proposal", {}).get("unified_diff") == run.attempts[0].proposal_attempt.proposal.unified_diff),
    }
    # Candidate 2 must remove original lines, never candidate 1's additions.
    original_lines = {line for text in original.values() for line in text.splitlines()}
    candidate_one_only = _diff_lines(first["candidate_diff"], "+") - original_lines if first else set()
    removed = _diff_lines(second["applied_diff_against_original"], "-") if second else set()
    reset = bool(second and second["same_original_context"] and removed
                 and removed <= original_lines and not removed & candidate_one_only)
    verified = proof.verified and proof.status == "VERIFIED"
    self_corrected = bool(
        verified and len(attempts) == 2 and first["demo_injected"] and not second["demo_injected"]
        and second["model"] == MODEL and first["verification_status"] == "targeted_test_failure"
        and first["targeted"] and first["targeted"]["result"] == "FAIL" and first["targeted"]["exit_code"] == 1
        and correction["occurred"] and correction["request_contains_attempt_1_evidence"] and reset
        and second["targeted"] and second["targeted"]["result"] == "PASS"
        and second["regression"] and second["regression"]["result"] == "PASS")
    reproduced = proof.reproduction.reproduced
    return {
        "run_id": run.run_id, "demo_only": True, "benchmark_evidence": False,
        "proof_status": proof.status, "verified": verified, "self_correction_demonstrated": self_corrected,
        "reproduction": {"reproduced": reproduced, "exit_code": proof.reproduction.exit_code,
                         "failed_tests": proof.reproduction.failed_tests,
                         "buggy_source": original.get("calculator.py")},
        "diagnosis": {"status": diagnosis.status, "model": diagnosis.model,
                      "model_hypothesis": proof.diagnosis.root_cause_hypothesis if proof.diagnosis else None,
                      "summary": proof.diagnosis.summary if proof.diagnosis else None},
        "attempts": attempts, "correction": correction, "candidate_2_from_original_source": reset,
        "integrity": {"original_unchanged": original_unchanged and bool(proof.integrity.original_repository_unchanged),
                      "workspaces_cleaned": bool(proof.integrity.workspace_cleaned)},
        "model_calls": {stage: sum(e["stage"] == stage for e in events)
                        for stage in ("diagnosis", "proposal", "correction", "format_retry")},
        "evidence_issues": proof.verification.evidence_issues,
    }


def _verdict(view: dict | None) -> str:
    return "NOT RUN" if view is None else "PASSED" if view["result"] == "PASS" else "FAILED"


def terminal_summary(summary: dict) -> str:
    first = summary["attempts"][0] if summary["attempts"] else None
    second = summary["attempts"][1] if len(summary["attempts"]) > 1 else None
    rows = [
        ("Reproduction", "FAIL reproduced" if summary["reproduction"]["reproduced"] else "NOT reproduced"),
        ("Diagnosis", "complete" if summary["diagnosis"]["status"] == "diagnosed" else "failed"),
        ("Candidate 1", ("demo-injected; " if first and first["demo_injected"] else "") +
         (f"verification {_verdict(first['targeted'])}" if first else "not produced")),
        ("Correction", "generated from execution evidence" if summary["correction"]["occurred"] else "not performed"),
        ("Candidate 2", f"verification {_verdict(second['targeted'])}" if second else "not produced"),
        ("Regression", _verdict(second["regression"]) if second else "NOT RUN"),
        ("Proof of Fix", summary["proof_status"]),
    ]
    lines = ["GhostFix AI - Verified Repair (DEMO: candidate 1 deliberately injected)", ""]
    lines += [f"{name} {'.' * (20 - len(name))} {value}" for name, value in rows]
    lines += ["", "Self-correction demonstrated: " + ("YES" if summary["self_correction_demonstrated"] else "NO")]
    return "\n".join(lines)


def _block(text: str, language: str = "text") -> str:
    fence = "`" * max(3, max((len(m) for m in re.findall(r"`+", text)), default=0) + 1)
    return f"{fence}{language}\n{text.rstrip(chr(10))}\n{fence}"


def render_demo(summary: dict) -> str:
    first = summary["attempts"][0] if summary["attempts"] else None
    second = summary["attempts"][1] if len(summary["attempts"]) > 1 else None
    out = ["# GhostFix AI - Verified Self-Correction", "",
           f"**Result: {summary['proof_status']}** - self-correction demonstrated: "
           f"**{'YES' if summary['self_correction_demonstrated'] else 'NO'}**  ",
           f"Run `{summary['run_id']}` - demo only, not benchmark evidence. Candidate 1 is deliberately injected.", "",
           "## 1. Bug reproduced (executed in Docker)",
           f"Status: **{'FAIL reproduced' if summary['reproduction']['reproduced'] else 'NOT reproduced'}** "
           f"(pytest exit {summary['reproduction']['exit_code']})",
           _block("\n".join(summary["reproduction"]["failed_tests"]) or "No failed test IDs."),
           _block(summary["reproduction"]["buggy_source"] or "Source unavailable.", "python"), "",
           "## 2. Root cause - *model hypothesis, not proof*",
           _block(summary["diagnosis"]["model_hypothesis"] or "Diagnosis unavailable."),
           f"Model: `{summary['diagnosis']['model']}`", ""]
    if first:
        out += ["## 3. Candidate 1 - " + first["candidate_source"],
                _block(first["candidate_diff"] or "", "diff"),
                f"**TARGETED TEST: {first['targeted']['result'] if first['targeted'] else 'NOT RUN'}**"
                + (f" (exit {first['targeted']['exit_code']})" if first["targeted"] else "")]
        if first["targeted"]:
            out += [_block("\n".join(first["targeted"]["failed_tests"] + first["targeted"]["key_output"]) or "No output.")]
        out.append("")
    c = summary["correction"]
    out += ["## 4. Autonomous correction",
            ("GhostFix fed Candidate 1's executed failure evidence (verification status, pytest exit code, failed "
             "test IDs, bounded output and the failed patch) back into a new model request. No human edited anything."
             if c["occurred"] else "No correction request was made."),
            f"Evidence fields sent: `{', '.join(c['evidence_fields']) or 'none'}`  ",
            f"Request contains Candidate 1 evidence: **{'yes' if c['request_contains_attempt_1_evidence'] else 'no'}**  ",
            f"Correction request SHA-256: `{c['request_sha256']}` - model: `{c['model']}`", ""]
    if second:
        recovery = ", ".join(second["recovery_transformations"]) or "none (raw diff valid as written)"
        out += ["## 5. Candidate 2 - " + second["candidate_source"],
                _block(second["candidate_diff"] or "", "diff"),
                f"Applied to a fresh copy of the ORIGINAL source: **{'yes' if summary['candidate_2_from_original_source'] else 'NOT CONFIRMED'}**  ",
                f"Protocol recovery: {recovery}; protocol retries: {second['protocol_retries']}", "",
                f"**TARGETED TEST: {second['targeted']['result'] if second['targeted'] else 'NOT RUN'}**  ",
                f"**REGRESSION TESTS: {second['regression']['result'] if second['regression'] else 'NOT RUN'}**", ""]
    integrity = summary["integrity"]
    tick = lambda ok: "PASS" if ok else "FAIL"
    out += ["## 6. Proof of Fix", f"**{summary['proof_status']}**", "",
            "| Executed evidence | Result |", "| --- | --- |",
            f"| Original failure reproduced | {tick(summary['reproduction']['reproduced'])} |",
            f"| Candidate 1 genuinely failed in Docker | {tick(bool(first and first['targeted'] and first['targeted']['result'] == 'FAIL'))} |",
            f"| Candidate 2 targeted tests passed | {tick(bool(second and second['targeted'] and second['targeted']['result'] == 'PASS'))} |",
            f"| Regression tests passed | {tick(bool(second and second['regression'] and second['regression']['result'] == 'PASS'))} |",
            f"| Original source unchanged | {tick(integrity['original_unchanged'])} |",
            f"| Isolated workspaces cleaned | {tick(integrity['workspaces_cleaned'])} |", ""]
    if summary["evidence_issues"]:
        out += ["Evidence issues:", _block("\n".join(summary["evidence_issues"])), ""]
    out += ["## 7. Why this matters",
            "GhostFix does not trust generated code. Every candidate must survive isolated execution, and failed "
            "execution becomes evidence for the next repair attempt. The diagnosis above is a model hypothesis; "
            "only the executed tests are proof.", "",
            "## Benchmark evidence (separate from this demo)",
            "| Version | Verified |", "| --- | --- |"]
    out += [f"| Benchmark {name} | {ok}/{total} = {100 * ok / total:.2f}% |" for name, ok, total in BENCHMARKS]
    out += ["", "Same frozen seven-case corpus; one official run per version. The corpus is small and model output "
            "is stochastic, so these are engineering evidence, not statistical proof. This demo is not counted in them.", ""]
    return "\n".join(out)


def publishable(files: dict[str, str], secrets: tuple[str, ...]) -> dict[str, list[str]]:
    """Return problems per file; credentials and host paths must never be published."""
    problems = {}
    for name, text in files.items():
        found = list(reasons(text, secrets))
        if any(s and s in text for s in secrets) or "NEBIUS_API_KEY" in text:
            found.append("credential")
        if found:
            problems[name] = sorted(set(found))
    return problems


def evidence_files(run, proof, summary, events, metadata, secrets) -> dict[str, str]:
    dump = lambda value: json.dumps(sanitize(value, secrets), indent=2, ensure_ascii=False) + "\n"
    return {"repair-run.json": dump(run.model_dump(mode="json")),
            "proof-of-fix.json": render_json(proof) + "\n", "proof-of-fix.md": render_markdown(proof),
            "model-audit.json": dump(events), "demo-summary.json": dump(summary),
            "run-metadata.json": dump(metadata), "demo.md": render_demo(summary)}


def sandbox_containers() -> int | None:
    try:
        return len(subprocess.run(["docker", "ps", "-aq", "--filter", "name=ghostfix-pytest"], capture_output=True,
                                  text=True, timeout=30, check=False).stdout.split())
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git(*args) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30,
                              check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


async def main_async(output: Path) -> int:
    settings = get_settings()
    problems = preflight(settings, output, docker_available)
    if problems:
        print("Demo not started:\n- " + "\n- ".join(problems))
        return 2
    secrets = (settings.nebius_api_key.get_secret_value(),)
    manifest = BenchmarkManifest.model_validate_json((ROOT / "benchmarks/manifest.json").read_text(encoding="utf-8"))
    fingerprint_before = corpus_fingerprint(manifest, ROOT / "fixtures")
    reserve_output(output)
    client = AuditedClient(NebiusClient(settings), secrets)
    try:
        sandbox = DockerPytestRunner(settings.sandbox_image, settings.sandbox_timeout_seconds)
        limits = PatchLimits(max_files=settings.patch_max_files, max_changed_lines=settings.patch_max_changed_lines,
                             max_diff_bytes=settings.patch_max_diff_bytes)
        run, before, after = await execute(client, sandbox, FIXTURE, secrets, limits)
    except Exception as error:
        (output / "demo-error.json").write_text(json.dumps({"stage": "execution", "error_type": type(error).__name__,
            "model_calls": len(client.events)}, indent=2) + "\n", encoding="utf-8")
        print(f"STOP: demo execution failed ({type(error).__name__}); no success inferred")
        return 1
    finally:
        await client.close()
    proof = build_proof_of_fix(run, secrets=secrets)
    summary = summarize(run, proof, client.events, before == after)
    metadata = {
        "purpose": "hackathon self-correction demo (DEMO-ONLY; not benchmark evidence)",
        "run_id": run.run_id, "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": _git("rev-parse", "HEAD"), "uncommitted_tracked_changes": bool(_git("status", "--porcelain", "-uno")),
        "model": MODEL, "fixture": "fixtures/broken_math",
        "demo_injection": {"enabled": True, "scope": "attempt 1 only", "mechanism": "scripts/test_self_correction.py DemoAgent",
                           "provenance_model_label": DEMO_MODEL, "available_in_production_or_benchmark": False},
        "model_calls": summary["model_calls"], "model_calls_total": len(client.events),
        "correction_request_sha256": summary["correction"]["request_sha256"],
        "original_hashes_before": before, "original_hashes_after": after, "original_unchanged": before == after,
        "corpus_fingerprint_before": fingerprint_before,
        "corpus_fingerprint_after": corpus_fingerprint(manifest, ROOT / "fixtures"),
        "remaining_verification_workspaces": len(list(Path(tempfile.gettempdir()).glob("ghostfix-verify-*"))),
        "remaining_sandbox_containers": sandbox_containers(),
        "fidelity_policy": "exact UTF-8 SHA-256 of raw model text; unsafe text withheld with hash and reason",
    }
    files = evidence_files(run, proof, summary, client.events, metadata, secrets)
    problems = publishable(files, secrets)
    if problems:
        (output / "demo-error.json").write_text(json.dumps({"stage": "publish_gate", "files": problems,
            "run_id": run.run_id}, indent=2) + "\n", encoding="utf-8")
        print("STOP: evidence failed the credential/host-path gate; nothing else was published")
        return 1
    for name, text in files.items():
        (output / name).write_text(text, encoding="utf-8")
    print(terminal_summary(summary))
    print(f"\nRun {run.run_id}\nEvidence: {output.relative_to(ROOT) if output.is_relative_to(ROOT) else output.name}")
    return 0 if summary["self_correction_demonstrated"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="GhostFix verified self-correction demo (writes fresh evidence).")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="new directory; must not exist")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    return asyncio.run(main_async(args.output.resolve()))


if __name__ == "__main__":
    raise SystemExit(main())
