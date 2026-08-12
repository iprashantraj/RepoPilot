"""``vector_search`` — pgvector cosine k-NN over indexed chunks.

Embeds the query through ``LLMProvider.embed()`` (in-process
fastembed with `nomic-ai/nomic-embed-text-v1.5-Q` in v1)
so the embedding cache applies automatically. Returns
``ChunkHit[]`` with the chunk's ``CodeRef`` and the cosine distance.

The query operator is pgvector's ``<=>`` (cosine distance). ``WHERE
repo_id = ...`` is non-negotiable — we never mix corpora across repos.

This is one half of the hybrid-retrieval spine. The other half is
``graph_traverse``: vector_search finds the candidate hits; graph_traverse
completes the context (callers/callees/inheritance, ≤ 3 hops).

RAG Phase 1 additions: ``recall_k`` widens the candidate pool independently
of the caller's ``k`` slice, and the metadata filters (``kind``,
``path_prefix``, ``path_glob``, ``exclude_path_prefixes``) expose what the
chunks schema already stores. ``NON_SOURCE_PATH_PREFIXES`` mirrors the noise
filter in ``evals/tools/propose_labels.py`` — gold labels are source-only by
construction, so the Q&A recall lane excludes those prefixes.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from repopilot_agents.types import ChunkHit, CodeRef
from repopilot_core.llm.provider import EMBED_QUERY_PREFIX, LLMProvider

log = structlog.get_logger(__name__)

# Must stay in sync with the noise filter in evals/tools/propose_labels.py:
# gold labels are source-only by construction, so the recall lane skips these.
NON_SOURCE_PATH_PREFIXES: tuple[str, ...] = (
    "tests/",
    "examples/",
    "docs/",
    "docs_src/",
    "scripts/",
)


def build_search_sql(
    *,
    kind: str | None,
    path_prefix: str | None,
    path_glob: str | None,
    exclude_path_prefixes: Sequence[str],
) -> str:
    """Compose the k-NN query with optional metadata filters.

    Pure string composition (no user input reaches the SQL text — every
    dynamic value is a bind parameter) so the fast lane can test filter
    composition without Postgres.
    """
    clauses = ["c.repo_id = :repo_id"]
    if kind is not None:
        clauses.append("c.kind = :kind")
    if path_prefix is not None:
        clauses.append("c.file_path LIKE :path_prefix || '%'")
    if path_glob is not None:
        clauses.append("c.file_path SIMILAR TO :path_glob")
    for i in range(len(exclude_path_prefixes)):
        clauses.append(f"c.file_path NOT LIKE :exclude_{i} || '%'")
    where = "\n          AND ".join(clauses)
    return f"""
        WITH candidates AS MATERIALIZED (
            SELECT c.file_path, c.start_line, c.end_line, c.symbol, c.kind, c.summary,
                   ce.embedding
            FROM chunks c
            JOIN chunk_embeddings ce ON ce.chunk_id = c.id
            WHERE {where}
        )
        SELECT file_path, start_line, end_line, symbol, kind, summary,
               (embedding <=> CAST(:vec AS vector)) AS distance
        FROM candidates
        ORDER BY embedding <=> CAST(:vec AS vector)
        LIMIT :limit
        """


async def vector_search(
    query: str,
    *,
    engine: AsyncEngine,
    provider: LLMProvider,
    repo_id: str,
    k: int = 8,
    recall_k: int | None = None,
    kind: str | None = None,
    path_prefix: str | None = None,
    path_glob: str | None = None,
    exclude_path_prefixes: Sequence[str] = (),
) -> list[ChunkHit]:
    """Return the top chunks for ``query`` in ``repo_id``.

    ``recall_k`` (when given) sets the pool size fetched from Postgres;
    ``k`` stays the back-compat default for callers that want a small
    ranked list. The caller slices the pool — this tool never truncates
    below what it was asked for.
    """
    if not query.strip():
        return []
    limit = recall_k if recall_k is not None else k
    if limit <= 0:
        return []

    embedding = await provider.embed(EMBED_QUERY_PREFIX + query)
    literal = "[" + ",".join(repr(float(x)) for x in embedding.vector) + "]"

    # Filter and materialize the target corpus before cosine ranking. A global
    # IVFFlat scan can choose neighbours from other repositories and only then
    # apply ``repo_id``/path filters, producing a false empty result. Exact
    # ranking over the bounded per-repository candidate set prioritizes recall.
    sql = text(
        build_search_sql(
            kind=kind,
            path_prefix=path_prefix,
            path_glob=path_glob,
            exclude_path_prefixes=exclude_path_prefixes,
        )
    )
    params: dict[str, object] = {"vec": literal, "repo_id": repo_id, "limit": limit}
    if kind is not None:
        params["kind"] = kind
    if path_prefix is not None:
        params["path_prefix"] = path_prefix
    if path_glob is not None:
        params["path_glob"] = path_glob
    for i, prefix in enumerate(exclude_path_prefixes):
        params[f"exclude_{i}"] = prefix

    async with engine.connect() as conn:
        rows = (await conn.execute(sql, params)).all()

    hits: list[ChunkHit] = []
    for file_path, start_line, end_line, symbol, kind_, summary, distance in rows:
        hits.append(
            ChunkHit(
                ref=CodeRef(
                    file_path=file_path,
                    start_line=int(start_line),
                    end_line=int(end_line),
                    symbol=symbol,
                ),
                distance=float(distance),
                summary=summary,
                kind=kind_,
            )
        )
    log.debug("vector_search.done", repo_id=repo_id, limit=limit, hits=len(hits))
    return hits


__all__ = ["NON_SOURCE_PATH_PREFIXES", "build_search_sql", "vector_search"]
