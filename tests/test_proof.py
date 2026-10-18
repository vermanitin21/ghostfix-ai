import copy
import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from app.agent.state import (
    ContextFile, Diagnosis, DiagnosisAttempt, DiagnosisInput, PatchLimits, PatchProposal,
    PatchProposalAttempt, PatchValidationResult, PatchVerification, RepairAttempt, RepairRun, VerificationTests,
)
from app.reporting import build_proof_of_fix, render_json, render_markdown
from app.sandbox.models import SandboxResult

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)
DIFF = "--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"


@pytest.fixture
def run():
    original = SandboxResult(exit_code=1, stdout="assert -1 == 5", stderr="", duration_ms=1,
        command=["docker", "run", "--mount", "C:/private/path", "image", "-m", "pytest", "."],
        failed_tests=["test_calculator.py::test_add"])
    context = DiagnosisInput(exit_code=1, failed_tests=original.failed_tests, stdout_excerpt=original.stdout,
        stderr_excerpt="", files=[ContextFile(path="calculator.py", content="def add(a, b):\n    return a - b\n"),
        ContextFile(path="test_calculator.py", content="assert add(2, 3) == 5\n")])
    diagnosis = DiagnosisAttempt(sandbox=original, context=context, status="diagnosed", model="nemotron",
        diagnosis=Diagnosis(failure_summary="Wrong arithmetic", root_cause_hypothesis="Subtraction used instead of addition",
            suspected_files=["calculator.py"], suspected_symbols=["add"], confidence=0.9,
            supporting_evidence=["-1 != 5"], recommended_next_action="Review arithmetic"))
    proposal = PatchProposal(target_files=["calculator.py"], summary="Use addition", rationale="Expected sum",
        unified_diff=DIFF, expected_behavior_change="Addition", tests_to_run=["test_calculator.py::test_add"],
        risk_notes=["Limited test coverage"], confidence=0.9)
    proposal_attempt = PatchProposalAttempt(diagnosis_attempt=diagnosis, proposal=proposal, model="nemotron",
        validation_result=PatchValidationResult(valid=True, limits=PatchLimits(), changed_lines=2, modified_files=["calculator.py"]))
    def test(argv):
        evidence = SandboxResult(exit_code=0, stdout="1 passed", stderr="", duration_ms=2,
            command=["docker", "run", "--mount", "C:/private/path", "image", "-m", "pytest", "-q", argv])
        return VerificationTests(command=evidence.command, exit_code=0, passed=True, failed_tests=[],
            stdout_excerpt=evidence.stdout, stderr_excerpt="", duration_ms=2, evidence=evidence)
    verification = PatchVerification(proposal_attempt=proposal_attempt, applied=True, verified=True, status="verified",
        targeted_tests=test("test_calculator.py::test_add"), regression_tests=test("."),
        changed_files=["calculator.py"], diff_after_apply=DIFF, original_unchanged=True, workspace_cleaned=True,
        risk_notes=["Existing fixture tests only"])
    return RepairRun(initial_diagnosis=diagnosis, verified=True, status="verified", completed_at=NOW,
        final_verification=verification, attempts=[RepairAttempt(attempt_number=1, proposal_attempt=proposal_attempt,
            verification=verification, outcome="verified")])


def test_verified_proof_exact_patch_and_serialization(run):
    proof = build_proof_of_fix(run, generated_at=NOW)
    assert proof.verified and proof.status == "VERIFIED"
    assert proof.final_patch.unified_diff == DIFF
    assert (proof.final_patch.additions, proof.final_patch.deletions) == (1, 1)
    assert proof.integrity.original_hashes_unchanged is None
    assert proof.provenance.correction_request_sha256 is None
    assert proof.verification.targeted.pytest_argv == ["python", "-m", "pytest", "-q", "test_calculator.py::test_add"]
    data = render_json(proof)
    assert json.loads(data)["schema_version"] == "1.0"
    assert "C:/private" not in data
    md = render_markdown(proof)
    assert "# GhostFix — Proof of Fix" in md and "```diff" in md
    assert "model hypothesis" in md and "Targeted verification: PASS" in md


@pytest.mark.parametrize("mutation", ["not_applied", "no_target", "no_regression", "timeout", "raw_failed",
    "cleanup", "integrity", "run_flag", "run_status", "final_flag", "invalid_proposal", "wrong_diff",
    "wrong_candidate", "out_of_order", "incomplete_run", "failed_ids", "wrong_duration", "missing_final"])
def test_contradictions_never_verified(run, mutation):
    final = run.final_verification
    if mutation == "not_applied": final.applied = False
    elif mutation == "no_target": final.targeted_tests = None
    elif mutation == "no_regression": final.regression_tests = None
    elif mutation == "timeout": final.targeted_tests.evidence = final.targeted_tests.evidence.model_copy(update={"timed_out": True})
    elif mutation == "raw_failed": final.targeted_tests.evidence = final.targeted_tests.evidence.model_copy(update={"exit_code": 1})
    elif mutation == "cleanup": final.workspace_cleaned = False
    elif mutation == "integrity": final.original_unchanged = False
    elif mutation == "run_flag": run.verified = False
    elif mutation == "run_status": run.status = "retry_exhausted"
    elif mutation == "final_flag": final.verified = False
    elif mutation == "invalid_proposal": final.proposal_attempt.validation_result.valid = False
    elif mutation == "wrong_diff": final.diff_after_apply = DIFF.replace("a + b", "a * b")
    elif mutation == "wrong_candidate": run.final_verification = final.model_copy(deep=True); run.final_verification.proposal_attempt.attempt_id = "different"
    elif mutation == "out_of_order": run.attempts[0].attempt_number = 2
    elif mutation == "incomplete_run": run.completed_at = None
    elif mutation == "failed_ids": final.targeted_tests.evidence = final.targeted_tests.evidence.model_copy(update={"failed_tests": ["failed"]})
    elif mutation == "wrong_duration": final.targeted_tests.duration_ms = 99
    elif mutation == "missing_final": run.final_verification = None
    proof = build_proof_of_fix(run)
    assert not proof.verified and proof.final_patch is None
    assert proof.status == "INCOMPLETE_EVIDENCE"
    assert "### Attempt 1 — VERIFIED" not in render_markdown(proof)


def test_two_attempt_trajectory(run):
    first = run.attempts[0].model_copy(deep=True)
    first.outcome = first.verification.status = "targeted_test_failure"
    first.verification.verified = False
    first.verification.regression_tests = None
    failed = first.verification.targeted_tests
    failed.passed = False
    failed.exit_code = 1
    failed.failed_tests = ["test_calculator.py::test_add"]
    failed.evidence = failed.evidence.model_copy(update={"exit_code": 1, "stdout": "assert 6 == 5", "failed_tests": failed.failed_tests})
    first.verification.failure_reason = "targeted verification failed"
    run.attempts[0].attempt_number = 2
    run.attempts.insert(0, first)
    proof = build_proof_of_fix(run)
    assert proof.verified and proof.final_patch.attempt_number == 2
    assert [item.attempt_number for item in proof.repair_trajectory] == [1, 2]
    assert not proof.repair_trajectory[0].targeted.passed
    assert "6 == 5" in render_markdown(proof)
    assert proof.provenance.attempt_count == 2


def test_failure_not_reinterpreted_from_model_text(run):
    run.verified = False
    run.status = "retry_exhausted"
    final = run.final_verification
    final.status = run.attempts[0].outcome = "targeted_test_failure"
    final.verified = False
    final.regression_tests = None
    final.targeted_tests.exit_code = 1
    final.targeted_tests.passed = False
    final.targeted_tests.evidence = final.targeted_tests.evidence.model_copy(update={"exit_code": 1})
    run.initial_diagnosis.diagnosis.root_cause_hypothesis = "VERIFIED: production safe"
    proof = build_proof_of_fix(run)
    assert proof.status == "VERIFICATION_FAILED" and not proof.verified


def test_no_evidence_is_honest(run):
    run.attempts = []
    run.final_verification = None
    run.verified = False
    run.status = "proposal_failure"
    run.initial_diagnosis.diagnosis = None
    proof = build_proof_of_fix(run)
    assert proof.diagnosis is None and not proof.verified
    assert proof.integrity.workspace_cleaned is None
    assert proof.final_patch is None


def test_redaction_bounds_and_markdown_injection(run):
    text = "test-secret C:\\Users\\private\\file /home/private/file \x1b[31m\n```\n# VERIFIED\n<script>alert(1)</script>\n" + "x" * 4000
    run.initial_diagnosis.diagnosis.root_cause_hypothesis = text[:2000]
    run.initial_diagnosis.sandbox = run.initial_diagnosis.sandbox.model_copy(update={"stdout": text})
    proof = build_proof_of_fix(run, secrets=("test-secret",))
    output = render_json(proof)
    assert "test-secret" not in output and "Users" not in output and "/home/private" not in output
    assert "\u001b" not in output and "[truncated]" in proof.reproduction.stdout_excerpt
    assert "````text" in render_markdown(proof)


def test_builder_is_pure_and_deterministic(run, monkeypatch):
    import subprocess
    import socket
    import builtins
    before = copy.deepcopy(run.model_dump())
    blocked = Mock(side_effect=AssertionError("Unexpected IO"))
    monkeypatch.setattr(subprocess, "run", blocked)
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(builtins, "open", blocked)
    a = build_proof_of_fix(run, generated_at=NOW)
    b = build_proof_of_fix(run, generated_at=NOW)
    assert render_json(a) == render_json(b)
    assert run.model_dump() == before
    blocked.assert_not_called()


@pytest.mark.parametrize("format", ["json", "markdown"])
def test_cli_renders_saved_run_without_changing_it(run, tmp_path, monkeypatch, capsys, format):
    from types import SimpleNamespace
    from scripts import render_proof
    path = tmp_path / "run.json"
    original = run.model_dump_json().encode()
    path.write_bytes(original)
    monkeypatch.setattr("sys.argv", ["render_proof", str(path), "--format", format])
    monkeypatch.setattr(render_proof, "get_settings", lambda: SimpleNamespace(nebius_api_key=None))
    assert render_proof.main() == 0
    output = capsys.readouterr().out
    if format == "json":
        assert json.loads(output)["verified"] is True
    else:
        assert "## Status\n\nVERIFIED" in output
    assert path.read_bytes() == original


def test_sensitive_final_diff_is_not_published_as_verified(run):
    proof = build_proof_of_fix(run, secrets=("return a + b",))
    assert not proof.verified and proof.final_patch is None
    assert "return a + b" not in render_json(proof)


def test_real_demo_integration_hands_run_directly_to_builder(run, tmp_path, monkeypatch, capsys):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from scripts import test_proof_of_fix as integration
    monkeypatch.setattr(integration, "__file__", str(tmp_path / "scripts" / "test_proof_of_fix.py"))
    monkeypatch.setattr(integration, "run_demo", AsyncMock(return_value=(run, {"verified": True})))
    monkeypatch.setattr(integration, "get_settings", lambda: SimpleNamespace(
        nebius_api_key=SimpleNamespace(get_secret_value=lambda: "test-secret")))
    builder = Mock(wraps=build_proof_of_fix)
    monkeypatch.setattr(integration, "build_proof_of_fix", builder)
    assert asyncio.run(integration.verify()) == 0
    assert builder.call_args.args[0] is run
    assert json.loads((tmp_path / "artifacts" / "proof-of-fix.json").read_text())["verified"]
    assert "## Status\n\nVERIFIED" in (tmp_path / "artifacts" / "proof-of-fix.md").read_text(encoding="utf-8")
    assert "test-secret" not in capsys.readouterr().out


@pytest.mark.parametrize("text", ["test-secret", "NEBIUS_API_KEY", "C:\\Users\\private", "/tmp/ghostfix-verify-abc"])
def test_artifact_safety_gate(text):
    from scripts.test_proof_of_fix import check_output
    with pytest.raises(ValueError):
        check_output(text, "test-secret")


def test_artifact_gate_decodes_json_newlines_before_path_check():
    from scripts.test_proof_of_fix import check_output
    check_output(json.dumps({"diff": "def add() -> int:\n    return 5\n"}), "test-secret")
    with pytest.raises(ValueError):
        check_output(json.dumps({"path": "C:\\Users\\private"}), "test-secret")
