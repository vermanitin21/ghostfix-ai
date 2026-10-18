import asyncio
import ast
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.agent.patches import patched_contents
from app.agent.verification import fixture_snapshot
from app.evaluation.corpus import (
    CorpusValidationRun, corpus_fingerprint, fixture_hashes, read_reference,
    reference_attempt, validate_case, validate_corpus,
)
from app.evaluation.models import BenchmarkCase, BenchmarkManifest
from app.evaluation.runner import fixture_repository, pipeline_repair
from app.sandbox.models import SandboxResult

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
REFERENCES = ROOT / "benchmarks/reference_fixes"
MANIFEST = BenchmarkManifest.model_validate_json((ROOT / "benchmarks/manifest.json").read_text())
CASES = MANIFEST.cases


def evidence(code=0, failures=(), **kwargs):
    return SandboxResult(exit_code=code, stdout="synthetic corpus test evidence", stderr="", duration_ms=1,
                         command=["python", "-m", "pytest"], failed_tests=list(failures), **kwargs)


def test_inventory_and_source_quality():
    assert len(CASES) == sum(c.enabled for c in CASES) == 7
    assert [c.id for c in CASES] == sorted({c.id for c in CASES})
    assert sorted(c.difficulty for c in CASES) == ["easy"] * 2 + ["harder"] * 2 + ["medium"] * 3
    for case in CASES:
        repository = fixture_repository(FIXTURES, case, ())
        snapshot = fixture_snapshot(repository)
        assert 2 <= len(snapshot) <= 3
        assert case.bug_category and case.description
        tests = [node.name for name, data in snapshot.items() if name.startswith("test_")
                 for node in ast.parse(data.decode()).body if isinstance(node, ast.FunctionDef)]
        assert len(tests) >= 4
        assert all(selector.split("::")[1] in tests for selector in case.expected_failing_tests)
        for data in snapshot.values():
            ast.parse(data.decode())  # Syntax inspection only; never import fixture modules.
            assert b"# BUG" not in data and b"reference_patch" not in data


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_reference_application_validation_and_cleanup(case, monkeypatch):
    import subprocess
    import socket
    repository = fixture_repository(FIXTURES, case, ())
    before = fixture_hashes(repository)
    diff = read_reference(REFERENCES, FIXTURES, case)
    broken = evidence(1, case.expected_failing_tests)
    attempt = reference_attempt(case, repository, broken, diff)
    assert attempt.validation_result.valid
    expected = fixture_snapshot(repository)
    changes = patched_contents(attempt.proposal, attempt.diagnosis_attempt.context, repository.root,
                               attempt.validation_result.limits)
    for name, content in changes.items():
        expected[name] = content.encode()
    temporary = []
    def run(path, selectors=None):
        if path == repository.root:
            assert selectors is None
            return broken
        temporary.append(path)
        actual = {name: data.replace(b"\r\n", b"\n") for name, data in fixture_snapshot(
            type(repository)(path)).items()}
        assert actual == {name: data.replace(b"\r\n", b"\n") for name, data in expected.items()}
        assert selectors is None or selectors == case.expected_failing_tests
        return evidence()
    forbidden = Mock(side_effect=AssertionError("Host/network execution prohibited"))
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    result = validate_case(case, FIXTURES, REFERENCES, SimpleNamespace(run=run))
    assert result.valid and result.broken_target_failed and result.broken_failure_exit_code == 1
    assert result.reference_patch_applied and result.repaired_target_passed and result.repaired_regression_passed
    assert result.original_unchanged and result.workspace_cleaned
    assert len(temporary) == 2 and all(not path.exists() for path in temporary)
    assert before == fixture_hashes(repository)
    assert all(value not in result.model_dump_json() for value in ("C:", "/tmp/", "ghostfix-verify"))
    forbidden.assert_not_called()


@pytest.mark.parametrize("broken", [evidence(), evidence(2), evidence(1, ["wrong.py::test_wrong"]),
    evidence(1, [], infrastructure_error="failure"), evidence(124, [], timed_out=True)])
def test_bad_reproduction_rejected(broken):
    sandbox = Mock()
    sandbox.run.return_value = broken
    result = validate_case(CASES[0], FIXTURES, REFERENCES, sandbox)
    assert not result.valid and not result.broken_target_failed
    sandbox.run.assert_called_once()


@pytest.mark.parametrize("stage", ["targeted", "regression"])
def test_failed_reference_stages_clean_up(stage):
    case = CASES[0]
    paths = []
    def run(path, selectors=None):
        if path == fixture_repository(FIXTURES, case, ()).root:
            return evidence(1, case.expected_failing_tests)
        paths.append(path)
        return evidence(1, case.expected_failing_tests) if stage == "targeted" or selectors is None else evidence()
    result = validate_case(case, FIXTURES, REFERENCES, SimpleNamespace(run=run))
    assert not result.valid and result.original_unchanged and result.workspace_cleaned
    assert not result.repaired_regression_passed and all(not path.exists() for path in paths)


def test_reference_isolation_and_context():
    assert not REFERENCES.resolve().is_relative_to(FIXTURES.resolve())
    for case in CASES:
        repository = fixture_repository(FIXTURES, case, ())
        context = repository.read_files(sorted(fixture_snapshot(repository)))
        contents = "\n".join(file.content for file in context)
        assert read_reference(REFERENCES, FIXTURES, case) not in contents
        assert "--- a/" not in contents and "+++ b/" not in contents
        with pytest.raises(ValueError):
            repository.read_files(["../../benchmarks/reference_fixes/" + case.id + ".diff"])
    with pytest.raises(ValidationError):
        BenchmarkCase.model_validate({**CASES[0].model_dump(), "fixture_path": "../benchmarks/reference_fixes"})
    with pytest.raises(ValueError):
        read_reference(FIXTURES, FIXTURES, CASES[0])


def test_adapter_supplies_only_fixture_context():
    for case in CASES:
        repository = fixture_repository(FIXTURES, case, ())
        agent = SimpleNamespace(analyze=AsyncMock(return_value="diagnosis"))
        loop = SimpleNamespace(run=AsyncMock(return_value="run"))
        assert asyncio.run(pipeline_repair(agent, loop)(case, repository)) == "run"
        args = agent.analyze.call_args.args
        assert args[0].root == repository.root
        assert all((repository.root / path).is_relative_to(repository.root) for path in args[1])
        assert all("reference" not in path for path in args[1])


def test_fingerprint_determinism_and_semantics(tmp_path):
    copied = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, copied, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    original = corpus_fingerprint(MANIFEST, copied)
    assert original == corpus_fingerprint(MANIFEST, copied) == corpus_fingerprint(MANIFEST, FIXTURES)
    reordered = MANIFEST.model_copy(update={"cases": list(reversed(CASES))})
    assert corpus_fingerprint(reordered, copied) == original
    changed = MANIFEST.model_copy(update={"name": "Changed semantic metadata"})
    assert corpus_fingerprint(changed, copied) != original
    references = tmp_path / "reference_fixes"
    shutil.copytree(REFERENCES, references)
    (references / f"{CASES[0].id}.diff").write_text("changed reference")
    assert corpus_fingerprint(MANIFEST, copied) == original
    source = copied / CASES[0].fixture_path / "users.py"
    source.write_text(source.read_text() + "\n")
    assert corpus_fingerprint(MANIFEST, copied) != original


def test_duplicate_ids_disabled_cases_and_round_trip():
    with pytest.raises(ValidationError):
        BenchmarkManifest(name="duplicate", cases=[CASES[0], CASES[0]])
    disabled = MANIFEST.model_copy(update={"cases": [c.model_copy(update={"enabled": False}) for c in CASES]})
    sandbox = Mock()
    result = validate_corpus(disabled, FIXTURES, REFERENCES, sandbox)
    assert result.case_count == result.valid_count == result.invalid_count == 0
    sandbox.run.assert_not_called()
    assert CorpusValidationRun.model_validate_json(result.model_dump_json()) == result


def test_no_benchmark_specific_repair_branches():
    for directory in ("agent", "llm", "sandbox", "tools"):
        text = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "app" / directory).glob("*.py"))
        for case in CASES:
            assert case.id not in text
            assert case.fixture_path not in text
            for selector in case.expected_failing_tests:
                assert selector not in text


def test_qa_has_no_model_or_host_execution_imports():
    from app.evaluation import corpus
    source = Path(corpus.__file__).read_text()
    tree = ast.parse(source)
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) and
                   any(term in ast.unparse(node) for term in ("subprocess", "nebius", "orchestrator"))
                   for node in ast.walk(tree))


def test_malformed_reference_never_reaches_application(tmp_path):
    (tmp_path / f"{CASES[0].id}.diff").write_text("not a patch")
    sandbox = Mock()
    sandbox.run.return_value = evidence(1, CASES[0].expected_failing_tests)
    result = validate_case(CASES[0], FIXTURES, tmp_path, sandbox)
    assert not result.valid and not result.reference_patch_applied and result.original_unchanged
    sandbox.run.assert_called_once()


def test_exception_details_never_serialized():
    sandbox = Mock()
    sandbox.run.side_effect = RuntimeError("NEBIUS_API_KEY=secret C:/Users/private")
    result = validate_case(CASES[0], FIXTURES, REFERENCES, sandbox)
    assert not result.valid and result.original_unchanged
    assert all(value not in result.model_dump_json() for value in ("NEBIUS_API_KEY", "secret", "C:/Users"))
