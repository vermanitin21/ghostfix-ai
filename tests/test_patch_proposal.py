import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent.orchestrator import GhostFixOrchestrator
from app.agent.patches import validate_patch
from app.agent.state import (
    ContextFile, Diagnosis, DiagnosisAttempt, DiagnosisInput, PatchLimits, PatchProposal,
)
from app.llm.nebius_client import CompletionResult
from app.sandbox.models import SandboxResult
from app.tools.files import RepositoryContext

SOURCE = "def add(a, b):\n    return a - b\n"
DIFF = "--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"


def proposal(diff=DIFF, targets=None):
    return PatchProposal(target_files=targets or ["calculator.py"], summary="Fix addition",
                         rationale="Subtraction caused failure", unified_diff=diff,
                         expected_behavior_change="Return 5", tests_to_run=["test_calculator.py"],
                         risk_notes=[], confidence=0.95)


@pytest.fixture
def context():
    return DiagnosisInput(exit_code=1, failed_tests=["test_add"], stdout_excerpt="assert -1 == 5",
                          stderr_excerpt="", files=[ContextFile(path="calculator.py", content=SOURCE),
                          ContextFile(path="test_calculator.py", content="assert add(2,3) == 5\n")])


@pytest.mark.parametrize("diff,targets", [
    (DIFF.replace("calculator.py", "../calculator.py"), ["../calculator.py"]),
    (DIFF.replace("calculator.py", "/absolute.py"), ["/absolute.py"]),
    (DIFF.replace("calculator.py", ".env"), [".env"]),
    (DIFF.replace("calculator.py", "secrets.py"), ["secrets.py"]),
    (DIFF.replace("calculator.py", "other.py"), ["other.py"]),
    (DIFF.replace("--- a/calculator.py", "--- /dev/null"), None),
    (DIFF.replace("+++ b/calculator.py", "+++ /dev/null"), None),
    ("not a diff\n", None),
    (DIFF.replace("@@ -1,2 +1,2 @@", "@@ -1,9 +1,2 @@"), None),
    (DIFF.replace("a - b", "a * b"), None),
    (DIFF.replace("+++ b/calculator.py", "+++ b/test_calculator.py"), None),
    (DIFF + "\x00", None),
    (DIFF, ["calculator.py", "test_calculator.py", "third.py"]),
    (DIFF.replace("calculator.py", "setup.py"), ["setup.py"]),
    (DIFF.replace("calculator.py", ".github/workflows/ci.yml"), [".github/workflows/ci.yml"]),
    (DIFF.replace("calculator.py", "uv.lock"), ["uv.lock"]),
    (DIFF + DIFF, None),
    (DIFF.replace("+1,2", "+2,2"), None),
    (DIFF.replace("a + b", "a - b"), None),
])
def test_rejects_unsafe_or_malformed(context, tmp_path, diff, targets):
    result = validate_patch(proposal(diff, targets), context, tmp_path)
    assert not result.valid
    assert result.errors


def test_valid_minimal_patch(context, tmp_path):
    result = validate_patch(proposal(), context, tmp_path)
    assert result.valid and result.changed_lines == 2
    assert result.modified_files == ["calculator.py"]
    assert validate_patch(proposal("diff --git a/calculator.py b/calculator.py\nindex abc..def 100644\n" + DIFF), context, tmp_path).valid


def test_configurable_limits(context, tmp_path):
    assert not validate_patch(proposal(), context, tmp_path, PatchLimits(max_changed_lines=1)).valid
    assert not validate_patch(proposal(), context, tmp_path, PatchLimits(max_diff_bytes=5)).valid


@pytest.mark.parametrize("invalid", [False, True])
def test_orchestrator_preserves_chain(context, tmp_path, invalid):
    for file in context.files:
        (tmp_path / file.path).write_text(file.content, encoding="utf-8", newline="")
    diagnosis = DiagnosisAttempt(
        sandbox=SandboxResult(exit_code=1, stdout="failure", stderr="", duration_ms=1, command=["docker"]),
        context=context, status="diagnosed", diagnosis=Diagnosis(
            failure_summary="failure", suspected_files=["calculator.py"], suspected_symbols=["add"],
            root_cause_hypothesis="subtracts", supporting_evidence=["-1 != 5"], confidence=0.9,
            recommended_next_action="Review arithmetic",
        ),
    )
    candidate = proposal("bad\n" if invalid else DIFF)
    llm = Mock(complete_structured=AsyncMock(return_value=CompletionResult(
        model="nemotron", content=candidate.model_dump_json())))
    sandbox = Mock()
    agent = GhostFixOrchestrator(sandbox, llm)
    result = asyncio.run(agent.propose_patch(diagnosis, RepositoryContext(tmp_path)))
    assert result.diagnosis_attempt == diagnosis
    assert result.proposal == candidate
    assert result.validation_result.valid == (not invalid)
    assert result.completed_at
    sandbox.run.assert_not_called()
    assert (tmp_path / "calculator.py").read_text() == SOURCE
    payload = json.loads(llm.complete_structured.call_args.args[0][1]["content"])
    assert "command" not in payload["evidence_and_context"]
    assert payload["numbered_lines"]["calculator.py"][0].startswith("1: ")
    assert llm.complete_structured.call_args.kwargs == {"multiline": True}
    llm.complete_structured.assert_awaited_once()


def test_no_call_without_valid_diagnosis(tmp_path):
    llm = Mock(complete_structured=AsyncMock())
    diagnosis = DiagnosisAttempt(sandbox=SandboxResult(
        exit_code=125, stdout="", stderr="", command=[], duration_ms=0))
    result = asyncio.run(GhostFixOrchestrator(Mock(), llm).propose_patch(diagnosis, RepositoryContext(tmp_path)))
    assert not result.validation_result.valid
    llm.complete_structured.assert_not_called()


def test_default_changed_line_limit(context, tmp_path):
    context.files[0].content = "x = 1\n" * 51
    diff = ("--- a/calculator.py\n+++ b/calculator.py\n@@ -1,51 +1,51 @@\n"
            + "-x = 1\n" * 51 + "+x = 2\n" * 51)
    result = validate_patch(proposal(diff), context, tmp_path)
    assert not result.valid and "Changed-line" in result.errors[0]


def test_rejects_whole_file_deletion(context, tmp_path):
    diff = "--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +0,0 @@\n-def add(a, b):\n-    return a - b\n"
    assert not validate_patch(proposal(diff), context, tmp_path).valid


def test_multiple_hunks_and_two_files(context, tmp_path):
    context.files[0].content = "a = 1\nb = 2\nc = 3\n"
    diff = ("--- a/calculator.py\n+++ b/calculator.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
            "@@ -3 +3 @@\n-c = 3\n+c = 4\n"
            "--- a/test_calculator.py\n+++ b/test_calculator.py\n@@ -1 +1 @@\n"
            "-assert add(2,3) == 5\n+assert add(2, 3) == 5\n")
    assert validate_patch(proposal(diff, ["calculator.py", "test_calculator.py"]), context, tmp_path).valid


def test_rejects_unsupported_test_reference(context, tmp_path):
    candidate = proposal()
    candidate.tests_to_run = ["pytest; dangerous-command"]
    assert not validate_patch(candidate, context, tmp_path).valid
