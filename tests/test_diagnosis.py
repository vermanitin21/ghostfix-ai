import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent.orchestrator import GhostFixOrchestrator
from app.agent.state import Diagnosis
from app.core.config import Settings
from app.llm.nebius_client import CompletionResult, NebiusClient
from app.sandbox.models import SandboxResult
from app.tools.files import RepositoryContext


PAYLOAD = dict(
    failure_summary="Addition test fails", suspected_files=["calculator.py"],
    suspected_symbols=["add"], root_cause_hypothesis="Subtraction is performed",
    supporting_evidence=["add(2, 3) returned -1, expected 5"], confidence=0.99,
    recommended_next_action="Review the arithmetic operation in add",
)


@pytest.fixture
def repository(tmp_path):
    (tmp_path / "calculator.py").write_text("def add(a, b): return a - b", encoding="utf-8")
    return RepositoryContext(tmp_path, ("test-secret",))


def evidence(**kwargs):
    return SandboxResult(exit_code=kwargs.get("exit_code", 1), stdout="test-secret " + "x" * 5000,
                         stderr="", duration_ms=1, command=["docker"], failed_tests=["test_add"])


@pytest.mark.parametrize("content,success", [
    (json.dumps(PAYLOAD), True), ("```json\n{}\n```", False),
    (json.dumps({**PAYLOAD, "confidence": 2}), False),
    (json.dumps({**PAYLOAD, "suspected_files": ["../outside.py"]}), False),
    (json.dumps({**PAYLOAD, "patch": "unwanted"}), False),
])
def test_analysis_preserves_evidence_and_validates(repository, content, success):
    original = evidence()
    sandbox = Mock(run=Mock(return_value=original))
    llm = Mock(complete_structured=AsyncMock(return_value=CompletionResult(model="nemotron", content=content)))
    agent = GhostFixOrchestrator(sandbox, llm)
    result = asyncio.run(agent.analyze(repository, ["calculator.py"]))
    assert result.sandbox == original
    assert result.context is not None
    assert (result.status == "diagnosed") == success
    assert (result.diagnosis is not None) == success
    assert "test-secret" not in result.context.model_dump_json()
    assert len(result.context.stdout_excerpt) <= 2000
    messages, schema = llm.complete_structured.call_args.args
    assert schema is Diagnosis
    assert "UNTRUSTED" in messages[0]["content"]
    assert "command" not in json.loads(messages[1]["content"])


def test_no_model_call_on_sandbox_failure(repository):
    llm = Mock(complete_structured=AsyncMock())
    agent = GhostFixOrchestrator(Mock(run=Mock(return_value=evidence(exit_code=125))), llm)
    result = asyncio.run(agent.analyze(repository, ["calculator.py"]))
    assert result.status == "failed"
    llm.complete_structured.assert_not_called()


@pytest.mark.parametrize("name", ["../outside.py", "/etc/passwd", "C:/keys.py", ".env", ".env.py",
                                     "secrets.py", "config/credentials.txt", "file.py:stream", "file.bin"])
def test_context_rejects_unsafe_paths(repository, name):
    with pytest.raises((ValueError, OSError)):
        repository.read_files([name])


@pytest.mark.parametrize("data", [b"\x00binary", b"\xff", b"x" * 4097])
def test_context_rejects_binary_and_large_files(repository, data):
    (repository.root / "bad.py").write_bytes(data)
    with pytest.raises(ValueError):
        repository.read_files(["bad.py"])


def test_context_budget_and_redaction(repository):
    with pytest.raises(ValueError):
        repository.read_files(["calculator.py"] * 5)
    (repository.root / "notes.txt").write_text('API_KEY="hidden"\ntest-secret\nnormal', encoding="utf-8")
    text = repository.read_files(["notes.txt"])[0].content
    assert "hidden" not in text and "test-secret" not in text and "normal" in text
    for name in ("a.py", "b.py", "c.py"):
        (repository.root / name).write_text("x" * 3000, encoding="utf-8")
    with pytest.raises(ValueError, match="budget"):
        repository.read_files(["a.py", "b.py", "c.py"])


def test_context_rejects_links(repository, monkeypatch):
    from pathlib import Path
    import stat
    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(st_mode=stat.S_IFLNK))
    with pytest.raises(ValueError, match="Links"):
        repository.read_files(["calculator.py"])


@pytest.mark.parametrize("finish,content,refusal", [
    ("stop", json.dumps(PAYLOAD), None), ("length", json.dumps(PAYLOAD), None),
    ("stop", "{}", None), ("stop", None, "refused"),
])
def test_structured_client_contract(monkeypatch, finish, content, refusal):
    create = AsyncMock(return_value=SimpleNamespace(model="returned-model", choices=[
        SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content, refusal=refusal))
    ]))
    sdk = Mock()
    sdk.with_options.return_value.chat.completions.create = create
    monkeypatch.setattr("app.llm.nebius_client.AsyncOpenAI", Mock(return_value=sdk))
    client = NebiusClient(Settings(_env_file=None, nebius_api_key="test", nebius_model="nemotron"))
    if finish == "stop" and content == json.dumps(PAYLOAD):
        result = asyncio.run(client.complete_structured([], Diagnosis))
        assert result.model == "returned-model"
    else:
        with pytest.raises(ValueError):
            asyncio.run(client.complete_structured([], Diagnosis))
    request = create.call_args.kwargs
    assert request["response_format"]["type"] == "json_schema"
    assert request["max_tokens"] == 4096
    assert "tools" not in request
    assert "extra_body" not in request


def test_structured_client_multiline_mode(monkeypatch):
    create = AsyncMock(return_value=SimpleNamespace(model="returned-model", choices=[
        SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(PAYLOAD), refusal=None))
    ]))
    sdk = Mock()
    sdk.with_options.return_value.chat.completions.create = create
    monkeypatch.setattr("app.llm.nebius_client.AsyncOpenAI", Mock(return_value=sdk))
    client = NebiusClient(Settings(_env_file=None, nebius_api_key="test", nebius_model="nemotron"))
    asyncio.run(client.complete_structured([], Diagnosis, multiline=True))
    request = create.call_args.kwargs
    assert request["response_format"] == {"type": "json_object"}
    assert request["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert request["max_tokens"] == 4096
    assert "tools" not in request


@pytest.mark.parametrize("success", [True, False])
def test_real_script_exit_and_redaction(monkeypatch, capsys, success):
    from app.agent.state import DiagnosisAttempt
    from scripts import test_diagnosis

    attempt = DiagnosisAttempt(
        sandbox=evidence(), status="diagnosed" if success else "failed",
        diagnosis=Diagnosis(**{**PAYLOAD, "failure_summary": "test-secret"}) if success else None,
        model="nemotron" if success else None,
        error=None if success else "Schema validation failed",
    )
    client = Mock(close=AsyncMock())
    monkeypatch.setattr(test_diagnosis, "NebiusClient", Mock(return_value=client))
    monkeypatch.setattr(test_diagnosis, "get_settings", lambda: Settings(
        _env_file=None, nebius_api_key="test-secret", nebius_model="nvidia/Nemotron-3_5-Lightning",
    ))
    monkeypatch.setattr(GhostFixOrchestrator, "analyze", AsyncMock(return_value=attempt))
    assert asyncio.run(test_diagnosis.verify()) == (0 if success else 1)
    client.close.assert_awaited_once()
    output = capsys.readouterr().out
    assert "test-secret" not in output
    assert json.loads(output)["verified"] == success
