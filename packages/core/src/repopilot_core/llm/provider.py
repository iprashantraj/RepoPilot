"""The single LLMProvider every agent goes through.

Responsibilities (Phase 0 deliverable per `docs/05_PHASE_PROMPTS.md`):

* Resolve a logical `ModelId` to the right physical model on the right provider.
* SQLite cache keyed on sha256(model + canonical_json(messages) + kwargs).
* Exponential backoff with jitter on 429, max N attempts (config).
* Provider fallback chain: Groq → Cerebras → Hugging Face (Inference Providers).
  If the entire chain is exhausted, raise `ProviderError`.
* Per-`ModelId` `tokens_used` counter, summed across providers.

Design notes
------------
The provider speaks OpenAI-compatible HTTP for Groq, Cerebras, and Hugging
Face's Inference Providers gateway (https://router.huggingface.co/v1). The
unified `LLMResponse` shape lets callers stay provider-agnostic.

Embeddings run **in-process** via `fastembed` (ONNX Runtime, Hugging Face
model weights, no daemon, no HTTP, no torch). This is the only embeddings
backend in v1 — there is no Ollama anywhere.

Tests in `packages/core/tests/test_llm_provider.py` cover the five Phase 0
TDD gates. The forced-429-storm test exercises the real fallback chain end
to end against an httpx mock — no shortcut around the backoff/timeout logic.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sqlite3
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Self

import httpx
import numpy
import structlog

from repopilot_core.llm.models import (
    HF_CHAT_FALLBACK,
    RESOLUTION,
    ModelBinding,
    ModelId,
    ProviderName,
)
from repopilot_core.settings import Settings, get_settings

log = structlog.get_logger(__name__)

_MODEL_FALLBACKS: dict[ModelId, tuple[ModelId, ...]] = {
    ModelId.QA_PRIMARY: (ModelId.QA_FALLBACK,),
}


# ─── Public types ──────────────────────────────────────────────────────────


class ProviderError(RuntimeError):
    """All providers in the fallback chain failed."""


class ProviderCredentialsError(ProviderError):
    """Every binding was rejected for money or auth, not for capacity.

    401/403 (bad or revoked key) and 402 (credit exhausted) are terminal: no
    retry, no fallback model and no amount of waiting changes them, and the
    reader must be told to fix the key rather than shown a degraded answer
    that blames the question.
    """

    def __init__(self, message: str, *, provider: str, status_code: int) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class TruncatedReasoningError(ProviderError):
    """The model spent its whole budget thinking and emitted no answer.

    Reasoning models put their chain of thought in a separate ``reasoning``
    field and only then write ``content``. Hit the ``max_tokens`` ceiling
    inside that field and the response comes back ``finish_reason: "length"``
    with content empty — not a malformed payload, just a budget too small for
    this model. Distinct from ``ProviderError`` so the caller can retry with
    more room instead of writing the whole binding off.
    """


class RateLimitError(RuntimeError):
    """HTTP 429 from a provider — triggers retry/fallback inside the provider.

    ``retry_after`` is the parsed ``Retry-After`` header in seconds when the
    provider supplied one (naming the exact quota-window reset), else ``None``.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    content: str

    def to_openai(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class LLMResponse:
    """Provider-agnostic response shape."""

    text: str
    model: ModelId
    provider: ProviderName
    physical_model: str
    prompt_tokens: int
    completion_tokens: int
    cached: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# nomic-embed-text-v1.5 is trained with task prefixes and is asymmetric: the
# corpus side must be embedded as ``search_document:`` and the query side as
# ``search_query:``. Embedding both sides bare — which this codebase did until
# 2026-08-06 — puts every comparison in a regime the model was not trained for.
# Applied at the two call sites (ingestion ``embed_chunks``, retrieval
# ``vector_search``) rather than inside the provider, so the embedding cache
# keys the prefixed text and the two roles can never collide.
EMBED_DOCUMENT_PREFIX = "search_document: "
EMBED_QUERY_PREFIX = "search_query: "


@dataclass(slots=True)
class EmbeddingResponse:
    """Provider-agnostic embedding shape."""

    vector: list[float]
    model: ModelId
    provider: ProviderName
    physical_model: str
    cached: bool = False

    @property
    def dim(self) -> int:
        return len(self.vector)


# ─── Cache ──────────────────────────────────────────────────────────────────


class _SQLiteCache:
    """Thread-safe SQLite cache keyed on the canonical request hash."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS llm_cache (
        key TEXT PRIMARY KEY,
        model TEXT NOT NULL,
        provider TEXT NOT NULL,
        physical_model TEXT NOT NULL,
        response_text TEXT NOT NULL,
        prompt_tokens INTEGER NOT NULL,
        completion_tokens INTEGER NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS embedding_cache (
        key TEXT PRIMARY KEY,
        model TEXT NOT NULL,
        provider TEXT NOT NULL,
        physical_model TEXT NOT NULL,
        vector_json TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    """

    # Entries older than this are dropped when the cache is opened. `created_at`
    # was recorded from the start and never read, so the file grew for the life
    # of the deployment. Embeddings are the expensive half to rebuild, so both
    # tables share a generous window rather than expiring on model changes.
    _MAX_AGE_SECONDS = 30 * 24 * 60 * 60

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = asyncio.Lock()
        with self._connect() as conn:
            conn.executescript(self._SCHEMA)
            self._prune(conn)

    def _prune(self, conn: sqlite3.Connection) -> None:
        cutoff = time.time() - self._MAX_AGE_SECONDS
        conn.execute("DELETE FROM llm_cache WHERE created_at < ?", (cutoff,))
        conn.execute("DELETE FROM embedding_cache WHERE created_at < ?", (cutoff,))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    async def get(self, key: str) -> LLMResponse | None:
        async with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT model, provider, physical_model, response_text, "
                    "       prompt_tokens, completion_tokens "
                    "FROM llm_cache WHERE key = ?",
                    (key,),
                ).fetchone()
        if row is None:
            return None
        model_s, provider_s, physical, text, ptoks, ctoks = row
        return LLMResponse(
            text=text,
            model=ModelId(model_s),
            provider=ProviderName(provider_s),
            physical_model=physical,
            prompt_tokens=int(ptoks),
            completion_tokens=int(ctoks),
            cached=True,
        )

    async def put(self, key: str, response: LLMResponse) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO llm_cache "
                    "(key, model, provider, physical_model, response_text, "
                    " prompt_tokens, completion_tokens, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        response.model.value,
                        response.provider.value,
                        response.physical_model,
                        response.text,
                        response.prompt_tokens,
                        response.completion_tokens,
                        time.time(),
                    ),
                )

    async def get_embedding(self, key: str) -> EmbeddingResponse | None:
        async with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT model, provider, physical_model, vector_json "
                    "FROM embedding_cache WHERE key = ?",
                    (key,),
                ).fetchone()
        if row is None:
            return None
        model_s, provider_s, physical, vector_json = row
        return EmbeddingResponse(
            vector=json.loads(vector_json),
            model=ModelId(model_s),
            provider=ProviderName(provider_s),
            physical_model=physical,
            cached=True,
        )

    async def put_embedding(self, key: str, response: EmbeddingResponse) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO embedding_cache "
                    "(key, model, provider, physical_model, vector_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        response.model.value,
                        response.provider.value,
                        response.physical_model,
                        json.dumps(response.vector, separators=(",", ":")),
                        time.time(),
                    ),
                )


def _cache_key(model: ModelId, messages: Sequence[Message], kwargs: dict[str, Any]) -> str:
    payload = {
        "model": model.value,
        "messages": [m.to_openai() for m in messages],
        "kwargs": kwargs,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _embedding_cache_key(model: ModelId, text: str) -> str:
    canonical = json.dumps(
        {"model": model.value, "text": text},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ─── Backoff ────────────────────────────────────────────────────────────────


def _backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with full jitter. attempt=0 is the first retry."""
    expo = min(cap, base * (2**attempt))
    return random.uniform(0.0, expo)


def _is_rate_limited(resp: httpx.Response) -> bool:
    """True when the provider is refusing for quota, whatever status it used.

    Groq reports a tokens-per-minute overrun as **413**, not 429, with
    ``code: "rate_limit_exceeded"`` in the body. Read as a transport error it
    skipped the backoff, ignored ``Retry-After``, and surfaced to the reader as
    "413 Payload Too Large" — on a free-tier key that is what every second
    question looked like. A 413 without the rate-limit marker is a genuinely
    oversized body and stays a hard error: waiting never shrinks it.
    """
    if resp.status_code == 429:
        return True
    if resp.status_code != 413:
        return False
    try:
        error = resp.json().get("error") or {}
    except (ValueError, AttributeError):
        return False
    blob = f"{error.get('code', '')} {error.get('message', '')}".lower()
    return "rate_limit" in blob or "per minute" in blob


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP-date) into seconds.

    Providers emit the numeric delta-seconds form; the HTTP-date form is also
    RFC-legal, so honor both. Returns ``None`` for missing/garbage/negative
    values so the caller falls back to jittered backoff.
    """
    if not value:
        return None
    value = value.strip()
    try:
        secs = float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        secs = (parsed - datetime.now(UTC)).total_seconds()
    return secs if secs > 0 else None


# ─── Provider HTTP clients ──────────────────────────────────────────────────


class _BaseClient:
    """Common interface for provider HTTP shims."""

    provider: ProviderName

    async def chat(
        self,
        binding: ModelBinding,
        messages: Sequence[Message],
        kwargs: dict[str, Any],
    ) -> LLMResponse:
        raise NotImplementedError

    def supports_streaming(self) -> bool:
        return False

    def stream(
        self,
        binding: ModelBinding,
        messages: Sequence[Message],
        kwargs: dict[str, Any],
    ) -> AsyncIterator[str]:
        raise NotImplementedError


class _OpenAICompatibleClient(_BaseClient):
    """Speaks the OpenAI chat-completions shape. Used for Groq and Cerebras."""

    def __init__(
        self,
        provider: ProviderName,
        http: httpx.AsyncClient,
        base_url: str,
        api_key: str,
    ) -> None:
        self.provider = provider
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def chat(
        self,
        binding: ModelBinding,
        messages: Sequence[Message],
        kwargs: dict[str, Any],
    ) -> LLMResponse:
        body = {
            "model": binding.physical_model,
            "messages": [m.to_openai() for m in messages],
            **kwargs,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        resp = await self._http.post(
            f"{self._base_url}/chat/completions",
            json=body,
            headers=headers,
        )
        if _is_rate_limited(resp):
            raise RateLimitError(
                f"{self.provider.value} returned {resp.status_code} (quota)",
                retry_after=_parse_retry_after(resp.headers.get("retry-after")),
            )
        resp.raise_for_status()
        data = resp.json()
        text = _extract_openai_compatible_text(data)
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            model=ModelId.INTENT_PROFILER,  # overwritten by caller; see provider.generate
            provider=self.provider,
            physical_model=binding.physical_model,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )

    def supports_streaming(self) -> bool:
        return True

    async def stream(
        self,
        binding: ModelBinding,
        messages: Sequence[Message],
        kwargs: dict[str, Any],
    ) -> AsyncIterator[str]:
        """Yield content deltas from an OpenAI-compatible SSE completion."""
        body = {
            "model": binding.physical_model,
            "messages": [m.to_openai() for m in messages],
            "stream": True,
            **kwargs,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with self._http.stream(
            "POST",
            f"{self._base_url}/chat/completions",
            json=body,
            headers=headers,
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                if _is_rate_limited(resp):
                    raise RateLimitError(
                        f"{self.provider.value} returned {resp.status_code} (quota)",
                        retry_after=_parse_retry_after(resp.headers.get("retry-after")),
                    )
                resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                for choice in chunk.get("choices") or []:
                    delta = (choice.get("delta") or {}).get("content")
                    if delta:
                        yield str(delta)


# Ceiling on the widened retry. Doubling is enough for a model that merely
# thinks longer than the caller guessed; past this the budget is not the
# problem and another provider is the better bet.
_MAX_WIDENED_TOKENS = 16384


def _widen_budget(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Same call, double the token room."""
    current = kwargs.get("max_tokens")
    if not isinstance(current, int):
        return kwargs
    return {**kwargs, "max_tokens": min(current * 2, _MAX_WIDENED_TOKENS)}


def _extract_openai_compatible_text(data: dict[str, Any]) -> str:
    """Extract assistant text from OpenAI-compatible payload variants.

    Some providers return ``message.content`` as a string, others as a list of
    typed blocks, and some non-standard fallbacks expose ``text`` on the
    choice itself. Normalize those shapes into one plain string.
    """
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError(f"provider response missing choices: {data!r}")

    choice = choices[0]
    if not isinstance(choice, dict):
        raise ProviderError(f"provider response choice must be an object: {choice!r}")

    message = choice.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
                if item.get("type") == "output_text":
                    value = item.get("text") or item.get("content")
                    if isinstance(value, str):
                        parts.append(value)
            joined = "".join(parts).strip()
            if joined:
                return joined

    text = choice.get("text")
    if isinstance(text, str) and text.strip():
        return text

    if choice.get("finish_reason") == "length":
        raise TruncatedReasoningError(
            f"provider hit max_tokens before emitting any content: {data!r}"
        )

    raise ProviderError(f"provider response missing assistant text: {data!r}")


class _FastEmbedEmbedder(_BaseClient):
    """In-process embedder using fastembed (ONNX Runtime, HF weights).

    No HTTP, no daemon, no Docker, and — unlike sentence-transformers, which
    this replaced — no torch. torch cost ~2.5 GB of image and ~2 GB resident,
    which no free host will run; the quantized ONNX build of the same
    `nomic-embed-text-v1.5` is 0.13 GB. The reranker already runs on fastembed,
    so ONNX Runtime is loaded either way.

    Still `nomic-embed-text-v1.5`: 768-dim (matches the pgvector schema), 8192
    token context (so the 150-line chunk cap never truncates), and the same
    `search_document:` / `search_query:` prefixes the call sites apply.
    Weights download from huggingface.co on first use.
    """

    provider = ProviderName.HUGGINGFACE

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        # Lazy-loaded on first use so module import stays cheap.
        self._model: Any = None
        self._load_lock = asyncio.Lock()
        self._encode_lock = asyncio.Lock()

    async def _ensure_loaded(self) -> Any:
        if self._model is None:
            async with self._load_lock:
                if self._model is None:  # double-checked locking
                    self._model = await asyncio.to_thread(self._load_model)
        return self._model

    def _load_model(self) -> Any:
        # Import inside the method so a missing optional dep doesn't blow up
        # import of repopilot_core.
        from fastembed import TextEmbedding

        # Same cache dir as the reranker (see rerank/cross_encoder.py):
        # fastembed defaults to tempfile.gettempdir(), which macOS reaps,
        # leaving a half-populated snapshot that fails the ONNX load.
        cache_dir = Path.home() / ".cache" / "repopilot" / "fastembed"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return TextEmbedding(self._model_name, cache_dir=str(cache_dir))

    async def embed(self, binding: ModelBinding, text: str) -> EmbeddingResponse:
        responses = await self.embed_many(binding, [text], batch_size=1)
        return responses[0]

    async def embed_many(
        self,
        binding: ModelBinding,
        texts: Sequence[str],
        *,
        batch_size: int,
    ) -> list[EmbeddingResponse]:
        if not texts:
            return []
        model = await self._ensure_loaded()

        def _encode() -> list[list[float]]:
            # `embed` yields numpy arrays lazily; drain the generator inside the
            # worker thread so no ONNX work lands on the event loop.
            #
            # fastembed returns raw pooled vectors, where sentence-transformers
            # was called with `normalize_embeddings=True`. Ranking is unaffected
            # (pgvector compares with `<=>`, cosine), but the stored vectors are
            # unit-length everywhere else in the system, so normalise here
            # rather than leave two conventions in the same column.
            out: list[list[float]] = []
            for vector in model.embed(list(texts), batch_size=batch_size):
                norm = float(numpy.linalg.norm(vector))
                out.append((vector / norm if norm else vector).tolist())
            return out

        async with self._encode_lock:
            vectors = await asyncio.to_thread(_encode)
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"embedding backend returned {len(vectors)} vectors for {len(texts)} texts"
            )
        return [
            EmbeddingResponse(
                vector=list(vector),
                model=ModelId.EMBEDDINGS,
                provider=self.provider,
                physical_model=binding.physical_model,
            )
            for vector in vectors
        ]

    async def chat(
        self,
        binding: ModelBinding,
        messages: Sequence[Message],
        kwargs: dict[str, Any],
    ) -> LLMResponse:
        raise NotImplementedError("fastembed embedder does not support chat completion")


# ─── LLMProvider ────────────────────────────────────────────────────────────


@dataclass
class LLMProvider:
    """Single entrypoint to every LLM call in the system."""

    settings: Settings
    http: httpx.AsyncClient
    cache: _SQLiteCache
    clients: dict[ProviderName, _BaseClient]
    embedder: _BaseClient  # always fastembed in v1
    tokens_used: dict[ModelId, int] = field(default_factory=lambda: defaultdict(int))

    # ── construction ───────────────────────────────────────────────────────

    @classmethod
    def build(
        cls,
        *,
        settings: Settings | None = None,
        http: httpx.AsyncClient | None = None,
        clients: dict[ProviderName, _BaseClient] | None = None,
        embedder: _BaseClient | None = None,
    ) -> Self:
        """Default wiring used by the app. Tests pass `clients` for full control."""
        settings = settings or get_settings()
        http = http or httpx.AsyncClient(timeout=settings.llm_request_timeout_seconds)
        if clients is None:
            clients = {}
            if settings.groq_api_key:
                clients[ProviderName.GROQ] = _OpenAICompatibleClient(
                    ProviderName.GROQ,
                    http,
                    settings.groq_base_url,
                    settings.groq_api_key,
                )
            if settings.cerebras_api_key:
                clients[ProviderName.CEREBRAS] = _OpenAICompatibleClient(
                    ProviderName.CEREBRAS,
                    http,
                    settings.cerebras_base_url,
                    settings.cerebras_api_key,
                )
            # Hugging Face Inference Providers (OpenAI-compatible gateway).
            # The chat path uses HF for any model in the resolution chain whose
            # provider is HUGGINGFACE. The embedding path is served separately
            # by the in-process fastembed embedder below.
            if settings.huggingface_api_key:
                clients[ProviderName.HUGGINGFACE] = _OpenAICompatibleClient(
                    ProviderName.HUGGINGFACE,
                    http,
                    settings.huggingface_base_url,
                    settings.huggingface_api_key,
                )
        cache = _SQLiteCache(settings.llm_cache_path)
        # The embedder loads its model lazily on first call; constructing it
        # is cheap. Tests can pass `embedder=` to override.
        if embedder is None:
            embedder = _FastEmbedEmbedder(settings.huggingface_embedding_model)
        return cls(
            settings=settings,
            http=http,
            cache=cache,
            clients=clients,
            embedder=embedder,
        )

    async def aclose(self) -> None:
        with suppress(Exception):
            await self.http.aclose()

    # ── public API ─────────────────────────────────────────────────────────

    async def generate(
        self,
        model: ModelId,
        messages: Sequence[Message],
        retry_429_attempts: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Generate a completion. Hits cache first; otherwise walks the fallback chain."""
        key = _cache_key(model, messages, kwargs)
        cached = await self.cache.get(key)
        if cached is not None:
            cached.model = model
            log.debug("llm.cache_hit", model=model.value, provider=cached.provider.value)
            return cached

        candidate_models = (model, *_MODEL_FALLBACKS.get(model, ()))
        last_error: Exception | None = None
        credentials_error: ProviderCredentialsError | None = None
        for candidate in candidate_models:
            try:
                response = await self._generate_uncached(
                    candidate,
                    messages,
                    kwargs,
                    retry_429_attempts=retry_429_attempts,
                )
            except ProviderError as exc:
                last_error = exc
                if isinstance(exc, ProviderCredentialsError):
                    credentials_error = exc
                if candidate != model:
                    log.warning(
                        "llm.model_fallback_failed",
                        requested_model=model.value,
                        fallback_model=candidate.value,
                        error=repr(exc),
                    )
                continue

            if candidate != model:
                log.info(
                    "llm.model_fallback_used",
                    requested_model=model.value,
                    fallback_model=candidate.value,
                    provider=response.provider.value,
                    physical=response.physical_model,
                )
            response.model = model
            self.tokens_used[model] += response.total_tokens
            await self.cache.put(key, response)
            return response

        if credentials_error is not None:
            raise credentials_error from last_error
        raise ProviderError(
            f"all providers failed for {model.value}: {last_error!r}"
        ) from last_error

    async def generate_stream(
        self,
        model: ModelId,
        messages: Sequence[Message],
        retry_429_attempts: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Yield a completion incrementally, for UIs that render as it arrives.

        Streaming is an optimisation on top of ``generate``, never a weaker
        substitute: a cache hit replays in one piece, and any streaming failure
        (unconfigured provider, 429, mid-stream transport error before the
        first token) falls back to the full ``generate`` chain — retries,
        provider fallback and cache write included. Only the *first* binding is
        streamed; a stream that dies after emitting text cannot be retried
        without showing the reader the answer twice, so it surfaces as an error.
        """
        key = _cache_key(model, messages, kwargs)
        cached = await self.cache.get(key)
        if cached is not None:
            log.debug("llm.cache_hit", model=model.value, provider=cached.provider.value)
            yield cached.text
            return

        chain = RESOLUTION.get(model) or ()
        binding = next(
            (
                candidate
                for candidate in chain
                if (client := self.clients.get(candidate.provider)) is not None
                and client.supports_streaming()
            ),
            None,
        )
        text = ""
        if binding is not None:
            client = self.clients[binding.provider]
            try:
                async for delta in client.stream(binding, messages, kwargs):
                    text += delta
                    yield delta
            except Exception as exc:
                if text:
                    raise ProviderError(f"stream failed mid-answer for {model.value}") from exc
                log.warning(
                    "llm.stream_failed",
                    model=model.value,
                    provider=binding.provider.value,
                    error=str(exc),
                )

        if text and binding is not None:
            # Cached so a repeat of the same question replays instantly. Token
            # counts stay 0: the streamed chunks carry no usage block, and the
            # counter is accounting, not billing.
            await self.cache.put(
                key,
                LLMResponse(
                    text=text,
                    model=model,
                    provider=binding.provider,
                    physical_model=binding.physical_model,
                    prompt_tokens=0,
                    completion_tokens=0,
                ),
            )
            return

        response = await self.generate(
            model, messages, retry_429_attempts=retry_429_attempts, **kwargs
        )
        yield response.text

    async def embed(self, text: str, *, model: ModelId = ModelId.EMBEDDINGS) -> EmbeddingResponse:
        """Embed ``text`` via the in-process fastembed embedder.

        No HTTP, no daemon, no Docker. Model weights are downloaded from
        Hugging Face on first use into the local hub cache. Cached by
        ``sha256(model + text)`` in SQLite.
        """
        return (await self.embed_many([text], model=model, batch_size=1))[0]

    async def embed_many(
        self,
        texts: Sequence[str],
        *,
        model: ModelId = ModelId.EMBEDDINGS,
        batch_size: int = 32,
    ) -> list[EmbeddingResponse]:
        """Embed texts in backend batches while caching each distinct text independently."""
        if not texts:
            return []
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")

        chain = RESOLUTION.get(model)
        if not chain:
            raise ProviderError(f"no resolution chain for {model.value}")
        binding = chain[0]
        keys = [_embedding_cache_key(model, text) for text in texts]
        unique: dict[str, str] = {}
        for key, text in zip(keys, texts, strict=True):
            unique.setdefault(key, text)

        resolved: dict[str, EmbeddingResponse] = {}
        misses: list[tuple[str, str]] = []
        for key, text in unique.items():
            cached = await self.cache.get_embedding(key)
            if cached is None:
                misses.append((key, text))
            else:
                cached.model = model
                resolved[key] = cached

        if misses:
            miss_texts = [text for _, text in misses]
            embed_many_method = getattr(self.embedder, "embed_many", None)
            try:
                if embed_many_method is not None:
                    responses: list[EmbeddingResponse] = await embed_many_method(
                        binding,
                        miss_texts,
                        batch_size=batch_size,
                    )
                else:
                    embed_method = getattr(self.embedder, "embed", None)
                    if embed_method is None:
                        raise ProviderError("embedder does not support embed()")
                    responses = [await embed_method(binding, text) for text in miss_texts]
            except (OSError, RuntimeError) as exc:
                raise ProviderError(f"embedding failed for {model.value}: {exc!r}") from exc

            if len(responses) != len(misses):
                raise ProviderError(
                    f"embedding backend returned {len(responses)} responses "
                    f"for {len(misses)} cache misses"
                )
            for (key, _), response in zip(misses, responses, strict=True):
                response.model = model
                resolved[key] = response
                await self.cache.put_embedding(key, response)

        log.debug(
            "embedding.batch_done",
            total=len(texts),
            unique=len(unique),
            cache_hits=len(unique) - len(misses),
            cache_misses=len(misses),
            batch_size=batch_size,
        )
        return [resolved[key] for key in keys]

    # ── helpers ────────────────────────────────────────────────────────────

    async def _call_with_429_retry(
        self,
        client: _BaseClient,
        binding: ModelBinding,
        messages: Sequence[Message],
        kwargs: dict[str, Any],
        *,
        max_attempts: int | None = None,
    ) -> LLMResponse:
        """Per-binding 429 retry loop with exponential backoff + jitter."""
        max_attempts = max(1, max_attempts or self.settings.llm_max_429_retries)
        attempt = 0
        while True:
            try:
                return await client.chat(binding, messages, kwargs)
            except RateLimitError as exc:
                attempt += 1
                if attempt >= max_attempts:
                    raise
                jittered = _backoff_delay(
                    attempt - 1,
                    self.settings.llm_backoff_base_seconds,
                    self.settings.llm_backoff_max_seconds,
                )
                # A provider-supplied Retry-After names the exact quota-window
                # reset, so trust it over the guess — but never wait less than
                # the jittered backoff, and cap it so a huge header can't stall.
                if exc.retry_after is not None:
                    delay = max(
                        jittered,
                        min(exc.retry_after, self.settings.llm_retry_after_cap_seconds),
                    )
                else:
                    delay = jittered
                log.info(
                    "llm.backoff",
                    provider=binding.provider.value,
                    attempt=attempt,
                    delay=round(delay, 3),
                    retry_after=exc.retry_after,
                )
                await asyncio.sleep(delay)

    async def _generate_uncached(
        self,
        model: ModelId,
        messages: Sequence[Message],
        kwargs: dict[str, Any],
        *,
        retry_429_attempts: int | None,
    ) -> LLMResponse:
        chain = RESOLUTION.get(model)
        if not chain:
            raise ProviderError(f"no resolution chain for {model.value}")

        # Opt-in last-resort HF binding, appended only when enabled AND the HF
        # client is configured — so Groq→Cerebras exhaustion falls through to HF
        # instead of crashing an eval run. Off by default (HF free tier is tiny).
        if (
            self.settings.llm_hf_chat_fallback
            and ProviderName.HUGGINGFACE in self.clients
            and model in HF_CHAT_FALLBACK
        ):
            chain = (*chain, HF_CHAT_FALLBACK[model])

        last_error: Exception | None = None
        credentials_error: ProviderCredentialsError | None = None
        for binding in chain:
            client = self.clients.get(binding.provider)
            if client is None:
                log.debug("llm.skip_unconfigured_provider", provider=binding.provider.value)
                continue
            try:
                try:
                    response = await self._call_with_429_retry(
                        client,
                        binding,
                        messages,
                        kwargs,
                        max_attempts=retry_429_attempts,
                    )
                except TruncatedReasoningError:
                    # This model reasons more verbosely than the caller
                    # budgeted for. One retry with double the room, then give
                    # up on the binding — the next one may not reason at all.
                    log.info(
                        "llm.retry_wider_budget",
                        model=model.value,
                        provider=binding.provider.value,
                        physical=binding.physical_model,
                        max_tokens=_widen_budget(kwargs).get("max_tokens"),
                    )
                    response = await self._call_with_429_retry(
                        client,
                        binding,
                        messages,
                        _widen_budget(kwargs),
                        max_attempts=retry_429_attempts,
                    )
            except TruncatedReasoningError as exc:
                last_error = exc
                log.warning(
                    "llm.truncated_reasoning",
                    model=model.value,
                    provider=binding.provider.value,
                    physical=binding.physical_model,
                )
                continue
            except RateLimitError as exc:
                last_error = exc
                log.warning(
                    "llm.provider_exhausted_retries",
                    model=model.value,
                    provider=binding.provider.value,
                    physical=binding.physical_model,
                )
                continue
            except (httpx.HTTPError, ConnectionError, OSError) as exc:
                last_error = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (
                    401,
                    402,
                    403,
                ):
                    # Billing/auth, not capacity. Remember it so the caller can
                    # tell the reader to top up or re-connect the key instead
                    # of reporting a failure the question could never fix.
                    credentials_error = ProviderCredentialsError(
                        f"{binding.provider.value} rejected the request "
                        f"({exc.response.status_code})",
                        provider=binding.provider.value,
                        status_code=exc.response.status_code,
                    )
                log.warning(
                    "llm.provider_transport_error",
                    model=model.value,
                    provider=binding.provider.value,
                    error=str(exc),
                )
                continue

            response.model = model
            return response

        if credentials_error is not None:
            raise credentials_error from last_error
        raise ProviderError(
            f"all providers failed for {model.value}: {last_error!r}"
        ) from last_error
