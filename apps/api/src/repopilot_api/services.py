"""Service seams and live-ish implementations behind the Phase 4 routes."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote, unquote
from uuid import uuid4

import structlog
from arq.connections import ArqRedis, RedisSettings, create_pool
from arq.jobs import Job
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from repopilot_agents.intent.profiler import profile_intent
from repopilot_agents.qa import QAResult, TokenSink, answer_question
from repopilot_agents.state import IntentProfile
from repopilot_agents.tools.graph_query import graph_query
from repopilot_agents.tools.read_chunks import read_chunks
from repopilot_agents.types import CodeRef
from repopilot_agents.verifier.grounding import Claim as QAClaim
from repopilot_api.access import AccessService, InMemoryAccessService, ProductAccessService
from repopilot_api.models import (
    BaseTourEvent,
    ChunkPayload,
    ClaimPayload,
    GraphEdgeKind,
    GraphNeighbour,
    GraphNeighboursResponse,
    QAAnswerResponse,
    RepoStatus,
    TourDoneEvent,
    TourEventType,
    TourFirstImpressionEvent,
)
from repopilot_api.tours import InMemoryTourService, PostgresTourService, TourService
from repopilot_core.llm.provider import LLMProvider, ProviderCredentialsError, ProviderError
from repopilot_core.settings import Settings, get_settings
from repopilot_ingestion.clone import parse_github_url
from repopilot_ingestion.db import INDEX_RECIPE_VERSION
from repopilot_ingestion.db import chunk_embeddings as embeddings_table
from repopilot_ingestion.db import chunks as chunks_table
from repopilot_ingestion.db import graph_adjacency as graph_table
from repopilot_ingestion.db import repos as repos_table
from repopilot_ingestion.persist import make_engine
from repopilot_ingestion.pipeline import index_repo, revisit_status

log = structlog.get_logger(__name__)

# What a reader is told when indexing fails for a reason we cannot summarise.
# The cause goes to the log with its traceback; the reader gets something they
# can act on instead of a stringified psycopg or provider error.
INDEX_FAILED_MESSAGE = (
    "Indexing failed. Try again, and check the repository is public and reachable."
)


def repo_slug(repo_url: str) -> str:
    owner, name = parse_github_url(repo_url)
    return quote(f"{owner}/{name}", safe="")


def decode_repo_slug(repo_id: str) -> str:
    return unquote(repo_id)


def normalize_repo_id(repo_id: str) -> str:
    return quote(decode_repo_slug(repo_id), safe="")


@dataclass(slots=True)
class RepoRecord:
    repo_id: str
    repo_url: str
    status: RepoStatus
    progress: int | None = None
    error: str | None = None
    indexed_sha: str | None = None
    remote_sha: str | None = None
    commits_behind_estimate: int | None = None
    indexed_repo_id: str | None = None
    first_impression: str | None = None


class RepoService(Protocol):
    async def enqueue(
        self,
        repo_url: str,
        *,
        provider: LLMProvider | None = None,
        session_id: str | None = None,
    ) -> RepoRecord: ...

    async def get(self, repo_id: str) -> RepoRecord: ...

    def first_impression_stream(self, repo_id: str) -> AsyncIterator[BaseTourEvent]: ...


class QAService(Protocol):
    async def ask(
        self,
        repo_id: str,
        question: str,
        *,
        intent_profile: IntentProfile | None = None,
        provider: LLMProvider | None = None,
        on_token: TokenSink | None = None,
    ) -> QAAnswerResponse: ...

    async def draft_intent(
        self, raw_text: str, *, provider: LLMProvider | None = None
    ) -> IntentProfile: ...


class ChunkService(Protocol):
    async def get(self, chunk_id: str) -> ChunkPayload: ...


class GraphService(Protocol):
    async def neighbours(
        self, repo_id: str, symbol: str, *, limit: int = 60
    ) -> GraphNeighboursResponse: ...


class RepoNotReadyError(Exception):
    """Raised when a question is asked before a repo has an indexed snapshot."""

    def __init__(self, repo_id: str, status: RepoStatus) -> None:
        self.repo_id = repo_id
        self.status = status
        super().__init__(f"repo {repo_id!r} is not ready for questions; status={status!r}")


@dataclass(slots=True)
class AppServices:
    repos: RepoService
    qa: QAService
    chunks: ChunkService
    graph: GraphService | None = None
    access: AccessService = field(default_factory=InMemoryAccessService)
    tours: TourService = field(default_factory=InMemoryTourService)


@dataclass(slots=True)
class RepoSnapshot:
    repo_id: str
    repo_url: str
    head_sha: str


@dataclass(slots=True)
class Runtime:
    settings: Settings
    provider: LLMProvider


async def build_runtime() -> Runtime:
    settings = get_settings()
    provider = LLMProvider.build(settings=settings)
    return Runtime(settings=settings, provider=provider)


async def shutdown_runtime(runtime: Runtime) -> None:
    await runtime.provider.aclose()


@dataclass(slots=True)
class LiveRepoService:
    runtime: Runtime
    records: dict[str, RepoRecord] = field(default_factory=dict)
    tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    redis: ArqRedis | None = None

    async def enqueue(
        self,
        repo_url: str,
        *,
        provider: LLMProvider | None = None,
        session_id: str | None = None,
    ) -> RepoRecord:
        """Index ``repo_url`` on ``provider`` — the reader's own keys.

        ``session_id`` is passed only when the reader's keys are account-bound,
        because that is the one case the background worker can rebuild them
        from: it resolves them out of the encrypted store itself, so no API key
        is ever written to the job queue. Without it (an anonymous session,
        whose keys live in this process's memory) indexing runs here instead.
        """
        slug = repo_slug(repo_url)
        existing = await self._load_record_from_db(slug, repo_url=repo_url)
        if existing is not None and existing.status in {"ready", "stale"}:
            self.records[slug] = existing
            return existing

        record = self.records.get(slug)
        if record is None:
            record = RepoRecord(repo_id=slug, repo_url=repo_url, status="queued", progress=5)
            self.records[slug] = record
        if slug not in self.tasks or self.tasks[slug].done():
            if self.runtime.settings.repopilot_env == "production" and session_id is not None:
                self.tasks[slug] = asyncio.create_task(
                    self._enqueue_worker(slug, repo_url, session_id)
                )
            else:
                self.tasks[slug] = asyncio.create_task(
                    self._index(slug, repo_url, provider=provider)
                )
        return record

    async def _enqueue_worker(self, slug: str, repo_url: str, session_id: str) -> None:
        record = self.records[slug]
        record.status = "indexing"
        record.progress = 15
        record.first_impression = "Repository indexing is running in the background."
        progress_task = asyncio.create_task(self._advance_progress(record))
        try:
            if self.redis is None:
                self.redis = await create_pool(
                    RedisSettings.from_dsn(self.runtime.settings.redis_url)
                )
            # The session id, never the keys: the worker rehydrates them from
            # the encrypted credential store, so nothing secret enters Redis.
            job = await self.redis.enqueue_job("index_repo", repo_url, session_id)
            if job is None:
                raise RuntimeError("repository indexing job could not be enqueued")
            await self._apply_worker_result(record, job)
        except Exception:
            # The traceback goes to the log, never to the reader: `str(exc)` on
            # a redis or SQLAlchemy failure carries connection targets, and a
            # provider failure carries request detail.
            log.exception("repo.index_failed", repo_id=slug, repo_url=repo_url)
            record.status = "error"
            record.progress = None
            record.error = INDEX_FAILED_MESSAGE
        finally:
            progress_task.cancel()

    async def _apply_worker_result(self, record: RepoRecord, job: Job) -> None:
        payload = await job.result(timeout=60 * 60, poll_delay=1.0)
        if not isinstance(payload, dict):
            raise RuntimeError("repository indexing worker returned an invalid result")
        status = str(payload.get("status", "error"))
        if status == "too_large":
            record.status = "error"
            record.progress = None
            record.error = (
                str(payload.get("message") or "")
                or "Repository exceeds the configured indexing size limit."
            )
            return
        if status == "unsupported":
            record.status = "error"
            record.progress = None
            record.error = "Repository contains no supported source or context files."
            return
        snapshot_repo_id = payload.get("repo_id")
        if not isinstance(snapshot_repo_id, str) or not snapshot_repo_id:
            raise RuntimeError("repository indexing worker returned no snapshot id")
        record.progress = 100
        record.indexed_sha = str(payload.get("indexed_sha") or payload.get("head_sha") or "")
        record.remote_sha = str(payload.get("remote_sha") or payload.get("head_sha") or "")
        record.indexed_repo_id = snapshot_repo_id
        engine = make_engine(self.runtime.settings)
        try:
            record.first_impression = await self._compose_first_impression(engine, snapshot_repo_id)
        finally:
            await engine.dispose()
        record.status = "ready"

    async def get(self, repo_id: str) -> RepoRecord:
        normalized_repo_id = normalize_repo_id(repo_id)
        record = self.records.get(normalized_repo_id)
        if record is not None:
            return record
        loaded = await self._load_record_from_db(normalized_repo_id)
        if loaded is None:
            raise KeyError(normalized_repo_id)
        self.records[normalized_repo_id] = loaded
        return loaded

    async def _index(
        self, slug: str, repo_url: str, *, provider: LLMProvider | None = None
    ) -> None:
        record = self.records[slug]
        record.status = "indexing"
        record.progress = 15
        record.first_impression = "Cloning the repository and building a structural snapshot."
        progress_task = asyncio.create_task(self._advance_progress(record))
        try:
            result = await index_repo(
                repo_url,
                provider=provider or self.runtime.provider,
                settings=self.runtime.settings,
            )
            progress_task.cancel()
            if result.status == "too_large":
                record.status = "error"
                record.progress = None
                record.error = result.message or (
                    f"Repository has {result.loc_total or 0} indexable lines, exceeding the "
                    f"configured limit of {self.runtime.settings.ingestion_max_repo_loc}."
                )
                return
            if result.status == "unsupported":
                record.status = "error"
                record.progress = None
                record.error = "Repository contains no supported source or context files."
                return
            record.progress = 100
            record.indexed_sha = result.head_sha
            record.remote_sha = result.head_sha
            record.indexed_repo_id = result.repo_id
            record.first_impression = (
                f"Indexed {repo_url}. Structural snapshot is ready with "
                f"{result.chunk_count or 0} chunks and {result.edge_count or 0} graph edges."
            )
            record.status = "ready"
        except Exception:
            progress_task.cancel()
            log.exception("repo.index_failed", repo_id=slug, repo_url=repo_url)
            record.status = "error"
            record.progress = None
            record.error = INDEX_FAILED_MESSAGE

    async def _advance_progress(self, record: RepoRecord) -> None:
        while record.status == "indexing":
            await asyncio.sleep(10)
            if record.progress is None:
                record.progress = 15
            elif record.progress < 90:
                record.progress += 5

    async def _load_record_from_db(
        self,
        repo_id: str,
        *,
        repo_url: str | None = None,
    ) -> RepoRecord | None:
        normalized_repo_id = normalize_repo_id(repo_id)
        engine = make_engine(self.runtime.settings)
        try:
            resolved_url = repo_url
            if resolved_url is None:
                async with engine.connect() as conn:
                    row = (
                        await conn.execute(
                            select(repos_table.c.url)
                            .where(
                                repos_table.c.id.like(f"{decode_repo_slug(normalized_repo_id)}@%")
                            )
                            .order_by(repos_table.c.indexed_at.desc())
                            .limit(1)
                        )
                    ).first()
                resolved_url = None if row is None else str(row[0])
            if resolved_url is None:
                return None

            status = await revisit_status(repo_url=resolved_url, settings=self.runtime.settings)
            indexed_repo_id = await self._latest_repo_snapshot_id(engine, resolved_url)
            if status.status == "already_indexed":
                return RepoRecord(
                    repo_id=normalized_repo_id,
                    repo_url=resolved_url,
                    status="ready",
                    progress=100,
                    indexed_sha=status.indexed_sha,
                    remote_sha=status.remote_sha,
                    commits_behind_estimate=0,
                    indexed_repo_id=indexed_repo_id,
                    first_impression=await self._compose_first_impression(engine, indexed_repo_id),
                )
            if status.status == "stale" and indexed_repo_id is not None:
                return RepoRecord(
                    repo_id=normalized_repo_id,
                    repo_url=resolved_url,
                    status="stale",
                    progress=100,
                    indexed_sha=status.indexed_sha,
                    remote_sha=status.remote_sha,
                    commits_behind_estimate=1,
                    indexed_repo_id=indexed_repo_id,
                    first_impression=await self._compose_first_impression(engine, indexed_repo_id),
                )
            return None
        finally:
            await engine.dispose()

    async def _latest_repo_snapshot_id(self, engine: AsyncEngine, repo_url: str) -> str | None:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    select(repos_table.c.id)
                    .join(chunks_table, chunks_table.c.repo_id == repos_table.c.id)
                    .outerjoin(
                        embeddings_table,
                        embeddings_table.c.chunk_id == chunks_table.c.id,
                    )
                    .where(
                        repos_table.c.url == repo_url,
                        # A snapshot built by an older recipe is not a usable
                        # snapshot, exactly as `repo_already_indexed` and
                        # `known_head_sha` in persist.py already say. Without
                        # this the app serves stale data forever: a repo
                        # indexed at recipe 3 resolves here, `_load_record_from_db`
                        # reports it "stale" rather than absent, and `enqueue`
                        # returns early instead of rebuilding — so bumping
                        # INDEX_RECIPE_VERSION changed nothing anyone could see.
                        repos_table.c.index_version >= INDEX_RECIPE_VERSION,
                    )
                    .group_by(repos_table.c.id, repos_table.c.indexed_at)
                    .having(func.count(chunks_table.c.id) > 0)
                    .having(
                        func.count(embeddings_table.c.chunk_id) == func.count(chunks_table.c.id)
                    )
                    .order_by(repos_table.c.indexed_at.desc())
                    .limit(1)
                )
            ).first()
        return None if row is None else str(row[0])

    async def _compose_first_impression(
        self, engine: AsyncEngine, snapshot_repo_id: str | None
    ) -> str | None:
        if snapshot_repo_id is None:
            return None
        hubs = await graph_query("hubs", engine=engine, repo_id=snapshot_repo_id, top_n=3)
        entry_points = await graph_query(
            "entry_points",
            engine=engine,
            repo_id=snapshot_repo_id,
            top_n=3,
        )
        async with engine.connect() as conn:
            file_count_row = (
                await conn.execute(
                    select(repos_table.c.file_count, repos_table.c.loc_total).where(
                        repos_table.c.id == snapshot_repo_id
                    )
                )
            ).first()
        file_count = 0 if file_count_row is None else int(file_count_row[0])
        loc_total = 0 if file_count_row is None else int(file_count_row[1])
        hub_names = ", ".join(h.symbol.rsplit(".", 1)[-1] for h in hubs) or "no dominant hubs yet"
        entry_names = (
            ", ".join(e.symbol.rsplit(".", 1)[-1] for e in entry_points) or "no clear entry points"
        )
        return (
            f"Indexed snapshot across {file_count} files and {loc_total} lines. "
            f"Primary entry points: {entry_names}. Structural hubs: {hub_names}."
        )

    async def first_impression(self, repo_id: str) -> str:
        record = await self.get(repo_id)
        return (
            record.first_impression
            or "Indexing has started; first structural signals will appear shortly."
        )

    async def _first_impression_events(self, repo_id: str) -> AsyncIterator[TourEventType]:
        text = await self.first_impression(repo_id)
        yield TourFirstImpressionEvent(text=text)
        yield TourDoneEvent()

    def first_impression_stream(self, repo_id: str) -> AsyncIterator[TourEventType]:
        return self._first_impression_events(repo_id)


@dataclass(slots=True)
class LiveQAService:
    """Answers questions against an indexed repo snapshot, tilted by persona.

    Stateless by design. The old ``LiveTourService`` had to persist a tour row
    to remember the user's intent between "create" and "ask"; now the persona
    rides along with every question, so there is nothing to store and nothing
    to expire.
    """

    runtime: Runtime
    repos: LiveRepoService

    async def _snapshot_repo_id(self, repo_id: str) -> str:
        repo = await self.repos.get(repo_id)
        if repo.indexed_repo_id is None:
            raise RepoNotReadyError(repo_id, repo.status)
        return repo.indexed_repo_id

    async def draft_intent(
        self, raw_text: str, *, provider: LLMProvider | None = None
    ) -> IntentProfile:
        return await profile_intent(raw_text, provider=provider or self.runtime.provider)

    async def ask(
        self,
        repo_id: str,
        question: str,
        *,
        intent_profile: IntentProfile | None = None,
        provider: LLMProvider | None = None,
        on_token: TokenSink | None = None,
    ) -> QAAnswerResponse:
        snapshot_repo_id = await self._snapshot_repo_id(repo_id)
        engine = make_engine(self.runtime.settings)
        published = False

        async def track(text: str) -> None:
            nonlocal published
            published = True
            if on_token is not None:
                await on_token(text)

        sink: TokenSink | None = None if on_token is None else track

        async def attempt() -> QAResult:
            return await answer_question(
                question,
                engine=engine,
                provider=provider or self.runtime.provider,
                repo_id=snapshot_repo_id,
                use_compress=self.runtime.settings.qa_compress_enabled,
                retry_429_attempts=self.runtime.settings.qa_llm_max_429_retries,
                intent_profile=intent_profile,
                on_token=sink,
            )

        try:
            try:
                result = await attempt()
            except ProviderCredentialsError:
                # Money or auth, not the question. Retrying and keyword-dumping
                # both hide the one thing the reader can act on.
                raise
            except Exception:
                # Every optional stage (query understanding, hybrid's sparse
                # lane, rerank, compression, hops, the verifier) degrades in
                # place inside ``answer_question``, so reaching here means a
                # load-bearing stage failed — usually transiently. Retry once
                # with the full pipeline, advanced RAG included, before showing
                # the reader a keyword dump. Skipped when tokens already
                # streamed: the answer would restart mid-sentence.
                log.exception("qa.retrying", repo_id=repo_id)
                try:
                    if published:
                        raise
                    result = await attempt()
                except Exception as exc:
                    # Log before degrading: the keyword-only answer looks like a
                    # product behaviour in the UI, so without this the real cause
                    # (provider error, model load, DB) leaves no trace anywhere.
                    log.exception("qa.llm_failed_falling_back", repo_id=repo_id)
                    result = await answer_deterministically(
                        engine=engine,
                        snapshot_repo_id=snapshot_repo_id,
                        question=question,
                        fallback_reason=_reader_facing_reason(exc),
                    )
        finally:
            await engine.dispose()
        answer_id = uuid4().hex[:8]
        return QAAnswerResponse(
            answer=result.answer,
            claims=[
                ClaimPayload(
                    id=f"qa-{answer_id}-{index}",
                    text=claim.text,
                    refs=claim.refs,
                    status=claim.status,
                    verifier_note=claim.verifier_note,
                    retrieval_path=result.retrieval_path,
                )
                for index, claim in enumerate(result.claims)
            ],
            retrieval_path=result.retrieval_path,
        )


@dataclass(slots=True)
class LiveChunkService:
    runtime: Runtime
    repos: LiveRepoService

    async def get(self, chunk_id: str) -> ChunkPayload:
        repo_id, ref = decode_chunk_id(chunk_id)
        # Chunk ids embed the user-facing repo id (e.g. "owner/repo"), but chunks
        # are keyed by the indexed snapshot id ("owner/repo@<head_sha>"). Resolve
        # the display id to its snapshot before looking the chunk up, otherwise a
        # perfectly valid claim ref resolves to nothing ("chunk not found").
        snapshot_repo_id = repo_id
        if "@" not in repo_id:
            repo = await self.repos.get(repo_id)
            if repo.indexed_repo_id is None:
                raise KeyError(chunk_id)
            snapshot_repo_id = repo.indexed_repo_id
        engine = make_engine(self.runtime.settings)
        try:
            chunks = await read_chunks([ref], engine=engine, repo_id=snapshot_repo_id)
        finally:
            await engine.dispose()
        if not chunks:
            raise KeyError(chunk_id)
        chunk = chunks[0]
        return ChunkPayload(
            chunk_id=chunk_id,
            repo_id=snapshot_repo_id,
            ref=chunk.ref,
            content=chunk.content,
            summary=chunk.summary,
        )


#: Order neighbours are presented in, and — because the limit truncates from
#: the end — the order they survive in. `defined_by` is one row that orients
#: the reader ("this method belongs to that class"), so it leads. Call edges
#: answer "what does this do and who needs it", which is what a reader looking
#: at a claim actually asks. `defines` can be long on a module, so it sits
#: below the call edges rather than crowding them out. Imports are the noisiest
#: and go last.
_EDGE_ORDER: tuple[GraphEdgeKind, ...] = (
    "defined_by",
    "calls",
    "called_by",
    "inherits",
    "inherited_by",
    "defines",
    "imports",
    "imported_by",
)

#: Hard ceiling on one neighbourhood response, whatever the caller asks for.
MAX_NEIGHBOURS = 200


@dataclass(slots=True)
class LiveGraphService:
    """Read-only view over the snapshot's persisted code graph.

    This is a read API over data the AST already produced, not a seventh agent
    tool: no model is involved, nothing enters the tool registry, and no prompt
    budget is touched. ``services.py`` already calls ``graph_query`` the same
    way for first impressions.
    """

    runtime: Runtime
    repos: LiveRepoService

    async def neighbours(
        self, repo_id: str, symbol: str, *, limit: int = 60
    ) -> GraphNeighboursResponse:
        limit = max(1, min(limit, MAX_NEIGHBOURS))
        snapshot_repo_id = repo_id
        if "@" not in repo_id:
            repo = await self.repos.get(repo_id)
            if repo.indexed_repo_id is None:
                raise KeyError(repo_id)
            snapshot_repo_id = repo.indexed_repo_id

        engine = make_engine(self.runtime.settings)
        try:
            async with engine.connect() as conn:
                adjacency_row = (
                    await conn.execute(
                        select(graph_table.c.adjacency).where(
                            graph_table.c.repo_id == snapshot_repo_id
                        )
                    )
                ).first()
                # No graph at all is the normal case for a repo with no Python.
                if adjacency_row is None or not adjacency_row[0]:
                    return GraphNeighboursResponse(symbol=symbol, available=False, found=False)

                adjacency: dict[str, dict[str, list[str]]] = adjacency_row[0]
                buckets = adjacency.get(symbol)
                if buckets is None:
                    return GraphNeighboursResponse(symbol=symbol, available=True, found=False)

                pairs: list[tuple[str, GraphEdgeKind]] = []
                seen: set[tuple[str, str]] = set()
                for edge in _EDGE_ORDER:
                    for target in sorted(set(buckets.get(edge, []))):
                        key = (target, edge)
                        if key in seen:
                            continue
                        seen.add(key)
                        pairs.append((target, edge))

                total = len(pairs)
                truncated = total > limit
                pairs = pairs[:limit]
                if not pairs:
                    return GraphNeighboursResponse(
                        symbol=symbol, available=True, found=True, total=0
                    )

                resolved = await self._resolve_symbols(
                    conn, snapshot_repo_id, [p[0] for p in pairs]
                )
                internal_roots = await self._internal_roots(conn, snapshot_repo_id)
        finally:
            await engine.dispose()

        neighbours = [
            self._to_neighbour(target, edge, resolved, internal_roots, snapshot_repo_id)
            for target, edge in pairs
        ]
        # Internal, resolvable symbols first — a reader can act on those. Edge
        # order is preserved within each group so the grouping still reads.
        neighbours.sort(key=lambda n: (n.external, not n.resolved))
        return GraphNeighboursResponse(
            symbol=symbol,
            available=True,
            found=True,
            neighbours=neighbours,
            total=total,
            truncated=truncated,
        )

    @staticmethod
    def _to_neighbour(
        target: str,
        edge: GraphEdgeKind,
        resolved: dict[str, tuple[str, int, int, str | None]],
        internal_roots: set[str],
        snapshot_repo_id: str,
    ) -> GraphNeighbour:
        hit = resolved.get(target)
        root = target.split(".", 1)[0]
        ref = None
        chunk_id = None
        if hit is not None:
            file_path, start_line, end_line, _kind = hit
            ref = CodeRef(
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                symbol=target,
            )
            chunk_id = encode_chunk_id(snapshot_repo_id, ref)
        return GraphNeighbour(
            symbol=target,
            label=target.rsplit(".", 1)[-1] or target,
            edge=edge,
            kind=None if hit is None else hit[3],
            external=root not in internal_roots,
            resolved=hit is not None,
            chunk_id=chunk_id,
            ref=ref,
        )

    @staticmethod
    async def _resolve_symbols(
        conn: Any, snapshot_repo_id: str, symbols: Sequence[str]
    ) -> dict[str, tuple[str, int, int, str | None]]:
        """Map symbol -> (file_path, start_line, end_line, kind) in one query.

        A symbol can be chunked more than once (an oversized body is split), so
        the span is the union: the earliest start and the latest end.
        """
        if not symbols:
            return {}
        rows = (
            await conn.execute(
                select(
                    chunks_table.c.symbol,
                    chunks_table.c.file_path,
                    func.min(chunks_table.c.start_line),
                    func.max(chunks_table.c.end_line),
                    func.min(chunks_table.c.kind),
                )
                .where(
                    chunks_table.c.repo_id == snapshot_repo_id,
                    chunks_table.c.symbol.in_(list(dict.fromkeys(symbols))),
                )
                .group_by(chunks_table.c.symbol, chunks_table.c.file_path)
            )
        ).fetchall()
        out: dict[str, tuple[str, int, int, str | None]] = {}
        for sym, file_path, start_line, end_line, kind in rows:
            # Deterministic when a symbol appears in more than one file: keep
            # the lexicographically first path rather than whichever row landed.
            existing = out.get(str(sym))
            if existing is not None and existing[0] <= str(file_path):
                continue
            out[str(sym)] = (
                str(file_path),
                int(start_line),
                int(end_line),
                None if kind is None else str(kind),
            )
        return out

    @staticmethod
    async def _internal_roots(conn: Any, snapshot_repo_id: str) -> set[str]:
        """Top-level package names this repo actually defines.

        Anything outside them came in as an import target — stdlib or a third
        party — and is marked external rather than silently rendered as if it
        were the user's own code.
        """
        rows = (
            await conn.execute(
                select(func.split_part(chunks_table.c.symbol, ".", 1))
                .where(chunks_table.c.repo_id == snapshot_repo_id)
                .distinct()
            )
        ).fetchall()
        return {str(r[0]) for r in rows if r[0]}


async def create_live_services() -> AppServices:
    runtime = await build_runtime()
    # One product engine shared by access and tours; `access.aclose()` disposes it.
    product_engine = make_engine(runtime.settings)
    access = ProductAccessService(engine=product_engine, settings=runtime.settings)
    tours = PostgresTourService(engine=product_engine)
    repos = LiveRepoService(runtime=runtime)
    qa = LiveQAService(runtime=runtime, repos=repos)
    chunks = LiveChunkService(runtime=runtime, repos=repos)
    graph = LiveGraphService(runtime=runtime, repos=repos)
    return AppServices(repos=repos, qa=qa, chunks=chunks, graph=graph, access=access, tours=tours)


async def close_live_services(services: AppServices) -> None:
    repo_service = services.repos
    if isinstance(repo_service, LiveRepoService):
        active_tasks: list[asyncio.Task[None]] = []
        for task in repo_service.tasks.values():
            if not task.done():
                task.cancel()
                active_tasks.append(task)
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        if repo_service.redis is not None:
            await repo_service.redis.aclose()
        await shutdown_runtime(repo_service.runtime)
    await services.access.aclose()


def encode_chunk_id(repo_id: str, ref: CodeRef) -> str:
    payload = {
        "repo_id": repo_id,
        "file_path": ref.file_path,
        "start_line": ref.start_line,
        "end_line": ref.end_line,
        "symbol": ref.symbol,
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def decode_chunk_id(chunk_id: str) -> tuple[str, CodeRef]:
    padded = chunk_id + "=" * (-len(chunk_id) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        repo_id = str(payload["repo_id"])
        ref = CodeRef(
            file_path=str(payload["file_path"]),
            start_line=int(payload["start_line"]),
            end_line=int(payload["end_line"]),
            symbol=None if payload.get("symbol") is None else str(payload["symbol"]),
        )
    except (binascii.Error, KeyError, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ValueError("invalid chunk id; use a chunk id emitted by a tour or ask claim") from exc
    return repo_id, ref


def _reader_facing_reason(exc: BaseException) -> str:
    """Turn a pipeline failure into something the reader can act on.

    ``str(exc)`` went straight into ``retrieval_path`` and the web app renders
    it verbatim ("Model call failed: …"), so a free-tier quota window showed up
    as ``HTTPStatusError("413 Payload Too Large")`` — which reads as "my
    question was too big" and sends the reader off shortening their question,
    the one thing that cannot help. Only the shape of the failure is useful
    here; the detail belongs in the log line above the call.
    """
    if isinstance(exc, ProviderError) and "RateLimitError" in str(exc):
        return "the model provider is rate-limited right now — ask again in a minute"
    return "the model call failed; the answer below is keyword matching only"


async def answer_deterministically(
    *,
    engine: AsyncEngine,
    snapshot_repo_id: str,
    question: str,
    fallback_reason: str | None = None,
) -> QAResult:
    tokens = {token.lower() for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]+", question)}
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(
                    chunks_table.c.file_path,
                    chunks_table.c.start_line,
                    chunks_table.c.end_line,
                    chunks_table.c.symbol,
                    chunks_table.c.summary,
                    chunks_table.c.content,
                ).where(chunks_table.c.repo_id == snapshot_repo_id)
            )
        ).all()
    scored: list[tuple[int, CodeRef, str | None, str]] = []
    for file_path, start_line, end_line, symbol, summary, content in rows:
        haystack = " ".join(part for part in [str(symbol), str(summary), str(content)] if part)
        words = {token.lower() for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]+", haystack)}
        overlap = len(tokens & words)
        if overlap == 0:
            continue
        ref = CodeRef(
            file_path=str(file_path),
            start_line=int(start_line),
            end_line=int(end_line),
            symbol=str(symbol),
        )
        scored.append((overlap, ref, None if summary is None else str(summary), str(content)))
    scored.sort(key=lambda item: (-item[0], item[1].file_path, item[1].start_line))
    top = scored[:3]
    if not top:
        retrieval_path = ["deterministic_text_overlap"]
        if fallback_reason:
            retrieval_path.insert(0, f"rag_fallback:{fallback_reason[:160]}")
        return QAResult(
            question=question,
            answer="I couldn't find a strong match in the indexed snapshot yet. Try asking about a concrete symbol or file.",
            claims=[],
            objections=[],
            hops=0,
            retrieval_path=retrieval_path,
        )
    claims = [
        QAClaim(
            text=(
                f"`{ref.symbol}` in `{ref.file_path}` looks relevant because it overlaps with your question "
                f"and is summarized as {summary or 'an implementation chunk worth reading directly'}."
            ),
            refs=[ref],
            # Not "verified": nothing checked this claim against the code. It
            # is a keyword hit, and labelling it verified would be the exact
            # fluent-over-truthful failure the verifier exists to prevent.
            status="unverified",
            verifier_note="Matched by deterministic text overlap against the indexed snapshot.",
        )
        for _, ref, summary, _ in top
    ]
    answer = "\n".join(
        [
            "## Keyword match only",
            "The language model could not answer this question, so these are the "
            "closest matches by keyword overlap in the indexed snapshot — not a "
            "verified explanation.",
            "",
            "## Where to look next",
            *[
                f"Read `{ref.symbol}` in `{ref.file_path}`:{ref.start_line}-{ref.end_line}"
                + (f" — {summary}" if summary else ".")
                for _, ref, summary, _ in top
            ],
            "",
            "Ask again naming a concrete symbol or file for a fuller answer.",
        ]
    )
    retrieval_path = ["deterministic_text_overlap"]
    if fallback_reason:
        retrieval_path.insert(0, f"rag_fallback:{fallback_reason[:160]}")
    return QAResult(
        question=question,
        answer=answer,
        claims=claims,
        objections=[],
        hops=0,
        retrieval_path=retrieval_path,
    )


__all__ = [
    "AppServices",
    "ChunkService",
    "LiveChunkService",
    "LiveQAService",
    "LiveRepoService",
    "QAService",
    "RepoRecord",
    "RepoService",
    "build_runtime",
    "close_live_services",
    "create_live_services",
    "decode_chunk_id",
    "encode_chunk_id",
]
