"""OpenAI-compatible client abstraction for Nebius Token Factory."""

from collections.abc import Sequence
from typing import Protocol

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from app.core.config import Settings


class CompletionResult(BaseModel):
    model: str
    content: str


class StructuredOutputError(ValueError):
    """Safe, locally generated error without API response contents."""


class LLMClient(Protocol):
    async def complete(self, messages: Sequence[ChatCompletionMessageParam]) -> str:
        """Return a text completion for a chat conversation."""
        ...


class StructuredLLMClient(Protocol):
    async def complete_structured(
        self, messages: Sequence[ChatCompletionMessageParam], schema: type[BaseModel],
        *, multiline: bool = False,
    ) -> CompletionResult: ...


class NebiusClient:
    """Small adapter around the OpenAI-compatible Nebius API."""

    def __init__(self, settings: Settings) -> None:
        if settings.nebius_api_key is None or not settings.nebius_api_key.get_secret_value().strip():
            raise ValueError("NEBIUS_API_KEY is required to create the LLM client")
        if not settings.nebius_model.strip():
            raise ValueError("NEBIUS_MODEL is required to create the LLM client")

        self._model = settings.nebius_model
        self._client = AsyncOpenAI(
            api_key=settings.nebius_api_key.get_secret_value(),
            base_url=settings.nebius_base_url,
        )

    async def complete(
        self,
        messages: Sequence[ChatCompletionMessageParam],
    ) -> str:
        return (await self.complete_with_metadata(messages)).content

    async def complete_with_metadata(
        self,
        messages: Sequence[ChatCompletionMessageParam],
    ) -> CompletionResult:
        """Return text and the model identifier reported by the API."""
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=list(messages),
        )
        if not response.choices:
            raise ValueError("Nebius returned no completion choices")
        return CompletionResult(
            model=response.model,
            content=response.choices[0].message.content or "",
        )

    async def complete_structured(
        self, messages: Sequence[ChatCompletionMessageParam], schema: type[BaseModel],
        *, multiline: bool = False,
    ) -> CompletionResult:
        if multiline:
            # Strict-schema decoding drops newline escapes from strings, and reasoning
            # exhausts the budget on diffs. The caller's prompt must carry the schema;
            # the response is still validated locally below.
            options: dict = {
                "response_format": {"type": "json_object"},
                "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
            }
        else:
            options = {"response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "strict": True,
                                "schema": schema.model_json_schema()},
            }}
        response = await self._client.with_options(timeout=60, max_retries=0).chat.completions.create(
            model=self._model, messages=list(messages), temperature=0, max_tokens=4096, **options,
        )
        if not response.choices:
            raise StructuredOutputError("No structured completion returned")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise StructuredOutputError("Structured completion exceeded the output token budget")
        if choice.finish_reason != "stop" or choice.message.refusal or not choice.message.content:
            raise StructuredOutputError("Structured completion refused, empty, or incomplete")
        schema.model_validate_json(choice.message.content)
        return CompletionResult(model=response.model, content=choice.message.content)

    async def close(self) -> None:
        await self._client.close()
