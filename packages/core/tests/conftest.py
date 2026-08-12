"""Shared fixtures for the core package's tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from repopilot_core.llm.models import ModelId, ProviderName
from repopilot_core.llm.provider import (
    EmbeddingResponse,
    LLMProvider,
    LLMResponse,
    Message,
    _BaseClient,
    _SQLiteCache,
)
from repopilot_core.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    return Settings(
        repopilot_env="test",
        groq_api_key="test-groq",
        cerebras_api_key="test-cerebras",
        huggingface_api_key="test-hf",
        huggingface_embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        llm_cache_path=tmp_path / "llm.sqlite",
        llm_max_429_retries=3,
        llm_backoff_base_seconds=0.0,
        llm_backoff_max_seconds=0.0,
        llm_request_timeout_seconds=5.0,
    )


class FakeClient(_BaseClient):
    """Test double for an LLM provider client."""

    def __init__(self, provider: ProviderName, responses: list[object]) -> None:
        self.provider = provider
        self._responses = list(responses)
        self.calls: list[tuple[str, list[Message], dict[str, object]]] = []

    async def chat(self, binding, messages, kwargs):
        self.calls.append((binding.physical_model, list(messages), dict(kwargs)))
        if not self._responses:
            raise AssertionError(f"FakeClient({self.provider.value}) exhausted")
        head = self._responses.pop(0)
        if isinstance(head, Exception):
            raise head
        assert isinstance(head, LLMResponse)
        return head


class FakeStreamingClient(_BaseClient):
    """A client that streams, and can fail at a chosen point in the stream.

    ``error`` is raised after ``deltas`` have been yielded, so ``deltas=[]``
    models a failure before the first token (recoverable — ``generate_stream``
    falls back to the full chain) and a non-empty ``deltas`` models a failure
    mid-answer (not recoverable — the reader has already seen text).
    """

    def __init__(
        self,
        provider: ProviderName,
        deltas: list[str] | None = None,
        *,
        error: Exception | None = None,
        chat_responses: list[object] | None = None,
    ) -> None:
        self.provider = provider
        self._deltas = list(deltas or [])
        self._error = error
        self._chat_responses = list(chat_responses or [])
        self.stream_calls = 0
        self.chat_calls = 0

    def supports_streaming(self) -> bool:
        return True

    async def stream(self, binding, messages, kwargs):
        self.stream_calls += 1
        for delta in self._deltas:
            yield delta
        if self._error is not None:
            raise self._error

    async def chat(self, binding, messages, kwargs):
        self.chat_calls += 1
        if not self._chat_responses:
            raise AssertionError(f"FakeStreamingClient({self.provider.value}) chat exhausted")
        head = self._chat_responses.pop(0)
        if isinstance(head, Exception):
            raise head
        assert isinstance(head, LLMResponse)
        return head


class FakeEmbedder(_BaseClient):
    """Test double for the fastembed in-process embedder.

    Returns deterministic vectors based on text content so embeddings are
    reproducible across runs without loading a real model.
    """

    provider = ProviderName.HUGGINGFACE

    def __init__(self, dim: int = 768) -> None:
        self._dim = dim
        self.calls: list[str] = []

    async def chat(self, binding, messages, kwargs):
        raise NotImplementedError("FakeEmbedder does not support chat")

    async def embed(self, binding: Any, text: str) -> EmbeddingResponse:
        self.calls.append(text)
        # Cheap deterministic vector from a hash; not semantically meaningful
        # but stable across runs so cache hits work in tests.
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vector = [(b - 128) / 128.0 for b in digest]
        # Pad / truncate to requested dim.
        while len(vector) < self._dim:
            vector.extend(vector[: self._dim - len(vector)])
        vector = vector[: self._dim]
        return EmbeddingResponse(
            vector=vector,
            model=ModelId.EMBEDDINGS,
            provider=self.provider,
            physical_model=binding.physical_model,
        )


def make_provider(
    settings: Settings,
    clients: dict[ProviderName, _BaseClient],
    embedder: _BaseClient | None = None,
) -> LLMProvider:
    """Build an LLMProvider that uses the supplied fakes for every provider."""
    http = httpx.AsyncClient()
    cache = _SQLiteCache(settings.llm_cache_path)
    return LLMProvider(
        settings=settings,
        http=http,
        cache=cache,
        clients=clients,
        embedder=embedder or FakeEmbedder(),
    )


def make_response(
    *,
    provider: ProviderName,
    physical_model: str = "test-model",
    text: str = "ok",
    prompt_tokens: int = 7,
    completion_tokens: int = 3,
) -> LLMResponse:
    return LLMResponse(
        text=text,
        model=ModelId.INTENT_PROFILER,  # overwritten by provider.generate
        provider=provider,
        physical_model=physical_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
