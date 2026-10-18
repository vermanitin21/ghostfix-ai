from unittest.mock import Mock

import pytest

from app.agent.state import (ContextFile, DiagnosisInput, DiagnosisAttempt, PatchProposal,
                             PatchProposalAttempt, PatchValidationResult, PatchLimits)
from app.agent.verification import PatchVerifier
from app.sandbox.models import SandboxResult
from app.tools.files import RepositoryContext


def execution(code=0, **kwargs):
    return SandboxResult(exit_code=code, stdout="result", stderr="", duration_ms=1,
                         command=["docker"], **kwargs)


@pytest.fixture
def case(tmp_path):
    source = "def add(a, b):\n    return a - b\n"
    test = "from calculator import add\ndef test_add(): assert add(2, 3) == 5\n"
    for name, content in [("calculator.py", source), ("test_calculator.py", test)]:
        (tmp_path / name).write_bytes(content.encode())
    context = DiagnosisInput(exit_code=1, failed_tests=[], stdout_excerpt="", stderr_excerpt="",
                             files=[ContextFile(path="calculator.py", content=source),
                                    ContextFile(path="test_calculator.py", content=test)])
    proposal = PatchProposal(target_files=["calculator.py"], summary="fix", rationale="fix",
        unified_diff="--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n",
        expected_behavior_change="addition", tests_to_run=["test_calculator.py::test_add"],
        risk_notes=[], confidence=0.9)
    attempt = PatchProposalAttempt(diagnosis_attempt=DiagnosisAttempt(sandbox=execution(1), context=context),
        proposal=proposal, validation_result=PatchValidationResult(valid=True, limits=PatchLimits()))
    return RepositoryContext(tmp_path), attempt


@pytest.mark.parametrize("codes,status", [([0, 0], "verified"), ([1], "targeted_test_failure"),
                                         ([0, 1], "regression_failure")])
def test_verification_and_cleanup(case, codes, status):
    repository, attempt = case
    original = (repository.root / "calculator.py").read_bytes()
    workspaces = []
    def run(root, selectors):
        workspaces.append(root)
        assert root != repository.root
        assert b"a + b" in (root / "calculator.py").read_bytes()
        assert selectors == (["test_calculator.py::test_add"] if len(workspaces) == 1 else None)
        return execution(codes[len(workspaces) - 1])
    result = PatchVerifier(Mock(run=run)).verify(attempt, repository)
    assert result.status == status
    assert result.verified == (status == "verified")
    assert result.applied and result.workspace_cleaned and result.original_unchanged
    assert all(not root.exists() for root in workspaces)
    assert (repository.root / "calculator.py").read_bytes() == original


@pytest.mark.parametrize("kind", ["malformed", "stale", "traversal", "blocked", "unsafe_test"])
def test_rejects_before_execution(case, kind):
    repository, attempt = case
    if kind == "malformed":
        attempt.proposal.unified_diff = "invalid"
    elif kind == "stale":
        (repository.root / "calculator.py").write_bytes(b"changed\n")
    elif kind in ("traversal", "blocked"):
        name = "../calculator.py" if kind == "traversal" else ".env"
        attempt.proposal.target_files = [name]
        attempt.proposal.unified_diff = attempt.proposal.unified_diff.replace("calculator.py", name)
    else:
        attempt.proposal.tests_to_run = ["test_calculator.py::test_add;echo oops"]
    sandbox = Mock()
    result = PatchVerifier(sandbox).verify(attempt, repository)
    assert not result.verified and not result.applied and result.workspace_cleaned
    sandbox.run.assert_not_called()


@pytest.mark.parametrize("timeout", [True, False])
def test_infrastructure_and_timeout(case, timeout):
    repository, attempt = case
    sandbox = Mock(run=Mock(return_value=execution(124 if timeout else 125,
                   timed_out=timeout, infrastructure_error="error")))
    result = PatchVerifier(sandbox).verify(attempt, repository)
    assert result.status == ("timeout" if timeout else "infrastructure_failure")
    assert result.workspace_cleaned and result.original_unchanged


def test_cleanup_on_runner_exception(case):
    repository, attempt = case
    sandbox = Mock(run=Mock(side_effect=OSError))
    result = PatchVerifier(sandbox).verify(attempt, repository)
    assert result.status == "infrastructure_failure" and result.workspace_cleaned


def test_abort_if_written_diff_does_not_match(case, monkeypatch):
    from pathlib import Path
    repository, attempt = case
    write = Path.write_bytes
    def corrupt(path, data):
        if "ghostfix-verify-" in str(path) and b"a + b" in data:
            data = data.replace(b"a + b", b"a * b")
        return write(path, data)
    monkeypatch.setattr(Path, "write_bytes", corrupt)
    sandbox = Mock()
    result = PatchVerifier(sandbox).verify(attempt, repository)
    assert result.status == "application_failure"
    assert result.workspace_cleaned and result.original_unchanged
    sandbox.run.assert_not_called()


def test_runner_derives_safe_argv(case, monkeypatch):
    import subprocess
    from app.sandbox.runner import DockerPytestRunner
    repository, _ = case
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "1 passed", ""))
    monkeypatch.setattr(subprocess, "run", run)
    result = DockerPytestRunner("image").run(repository.root, ["test_calculator.py::test_add"])
    assert result.command[-1] == "test_calculator.py::test_add"
    assert "--network=none" in result.command
    assert not run.call_args.kwargs.get("shell", False)
