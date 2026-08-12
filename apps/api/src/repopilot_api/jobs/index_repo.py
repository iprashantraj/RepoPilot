"""arq worker function for the Phase 1 ingestion pipeline.

The actual pipeline logic lives in ``repopilot_ingestion.pipeline`` so it
stays unit-testable without arq. This module is the thin wiring that arq
picks up — startup/shutdown hooks build the shared ``LLMProvider`` once and
re-use it across jobs.
"""

from __future__ import annotations

from typing import Any, ClassVar

import structlog
from arq.connections import RedisSettings

from repopilot_api.access import ProductAccessService
from repopilot_core.settings import get_settings
from repopilot_ingestion.persist import make_engine
from repopilot_ingestion.pipeline import PipelineResult
from repopilot_ingestion.pipeline import index_repo as _index_repo

log = structlog.get_logger(__name__)


def _redis_settings_from_url() -> RedisSettings:
    """Build arq RedisSettings from Settings.redis_url.

    Without this, arq falls back to localhost:6379 and silently ignores the
    URL configured for the rest of the app.
    """
    return RedisSettings.from_dsn(get_settings().redis_url)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    ctx["settings"] = settings
    # No platform-keyed provider here: indexing runs on the reader's own keys,
    # resolved per job. A shared fallback provider would quietly put every
    # unresolvable job back on the deployment's own quota.
    ctx["access"] = ProductAccessService(engine=make_engine(settings), settings=settings)
    log.info("arq.startup")


async def shutdown(ctx: dict[str, Any]) -> None:
    access: ProductAccessService | None = ctx.get("access")
    if access is not None:
        await access.aclose()
    log.info("arq.shutdown")


async def index_repo(ctx: dict[str, Any], repo_url: str, session_id: str) -> dict[str, Any]:
    """arq job: index a GitHub repo end-to-end. Returns a JSON-able status dict.

    ``session_id`` names whose keys to index with. The keys themselves stay in
    the encrypted credential store and are read here, so the job payload in
    Redis carries no secret.
    """
    access: ProductAccessService = ctx["access"]
    provider = await access.provider_for(session_id)
    if provider is None:
        raise RuntimeError("indexing requires the reader's connected provider keys")
    result: PipelineResult = await _index_repo(
        repo_url, provider=provider, settings=ctx["settings"]
    )
    return {
        "status": result.status,
        "repo_id": result.repo_id,
        "head_sha": result.head_sha,
        "indexed_sha": result.indexed_sha,
        "remote_sha": result.remote_sha,
        "loc_total": result.loc_total,
        "chunk_count": result.chunk_count,
        "edge_count": result.edge_count,
        "message": result.message,
    }


class WorkerSettings:
    """arq discovery target. Run with: ``arq repopilot_api.jobs.index_repo.WorkerSettings``."""

    functions: ClassVar[list[Any]] = [index_repo]
    on_startup: ClassVar[Any] = startup
    on_shutdown: ClassVar[Any] = shutdown
    redis_settings: ClassVar[RedisSettings] = _redis_settings_from_url()
