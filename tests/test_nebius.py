import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AuthenticationError

from app.core.config import Settings
from app.llm.nebius_client import NebiusClient
from scripts import test_nebius as smoke


@pytest.mark.parametrize("key", [None, "", "   "])
def test_rejects_missing_credentials(key):
    with pytest.raises(ValueError, match="NEBIUS_API_KEY"):
        NebiusClient(Settings(_env_file=None, nebius_api_key=key, nebius_model="test-model"))


@pytest.mark.parametrize("outcome", ["success", "mismatch", "auth", "redaction"])
def test_smoke_uses_client_and_closes_it(monkeypatch, capsys, outcome):
    settings = Settings(
        _env_file=None, nebius_api_key="test-secret", nebius_model="requested-model"
    )
    content = {
        "success": smoke.EXPECTED_RESPONSE,
        "mismatch": "unexpected",
        "auth": "",
        "redaction": "test-secret",
    }[outcome]
    create = AsyncMock(return_value=SimpleNamespace(
        model="returned-model",
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
    ))
    if outcome == "auth":
        create.side_effect = AuthenticationError(
            "test-secret", response=httpx.Response(401, request=httpx.Request("POST", "https://example.test")),
            body={"message": "test-secret"},
        )
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock())
    monkeypatch.setattr("app.llm.nebius_client.AsyncOpenAI", lambda **kwargs: sdk)
    monkeypatch.setattr(smoke, "get_settings", lambda: settings)
    # Avoid changing logging state in the test process.
    monkeypatch.setattr(smoke.logging, "disable", lambda level: None)
    assert smoke.main() == (0 if outcome == "success" else 1)
    sdk.close.assert_awaited_once()
    create.assert_awaited_once_with(
        model="requested-model",
        messages=[{"role": "user", "content": f"Reply exactly: {smoke.EXPECTED_RESPONSE}"}],
    )
    output = capsys.readouterr()
    assert "test-secret" not in output.out + output.err
    if outcome != "auth":
        assert "Model: returned-model" in output.out


def test_complete_preserves_text_interface(monkeypatch):
    client = NebiusClient(Settings(
        _env_file=None, nebius_api_key="test-secret", nebius_model="test-model"
    ))
    from app.llm.nebius_client import CompletionResult

    monkeypatch.setattr(client, "complete_with_metadata", AsyncMock(
        return_value=CompletionResult(model="test-model", content="hello")
    ))

    async def run():
        try:
            assert await client.complete([]) == "hello"
        finally:
            await client.close()

    asyncio.run(run())
