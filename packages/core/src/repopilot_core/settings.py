"""Application settings, loaded from environment / `.env` via pydantic-settings."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _find_repo_env() -> Path:
    """Walk up from this file to the repo root and return the ``.env`` path.

    Lets ``alembic`` / ``pytest`` / scripts invoked from any subdirectory
    pick up the single repo-root ``.env`` instead of silently falling back
    to defaults (which masked Neon connectivity behind a local-Postgres
    fallback during Phase 3 entry checks).
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / ".env"
        if candidate.exists():
            return candidate
    return Path(".env")


def _split_csv(value: str) -> list[str]:
    """Parse a comma-separated env var into a cleaned list."""
    return [item.strip() for item in value.split(",") if item.strip()]


def _default_scan_workers() -> int:
    # One thread per core, capped: parsing is CPU-bound but tree-sitter drops
    # the GIL, so a big machine scanning a big repo should get to use it.
    return min(16, max(1, os.cpu_count() or 1))


class Settings(BaseSettings):
    """Single source of runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=_find_repo_env(),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Environment ──────────────────────────────────────────────────────────
    repopilot_env: str = "development"
    repopilot_log_level: str = "INFO"
    repopilot_session_secret: str = "repopilot-development-session-secret"
    repopilot_session_cookie_secure: bool = False
    repopilot_web_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://127.0.0.1:3000", "http://localhost:3000"]
    )

    # ── LLM providers ────────────────────────────────────────────────────────
    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"

    cerebras_api_key: str | None = None
    cerebras_base_url: str = "https://api.cerebras.ai/v1"

    # Hugging Face Inference Providers (https://router.huggingface.co/v1) —
    # OpenAI-compatible gateway that routes to underlying model providers
    # (Cerebras, Together, Replicate, etc.). Token can be a free user token
    # from huggingface.co/settings/tokens (read scope is sufficient).
    huggingface_api_key: str | None = None
    huggingface_base_url: str = "https://router.huggingface.co/v1"

    # fastembed embedder (ONNX Runtime). Runs in-process; no daemon, no HTTP,
    # no torch. nomic-embed-text-v1.5 is 768-dim and matches the existing
    # pgvector schema; the `-Q` suffix is the quantized ONNX build of the same
    # model — 0.13 GB instead of 0.52 GB, which is what fits a free host's
    # memory. Same 8192-token context, so the 150-line chunk cap never
    # truncates, and the same `search_document:` / `search_query:` prefixes
    # (see EMBED_DOCUMENT_PREFIX). Weights download from huggingface.co on
    # first use into the fastembed cache.
    huggingface_embedding_model: str = "nomic-ai/nomic-embed-text-v1.5-Q"

    # ── LLM behaviour ────────────────────────────────────────────────────────
    llm_cache_path: Path = Field(default_factory=lambda: Path(".cache/llm.sqlite"))
    # Retry budget sized to ride out a full Groq 60s TPM window before the
    # chain falls through to Cerebras. With base=0.5, max=20, jitter, worst
    # case cumulative sleep across 8 attempts is ~80s — enough to drain a
    # 60s per-minute quota reset before escalating.
    llm_max_429_retries: int = 8
    llm_backoff_base_seconds: float = 0.5
    llm_backoff_max_seconds: float = 20.0
    # When a provider returns a `Retry-After` on 429, honor it instead of the
    # jittered backoff — it names the exact quota-window reset, so guessing is
    # strictly worse. Capped so a hostile/huge header can't stall the run.
    llm_retry_after_cap_seconds: float = 60.0
    llm_request_timeout_seconds: float = 60.0
    # Opt-in last-resort chat fallback to Hugging Face Inference Providers when
    # BOTH Groq and Cerebras 429 (e.g. an eval storm exhausts both TPM windows).
    # Default off: the HF free tier is a tiny (~$0.10/mo) credit budget a single
    # bench run can drain. Set LLM_HF_CHAT_FALLBACK=1 for eval runs where you'd
    # rather spend HF credits than crash. Requires HUGGINGFACE_API_KEY.
    llm_hf_chat_fallback: bool = False
    # Cap on concurrent verifier LLM calls. Unbounded `asyncio.gather` over a
    # section's claims stampedes the free-tier per-second quota so hard that
    # backoff never catches up (both Groq and Cerebras 429 at once). A small
    # cap lets the 429 backoff actually drain the quota window. 0 = unbounded.
    # 10 -> 3 on 2026-08-08: a 12-claim answer on a free-tier BYOK key sent 10
    # verifier calls at once, every one 429'd, the retries exhausted together,
    # and the whole answer rendered "Unverified" — the failure mode this cap
    # exists to prevent.
    llm_verifier_max_concurrency: int = 3

    # ── Reranking (RAG Phase 4) ──────────────────────────────────────────────
    # Cross-encoder over the post-retrieval pool. MiniLM-L-6-v2 is 80 MB ONNX,
    # ~460 pairs/s on CPU; BAAI/bge-reranker-base (1 GB) is the quality
    # fallback if the pairwise self-test flags MiniLM as code-mismatched.
    # Defaults benched in Phase 4 (lambda x pool sweep); lambda=0.9 is the
    # min-regret diversity setting (recall up on every dataset).
    #
    # pool 50 -> 25 on 2026-08-04. Rerank was measured as the dominant latency
    # stage (~2.1s/query, 53% of wall-clock: ~990ms reading the pool, ~1265ms
    # scoring it). Sweeping 50/35/25/15 held retrieval quality flat down to 25
    # while saving ~933ms/query:
    #
    #   pool | httpx r@10 (n=13) | flask r@10 (n=17) | rare r@10 (n=12) | rerank ms
    #     50 |             0.974 |             0.868 |            1.000 |      2082
    #     25 |             0.974 |             0.868 |            1.000 |      1149
    #     15 |             0.949 |             0.917 |            1.000 |       873
    #
    # 15 is not safe: it costs httpx recall. Phase 4's rare-symbol win survives
    # at every pool size, but that set is saturated at 1.000 and could not have
    # detected damage -- the choice rests on httpx and flask.
    rerank_enabled: bool = True
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    rerank_max_pool: int = 25
    rerank_lambda: float = 0.9

    # ── Context compression (RAG Phase 5) ───────────────────────────────────
    # Off by default. Measured on fastapi 2026-08-04: +5.6% input-token
    # reduction against Phase 5's -40% gate, at multiple seconds per question.
    # The failure is not the documented 413 (zero payload errors observed) --
    # the model simply returns no usable keep-ranges for most chunks, so
    # ``compress_chunk`` hands back the original untouched. Code and the
    # ``use_compress`` kwarg are retained so this can be re-measured after
    # prompt or chunk-splitting work; flip this to re-enable.
    compress_enabled: bool = False
    compress_min_chunk_lines: int = 15

    # ── Query understanding (RAG Phase 2) ───────────────────────────────────
    query_understanding_enabled: bool = False
    query_understanding_max_rewrites: int = 3

    # ── Interactive Q&A ─────────────────────────────────────────────────────
    # UI asks should fail over quickly instead of spending a click-and-wait path
    # on the long retry budget used by indexing/evals. Compression is gated by
    # ``compress_enabled`` above (now off), so this flag is belt-and-braces:
    # ``use_compress=True`` alone no longer enables compression for any caller.
    # 1 = no 429 sleep at all on the click-and-wait path. A provider's
    # Retry-After in the free tier is 50-60s, so retrying in-request only turns
    # a fast fail-over into a two-minute hang the web proxy kills anyway: groq
    # 429 now falls straight through to cerebras, and both exhausted flags the
    # claim instead of waiting out the quota window.
    qa_llm_max_429_retries: int = 1
    qa_compress_enabled: bool = False

    # ── GitHub ───────────────────────────────────────────────────────────────
    github_pat: str | None = None

    # ── Ingestion (Phase 1) ──────────────────────────────────────────────────
    ingestion_clone_root: Path = Field(default_factory=lambda: Path(".cache/clones"))
    ingestion_max_repo_loc: int = 200_000
    ingestion_scan_workers: int = Field(default_factory=_default_scan_workers, ge=1, le=32)
    ingestion_max_file_bytes: int = Field(default=512_000, ge=1)
    ingestion_text_chunk_lines: int = Field(default=120, ge=1)
    ingestion_text_chunk_chars: int = Field(default=6_000, ge=256)
    ingestion_text_chunk_overlap_lines: int = Field(default=10, ge=0)
    # Summaries are one network round-trip per chunk and dominate wall-clock on
    # large repos — the scan is threaded and embedding is batched, this stage is
    # neither. It is latency-bound, not CPU-bound, so concurrency is the only
    # lever. ``summarise_chunks`` tolerates a burst of provider errors before
    # tripping its circuit, so a rate limit at this width costs retries rather
    # than every remaining summary.
    ingestion_summary_concurrency: int = 24
    # Measured on the local nomic backend: 8 outperformed 16/32 for realistic
    # code chunks while avoiding the memory spikes of larger batches.
    ingestion_embed_batch_size: int = Field(default=8, ge=1)
    # Retained for environment compatibility. Local sentence-transformer
    # throughput comes from ``ingestion_embed_batch_size`` rather than running
    # concurrent model.encode calls against one model instance.
    ingestion_embed_concurrency: int = 4
    # Phase 6 found synthetic prefixes can hurt dense ordering. Keep raw-source
    # embeddings by default; ``enriched_text`` still feeds the BM25 tsvector.
    ingestion_embed_enriched_text: bool = False

    # ── Datastores ───────────────────────────────────────────────────────────
    postgres_dsn: str = "postgresql+psycopg://repopilot:repopilot@localhost:5432/repopilot"
    redis_url: str = "redis://localhost:6379/0"

    # ── LangSmith (optional) ─────────────────────────────────────────────────
    langsmith_api_key: str | None = None
    langsmith_project: str = "repopilot-dev"

    @field_validator("repopilot_web_origins", mode="before")
    @classmethod
    def _coerce_web_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return _split_csv(value)
        return value

    @model_validator(mode="after")
    def _require_production_session_secret(self) -> Settings:
        if (
            self.repopilot_env == "production"
            and self.repopilot_session_secret == "repopilot-development-session-secret"
        ):
            raise ValueError("REPOPILOT_SESSION_SECRET must be set in production")
        if self.repopilot_env == "production" and not self.repopilot_session_cookie_secure:
            raise ValueError("REPOPILOT_SESSION_COOKIE_SECURE must be true in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Tests should call `get_settings.cache_clear()`."""
    return Settings()
