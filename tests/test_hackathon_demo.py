"""Offline demo orchestration, rendering, provenance and safety. No Docker, network or host code execution."""
import asyncio
import inspect
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

import scripts.run_self_correction_demo as demo
from app.agent.orchestrator import GhostFixOrchestrator
from app.agent.repair import RepairLoop
from app.agent.state import PatchLimits, PatchProposal
from app.core.config import Settings
from app.llm.nebius_client import CompletionResult
from app.reporting import build_proof_of_fix
from app.sandbox.models import SandboxResult

ROOT = Path(__file__).resolve().parents[1]
SECRET = "sk-demo-only-test-credential-0000"
SOURCE = 'def add(left: int, right: int) -> int:\n    """Add two integers."""\n    return left - right\n'
TESTS = "from calculator import add\n\n\ndef test_adds_two_numbers() -> None:\n    assert add(2, 3) == 5\n"
GOOD = ('--- a/calculator.py\n+++ b/calculator.py\n@@ -1,3 +1,3 @@\n def add(left: int, right: int) -> int:\n'
        '     """Add two integers."""\n-    return left - right\n+    return left + right\n')
DIAGNOSIS = dict(failure_summary="add subtracts", suspected_files=["calculator.py"], suspected_symbols=["add"],
                 root_cause_hypothesis="add uses '-' instead of '+'", supporting_evidence=["add(2, 3) returned -1"],
                 confidence=0.9, recommended_next_action="Inspect add")


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / "broken_math"
    root.mkdir()
    (root / "calculator.py").write_text(SOURCE, encoding="utf-8", newline="")
    (root / "test_calculator.py").write_text(TESTS, encoding="utf-8", newline="")
    return root


class FakeSandbox:
    """Reads workspace text only; never executes fixture code on the host."""
    def __init__(self, broken_candidate_fails_infra=False):
        self.calls, self.roots, self.infra = [], [], broken_candidate_fails_infra

    def run(self, project_dir, test_ids=None):
        self.roots.append(project_dir)
        code = (project_dir / "calculator.py").read_text(encoding="utf-8")
        passed = "left + right" in code
        self.calls.append((code, test_ids))
        command = ["docker", "run", "--mount", "type=bind,source=C:/host/private/x,target=/workspace",
                   "python", "-m", "pytest", "-q", *(test_ids or [])]
        if "left * right" in code and self.infra:
            return SandboxResult(exit_code=125, stdout="", stderr="", duration_ms=1, command=command,
                                 infrastructure_error="daemon unavailable")
        value = "6" if "*" in code else "-1"
        return SandboxResult(exit_code=0 if passed else 1, duration_ms=1, command=command, stderr="",
            stdout="" if passed else f"E       assert {value} == 5\nFAILED test_calculator.py::test_adds_two_numbers",
            failed_tests=[] if passed else ["test_calculator.py::test_adds_two_numbers"])


class FakeModel:
    def __init__(self, diff=GOOD, hypothesis=DIAGNOSIS["root_cause_hypothesis"]):
        self.diff, self.hypothesis, self.closed = diff, hypothesis, False
        self.complete_structured = AsyncMock(side_effect=self.respond)

    async def respond(self, messages, schema, *, multiline=False):
        if schema.__name__ == "ConciseDiagnosis":
            return CompletionResult(model=demo.MODEL, content=json.dumps({**DIAGNOSIS, "root_cause_hypothesis": self.hypothesis}))
        return CompletionResult(model=demo.MODEL, content=PatchProposal(
            target_files=["calculator.py"], summary="Use addition", rationale="Candidate 1 evidence: 6 != 5",
            unified_diff=self.diff, expected_behavior_change="add(2, 3) == 5",
            tests_to_run=["test_calculator.py::test_adds_two_numbers"], risk_notes=[], confidence=0.8).model_dump_json())

    async def close(self):
        self.closed = True


def row(name, value):
    return f"{name} {'.' * (20 - len(name))} {value}"


def run_demo(fixture, model=None, sandbox=None):
    model, sandbox = model or FakeModel(), sandbox or FakeSandbox()
    client = demo.AuditedClient(model, (SECRET,))
    run, before, after = asyncio.run(demo.execute(client, sandbox, fixture, (SECRET,), PatchLimits()))
    proof = build_proof_of_fix(run, secrets=(SECRET,))
    return run, proof, demo.summarize(run, proof, client.events, before == after), client, sandbox


def test_demo_self_corrects_from_executed_evidence(fixture):
    run, proof, summary, client, sandbox = run_demo(fixture)
    assert proof.status == "VERIFIED" and summary["self_correction_demonstrated"]
    first, second = summary["attempts"]
    assert first["demo_injected"] and first["model"] == demo.DEMO_MODEL and "DEMO-INJECTED" in first["candidate_source"]
    assert first["verification_status"] == "targeted_test_failure" and first["targeted"]["exit_code"] == 1
    assert "E       assert 6 == 5" in first["targeted"]["key_output"]
    assert not second["demo_injected"] and second["model"] == demo.MODEL and second["generated_from_execution_evidence"]
    assert second["targeted"]["result"] == "PASS" and second["regression"]["result"] == "PASS"
    # Injection replaces only the first proposal: diagnosis and correction are model calls.
    assert summary["model_calls"] == {"diagnosis": 1, "proposal": 0, "correction": 1, "format_retry": 0}
    correction = summary["correction"]
    assert correction["occurred"] and correction["request_contains_attempt_1_evidence"]
    assert "targeted_tests" in correction["evidence_fields"] and correction["targeted_exit_code"] == 1
    sent = json.loads(client.events[1]["messages"][-1]["content"])["correction_evidence"]
    assert sent["previous_proposal"]["unified_diff"] == run.attempts[0].proposal_attempt.proposal.unified_diff
    assert "command" not in json.dumps(sent["targeted_tests"])
    # Reproduction, candidate 1 targeted (stops at failure), candidate 2 targeted + regression.
    assert len(sandbox.calls) == 4 and "left * right" in sandbox.calls[1][0] and "left + right" in sandbox.calls[2][0]


def test_candidate_2_applied_to_fresh_original_copy(fixture):
    run, _, summary, _, sandbox = run_demo(fixture)
    assert summary["candidate_2_from_original_source"]
    applied = summary["attempts"][1]["applied_diff_against_original"]
    assert "-    return left - right" in applied and "left * right" not in applied
    assert sandbox.roots[1] != sandbox.roots[2] and not sandbox.roots[1].exists() and not sandbox.roots[2].exists()
    assert (fixture / "calculator.py").read_text(encoding="utf-8") == SOURCE
    # A layered application (removing candidate 1's line) is never reported as an original-source reset.
    verification = run.attempts[1].verification.model_copy(update={"diff_after_apply": GOOD.replace(
        "-    return left - right", "-    return left * right")})
    layered = run.model_copy(update={"attempts": [run.attempts[0], run.attempts[1].model_copy(update={"verification": verification})]})
    proof = build_proof_of_fix(layered, secrets=(SECRET,))
    assert not demo.summarize(layered, proof, [], True)["candidate_2_from_original_source"]


def test_no_correction_without_failed_test_evidence(fixture):
    model = FakeModel()
    run, proof, summary, _, _ = run_demo(fixture, model, FakeSandbox(broken_candidate_fails_infra=True))
    assert len(run.attempts) == 1 and run.status == "infrastructure_failure"
    assert not summary["correction"]["occurred"] and not summary["self_correction_demonstrated"]
    assert summary["model_calls"]["correction"] == 0 and proof.status != "VERIFIED"
    assert row("Correction", "not performed") in demo.terminal_summary(summary)


def test_never_claims_verified_when_final_verification_fails(fixture):
    wrong = GOOD.replace("left + right", "left // right")
    run, proof, summary, _, _ = run_demo(fixture, FakeModel(diff=wrong))
    assert run.status == "retry_exhausted" and len(run.attempts) == 2
    assert not proof.verified and proof.status != "VERIFIED" and not summary["self_correction_demonstrated"]
    text, markdown = demo.terminal_summary(summary), demo.render_demo(summary)
    assert "VERIFIED" not in text and "VERIFIED" not in markdown
    assert "Self-correction demonstrated: NO" in text and row("Candidate 2", "verification FAILED") in text


def test_reports_preserve_both_attempts_and_label_hypothesis(fixture):
    _, proof, summary, _, _ = run_demo(fixture)
    markdown = demo.render_demo(summary)
    for heading in ("## 1. Bug reproduced", "## 2. Root cause - *model hypothesis, not proof*",
                    "## 3. Candidate 1 - DEMO-INJECTED", "## 4. Autonomous correction",
                    "## 5. Candidate 2 - model: nvidia/Nemotron-3_5-Lightning", "## 6. Proof of Fix"):
        assert heading in markdown
    assert markdown.count("**TARGETED TEST: FAIL**") == 1 and "**TARGETED TEST: PASS**" in markdown
    assert "**REGRESSION TESTS: PASS**" in markdown and "1/7 = 14.29%" in markdown and "4/7 = 57.14%" in markdown
    assert "5/7" not in markdown and "not benchmark evidence" in markdown
    assert [a.attempt_number for a in proof.repair_trajectory] == [1, 2]
    assert demo.DEMO_MODEL in proof.provenance.runtime_models
    terminal = demo.terminal_summary(summary)
    for expected in (row("Reproduction", "FAIL reproduced"), row("Diagnosis", "complete"),
                row("Candidate 1", "demo-injected; verification FAILED"),
                row("Correction", "generated from execution evidence"),
                row("Candidate 2", "verification PASSED"), row("Regression", "PASSED"),
                row("Proof of Fix", "VERIFIED")):
        assert expected in terminal


def test_secrets_and_host_paths_never_published(fixture):
    run, proof, summary, client, _ = run_demo(fixture, FakeModel(hypothesis=f"key {SECRET} leaked"))
    files = demo.evidence_files(run, proof, summary, client.events, {"note": "x"}, (SECRET,))
    assert demo.publishable(files, (SECRET,)) == {}
    assert all(SECRET not in text and "C:/host" not in text for text in files.values())
    # Withheld values keep an exact hash and reason; nothing is silently rewritten.
    records = json.loads(files["repair-run.json"])["initial_diagnosis"]["sandbox"]["command"]
    assert any(isinstance(r, dict) and r["raw_response_redacted"] and r["raw_response_sha256"] for r in records)
    assert demo.publishable({"bad.md": f"token {SECRET}", "p.md": "see C:\\Users\\me\\x.py"}, (SECRET,)).keys() == {"bad.md", "p.md"}


def test_overwrite_protection(tmp_path):
    existing = tmp_path / "hackathon-demo"
    existing.mkdir()
    (existing / "demo.md").write_text("historical", encoding="utf-8")
    settings = Settings(_env_file=None, nebius_api_key=SECRET, nebius_model=demo.MODEL)
    docker = Mock(return_value=True)
    assert demo.preflight(settings, existing, docker) == ["Output directory already exists; refusing to overwrite demo evidence"]
    docker.assert_not_called()
    with pytest.raises(FileExistsError):
        demo.reserve_output(existing)
    assert (existing / "demo.md").read_text(encoding="utf-8") == "historical"
    assert demo.preflight(Settings(_env_file=None, nebius_model="other"), tmp_path / "new", docker)


def test_command_end_to_end_offline(fixture, tmp_path, monkeypatch, capsys):
    model = FakeModel()
    monkeypatch.setattr(demo, "get_settings", lambda: Settings(_env_file=None, nebius_api_key=SECRET, nebius_model=demo.MODEL))
    monkeypatch.setattr(demo, "NebiusClient", lambda settings: model)
    monkeypatch.setattr(demo, "DockerPytestRunner", lambda image, timeout: FakeSandbox())
    monkeypatch.setattr(demo, "docker_available", lambda image: True)
    monkeypatch.setattr(demo, "sandbox_containers", lambda: 0)
    monkeypatch.setattr(demo, "FIXTURE", fixture)
    output = tmp_path / "out" / "hackathon-demo"
    assert asyncio.run(demo.main_async(output)) == 0 and model.closed
    names = {p.name for p in output.iterdir()}
    assert names == {"repair-run.json", "proof-of-fix.json", "proof-of-fix.md", "model-audit.json",
                     "demo-summary.json", "run-metadata.json", "demo.md"}
    assert all(SECRET not in (output / n).read_text(encoding="utf-8") for n in names)
    metadata = json.loads((output / "run-metadata.json").read_text(encoding="utf-8"))
    assert metadata["demo_injection"]["available_in_production_or_benchmark"] is False
    assert metadata["original_unchanged"] and metadata["corpus_fingerprint_before"] == metadata["corpus_fingerprint_after"]
    assert row("Proof of Fix", "VERIFIED") in capsys.readouterr().out
    audit = json.loads((output / "model-audit.json").read_text(encoding="utf-8"))
    assert [e["stage"] for e in audit] == ["diagnosis", "correction"]
    for event in audit:
        evidence = event["response_evidence"]
        assert not evidence["raw_response_redacted"]
        assert demo.sha(evidence["raw_response_text"]) == evidence["raw_response_sha256"] == evidence["stored_text_sha256"]
    before = {n: (output / n).read_bytes() for n in names}
    calls = model.complete_structured.await_count
    assert asyncio.run(demo.main_async(output)) == 2
    assert {n: (output / n).read_bytes() for n in names} == before and model.complete_structured.await_count == calls


def test_production_and_benchmark_paths_cannot_inject(fixture):
    llm = Mock(complete_structured=AsyncMock(side_effect=FakeModel().respond))
    _, _, summary, _, _ = run_demo(fixture)
    diagnosis = asyncio.run(GhostFixOrchestrator(FakeSandbox(), llm).analyze(
        __import__("app.tools.files", fromlist=["RepositoryContext"]).RepositoryContext(fixture, ()), demo.PATHS))
    attempt = asyncio.run(GhostFixOrchestrator(FakeSandbox(), llm).propose_patch(
        diagnosis, __import__("app.tools.files", fromlist=["RepositoryContext"]).RepositoryContext(fixture, ())))
    assert attempt.model == demo.MODEL and "DEMO" not in attempt.proposal.summary
    assert llm.complete_structured.await_count == 2  # the default path always asks the model
    for function in (GhostFixOrchestrator.propose_patch, RepairLoop.run, RepairLoop.__init__):
        assert not any("demo" in p or "inject" in p for p in inspect.signature(function).parameters)
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "DemoAgent" not in text and "demo:injected" not in text and "from scripts" not in text, path
    # Importing the production and benchmark stacks never loads the script-only injector.
    code = ("import sys, app.agent.repair, app.evaluation.runner, app.evaluation.evaluator;"
            "print(any(m.startswith('scripts') for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    assert out.strip() == "False"
