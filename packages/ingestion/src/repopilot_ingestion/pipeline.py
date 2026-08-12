"""End-to-end Phase 1 pipeline orchestrator.

Wires: clone → parse → chunk → graph → (summarise || embed) → persist.

Two entry points:

* :func:`index_repo` does the full pipeline against live services.
* :func:`revisit_status` is the cheap ``git ls-remote`` staleness check used
  by the API when the user resubmits a known URL.

Both return a ``PipelineResult`` whose ``status`` matches the API contract
expected by Phase 4's ``POST /repos``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog

from repopilot_core.llm.provider import LLMProvider
from repopilot_core.settings import Settings
from repopilot_ingestion.chunk import Chunk, chunk_file, enrich_chunks_with_neighbors
from repopilot_ingestion.clone import (
    CloneResult,
    clone_to_tempdir,
    remote_head_sha,
    remote_repo_size_kb,
)
from repopilot_ingestion.embed import EmbeddedChunk, embed_chunks
from repopilot_ingestion.generic_chunk import (
    SKIP_DIRECTORIES,
    chunk_text_file,
    iter_generic_files,
)
from repopilot_ingestion.graph import ModuleSource, build_graph, graph_to_adjacency
from repopilot_ingestion.parse import parse_file
from repopilot_ingestion.persist import (
    delete_incomplete_index,
    known_head_sha,
    make_engine,
    persist_index,
    repo_already_indexed,
)
from repopilot_ingestion.summary import summarise_chunks

log = structlog.get_logger(__name__)


PipelineStatus = Literal[
    "indexed",
    "already_indexed",
    "stale",
    "too_large",
    "unsupported",
]


@dataclass(frozen=True, slots=True)
class PipelineResult:
    status: PipelineStatus
    repo_id: str | None = None
    head_sha: str | None = None
    indexed_sha: str | None = None
    remote_sha: str | None = None
    loc_total: int | None = None
    chunk_count: int | None = None
    edge_count: int | None = None
    # Set only when the reason is not derivable from the fields above (the
    # pre-clone size guard, which rejects before any line count exists).
    message: str | None = None


@dataclass(frozen=True, slots=True)
class _ScanJob:
    order: int
    path: Path
    rel_path: Path
    size: int
    language: str | None


@dataclass(frozen=True, slots=True)
class _ScannedFile:
    order: int
    rel_path: str
    module_source: ModuleSource | None
    chunks: tuple[Chunk, ...]
    line_count: int


async def revisit_status(*, repo_url: str, settings: Settings | None = None) -> PipelineResult:
    """Decide whether ``repo_url`` is already-current, stale, or unknown.

    Cheap — runs ``git ls-remote`` (no clone) and a single SELECT against
    ``repos``. Phase 4's UI calls this on URL paste to decide whether to
    show the "re-index?" banner.
    """
    settings = settings or Settings()
    engine = make_engine(settings)
    try:
        remote_sha = remote_head_sha(repo_url)
        indexed_sha = await known_head_sha(engine, repo_url=repo_url)
        if indexed_sha is None:
            return PipelineResult(status="stale", remote_sha=remote_sha)
        if indexed_sha == remote_sha:
            return PipelineResult(
                status="already_indexed",
                head_sha=indexed_sha,
                indexed_sha=indexed_sha,
                remote_sha=remote_sha,
            )
        return PipelineResult(status="stale", indexed_sha=indexed_sha, remote_sha=remote_sha)
    finally:
        await engine.dispose()


async def index_repo(
    repo_url: str,
    *,
    provider: LLMProvider,
    settings: Settings | None = None,
) -> PipelineResult:
    """Full ingestion pipeline. Idempotent on ``(repo_url, head_sha)``."""
    settings = settings or Settings()
    engine = make_engine(settings)
    total_started = time.perf_counter()
    try:
        # ``ingestion_max_repo_loc`` bounds what gets *indexed* and is checked
        # after the scan, which is too late: on a small host the clone itself is
        # what dies (OOM / disk) on a large repository. Ask GitHub for the size
        # first and reject with something the reader can act on.
        max_kb = settings.ingestion_max_repo_mb * 1024
        size_kb = await asyncio.to_thread(
            remote_repo_size_kb, repo_url, github_pat=settings.github_pat
        )
        if size_kb is not None and size_kb > max_kb:
            log.warning("pipeline.too_large_remote", repo_url=repo_url, size_kb=size_kb, cap=max_kb)
            return PipelineResult(
                status="too_large",
                message=(
                    f"Repository is {size_kb // 1024} MB, over the "
                    f"{settings.ingestion_max_repo_mb} MB indexing limit."
                ),
            )

        clone_started = time.perf_counter()
        with clone_to_tempdir(repo_url, root=settings.ingestion_clone_root) as clone:
            _log_stage("clone", clone_started, repo_url=repo_url)
            already = await repo_already_indexed(engine, repo_url=repo_url, head_sha=clone.head_sha)
            if already:
                log.info(
                    "pipeline.already_indexed",
                    repo_url=repo_url,
                    head_sha=clone.head_sha,
                )
                return PipelineResult(
                    status="already_indexed",
                    repo_id=clone.repo_id,
                    head_sha=clone.head_sha,
                )

            await delete_incomplete_index(engine, repo_url=repo_url, head_sha=clone.head_sha)
            scan_started = time.perf_counter()
            modules, chunks, loc_total = await asyncio.to_thread(
                _scan_repository_files,
                clone,
                settings=settings,
            )
            _log_stage(
                "scan",
                scan_started,
                repo_url=repo_url,
                files=len({chunk.file_path for chunk in chunks}),
                chunks=len(chunks),
                loc_total=loc_total,
                workers=settings.ingestion_scan_workers,
            )

            if not chunks:
                log.warning("pipeline.unsupported", repo_url=repo_url, head_sha=clone.head_sha)
                return PipelineResult(
                    status="unsupported",
                    repo_id=clone.repo_id,
                    head_sha=clone.head_sha,
                    loc_total=loc_total,
                    chunk_count=0,
                )

            if loc_total > settings.ingestion_max_repo_loc:
                log.warning(
                    "pipeline.too_large",
                    repo_url=repo_url,
                    loc_total=loc_total,
                    cap=settings.ingestion_max_repo_loc,
                )
                return PipelineResult(
                    status="too_large",
                    repo_id=clone.repo_id,
                    head_sha=clone.head_sha,
                    loc_total=loc_total,
                )

            graph_started = time.perf_counter()
            graph = build_graph(modules)
            adjacency = graph_to_adjacency(graph)
            chunks = enrich_chunks_with_neighbors(chunks, adjacency)
            _log_stage(
                "graph_and_enrichment",
                graph_started,
                repo_url=repo_url,
                modules=len(modules),
                nodes=len(adjacency),
            )

            model_started = time.perf_counter()
            # TaskGroup rather than gather: a bare gather propagates the first
            # failure but leaves the sibling running, so a failed summarise
            # left an embedding pass burning provider quota for a pipeline
            # that had already given up.
            async with asyncio.TaskGroup() as model_stage:
                summary_task = model_stage.create_task(
                    summarise_chunks(chunks, provider=provider, settings=settings)
                )
                embed_task = model_stage.create_task(
                    embed_chunks(chunks, provider=provider, settings=settings)
                )
            summarised, embedded = summary_task.result(), embed_task.result()
            _log_stage(
                "summary_and_embedding",
                model_started,
                repo_url=repo_url,
                chunks=len(chunks),
                embed_batch_size=settings.ingestion_embed_batch_size,
            )

            embed_index: dict[tuple[str, int, int], EmbeddedChunk] = {
                (e.chunk.file_path, e.chunk.start_line, e.chunk.end_line): e for e in embedded
            }

            persist_started = time.perf_counter()
            persist_result = await persist_index(
                engine=engine,
                repo_id=clone.repo_id,
                repo_url=repo_url,
                head_sha=clone.head_sha,
                summarised=summarised,
                embedded=embed_index,
                adjacency=adjacency,
                loc_total=loc_total,
            )
            _log_stage(
                "persist",
                persist_started,
                repo_url=repo_url,
                chunks=persist_result.chunk_count,
            )
            _log_stage("total", total_started, repo_url=repo_url)
            return PipelineResult(
                status="indexed",
                repo_id=persist_result.repo_id,
                head_sha=clone.head_sha,
                loc_total=loc_total,
                chunk_count=persist_result.chunk_count,
                edge_count=persist_result.edge_count,
            )
    finally:
        await engine.dispose()


# ── internals ───────────────────────────────────────────────────────────────


def _scan_python_files(
    clone: CloneResult,
) -> tuple[list[ModuleSource], list[Chunk], int]:
    modules: list[ModuleSource] = []
    chunks: list[Chunk] = []
    loc_total = 0

    for py_path in _iter_python_files(clone.path):
        rel = py_path.relative_to(clone.path)
        module = _path_to_module(rel, root=clone.path)
        parsed = parse_file(py_path, module=module)
        loc_total += parsed.line_count
        modules.append(ModuleSource(module=module, rel_path=str(rel), source=parsed.source))
        chunks.extend(chunk_file(parsed, rel_path=rel))
    return modules, chunks, loc_total


def _scan_repository_files(
    clone: CloneResult,
    *,
    settings: Settings,
) -> tuple[list[ModuleSource], list[Chunk], int]:
    """Scan supported files concurrently and reassemble canonical output order."""
    jobs = _discover_scan_jobs(clone.path)
    results = _run_scan_jobs(
        jobs,
        root=clone.path,
        settings=settings,
        workers=settings.ingestion_scan_workers,
    )
    modules: list[ModuleSource] = []
    chunks: list[Chunk] = []
    loc_total = 0
    for result in results:
        if result.module_source is not None:
            modules.append(result.module_source)
        chunks.extend(result.chunks)
        loc_total += result.line_count
    return modules, chunks, loc_total


def _discover_scan_jobs(root: Path) -> list[_ScanJob]:
    discovered: list[tuple[Path, str | None]] = [
        *((path, None) for path in _iter_python_files(root)),
        *iter_generic_files(root),
    ]
    canonical = sorted(discovered, key=lambda item: item[0].relative_to(root).as_posix())
    jobs: list[_ScanJob] = []
    for order, (path, language) in enumerate(canonical):
        rel = path.relative_to(root)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise RuntimeError(f"could not stat indexable file {rel.as_posix()}: {exc}") from exc
        jobs.append(
            _ScanJob(
                order=order,
                path=path,
                rel_path=rel,
                size=size,
                language=language,
            )
        )
    return jobs


def _run_scan_jobs(
    jobs: list[_ScanJob],
    *,
    root: Path,
    settings: Settings,
    workers: int,
) -> list[_ScannedFile]:
    scheduled = sorted(jobs, key=lambda job: (-job.size, job.rel_path.as_posix()))
    if workers == 1 or len(scheduled) <= 1:
        serial_results: list[_ScannedFile] = []
        for job in scheduled:
            try:
                serial_results.append(_scan_one_file(job, root=root, settings=settings))
            except Exception as exc:
                raise RuntimeError(f"failed to scan {job.rel_path.as_posix()}: {exc}") from exc
        return sorted(serial_results, key=lambda result: result.order)

    results: dict[int, _ScannedFile] = {}
    pending: dict[Future[_ScannedFile], _ScanJob] = {}
    iterator = iter(scheduled)
    max_pending = max(workers, workers * 2)
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="repopilot-scan")
    try:
        while len(pending) < max_pending:
            candidate = next(iterator, None)
            if candidate is None:
                break
            pending[executor.submit(_scan_one_file, candidate, root=root, settings=settings)] = (
                candidate
            )

        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                job = pending.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    for outstanding in pending:
                        outstanding.cancel()
                    raise RuntimeError(f"failed to scan {job.rel_path.as_posix()}: {exc}") from exc
                results[result.order] = result

            while len(pending) < max_pending:
                candidate = next(iterator, None)
                if candidate is None:
                    break
                pending[
                    executor.submit(_scan_one_file, candidate, root=root, settings=settings)
                ] = candidate
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    return [results[index] for index in range(len(jobs))]


def _scan_one_file(job: _ScanJob, *, root: Path, settings: Settings) -> _ScannedFile:
    rel = job.rel_path
    if job.language is None:
        module = _path_to_module(rel, root=root)
        parsed = parse_file(job.path, module=module)
        return _ScannedFile(
            order=job.order,
            rel_path=rel.as_posix(),
            module_source=ModuleSource(
                module=module, rel_path=rel.as_posix(), source=parsed.source
            ),
            chunks=tuple(chunk_file(parsed, rel_path=rel)),
            line_count=parsed.line_count,
        )

    file_chunks, line_count = chunk_text_file(
        job.path,
        root=root,
        language=job.language,
        max_file_bytes=settings.ingestion_max_file_bytes,
        max_chunk_lines=settings.ingestion_text_chunk_lines,
        max_chunk_chars=settings.ingestion_text_chunk_chars,
        overlap_lines=min(
            settings.ingestion_text_chunk_overlap_lines,
            settings.ingestion_text_chunk_lines - 1,
        ),
    )
    return _ScannedFile(
        order=job.order,
        rel_path=rel.as_posix(),
        module_source=None,
        chunks=tuple(file_chunks),
        line_count=line_count,
    )


def _log_stage(stage: str, started: float, **fields: object) -> None:
    log.info(
        "pipeline.stage_done",
        stage=stage,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        **fields,
    )


def _iter_python_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if path.is_symlink() or any(part in SKIP_DIRECTORIES for part in rel.parts[:-1]):
            continue
        yield path


def _path_to_module(rel_path: Path, *, root: Path | None = None) -> str:
    """Dotted module name for a repo-relative path, as the code imports itself.

    Naming from the repo-relative path alone splits a src-layout repo in two:
    ``src/flask/app.py`` becomes ``src.flask.app`` while every import of it
    says ``flask.app``, so the defining node and the imported node are
    different strings and no internal edge ever connects. Walking up while
    each directory is a package finds the real import root instead.

    ``root`` is optional only so callers without a clone on disk keep working;
    without it the old repo-relative name is returned.
    """
    parts = list(rel_path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if root is None or not parts:
        return ".".join(parts)

    # Climb while the directory is a package. A directory with no __init__.py
    # whose parent has one is a namespace subpackage — importable, and common:
    # flask's src/flask/sansio/ is exactly this, and stopping there names its
    # modules "app" instead of "flask.sansio.app", which breaks the join from a
    # graph node to its chunk.
    depth = 0
    directory = (root / rel_path).parent
    while directory != root and (
        (directory / "__init__.py").exists() or (directory.parent / "__init__.py").exists()
    ):
        depth += 1
        directory = directory.parent

    keep = depth if rel_path.name == "__init__.py" else depth + 1
    parts = parts[-keep:] if keep > 0 else parts[-1:]
    return ".".join(parts)


__all__ = ["PipelineResult", "PipelineStatus", "index_repo", "revisit_status"]
