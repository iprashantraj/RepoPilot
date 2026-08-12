"""Slow / integration: full Phase 1 pipeline against the real ``httpx`` repo.

Gated by both ``slow`` (live clone, real Groq calls, fastembed
weights downloaded into the local HF cache on first run) and ``integration``
(live Postgres). Skipped in the fast CI lane.
"""

from __future__ import annotations

import time

import networkx as nx
import pytest

from repopilot_core.llm.provider import LLMProvider
from repopilot_core.settings import Settings
from repopilot_ingestion.clone import clone_to_tempdir
from repopilot_ingestion.graph import build_graph
from repopilot_ingestion.parse import parse_file
from repopilot_ingestion.pipeline import index_repo


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.asyncio
async def test_indexing_under_90s() -> None:
    """Phase 1 quality gate: httpx (~50 kLOC) indexes in ≤ 90 s."""
    settings = Settings()
    provider = LLMProvider.build(settings=settings)
    try:
        start = time.perf_counter()
        result = await index_repo(
            "https://github.com/encode/httpx",
            provider=provider,
            settings=settings,
        )
        elapsed = time.perf_counter() - start
        assert result.status == "indexed"
        assert elapsed <= 90.0, f"indexing took {elapsed:.1f}s (>90s budget)"
    finally:
        await provider.aclose()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_graph_known_call_chain_httpx() -> None:
    """A known call chain in httpx must exist as a graph path.

    This runs without Postgres — it clones + parses + builds the graph and
    asserts the path, then throws everything away.
    """
    from repopilot_ingestion.graph import ModuleSource

    with clone_to_tempdir("https://github.com/encode/httpx") as clone:
        modules = []
        for path in clone.path.rglob("*.py"):
            rel = path.relative_to(clone.path)
            mod = ".".join(rel.with_suffix("").parts)
            parsed = parse_file(path, module=mod)
            modules.append(ModuleSource(module=mod, rel_path=str(rel), source=parsed.source))
        graph = build_graph(modules)

    # The exact symbol IDs depend on httpx's package layout, so match by suffix.
    #
    # The hop asserted is to *BaseTransport*, not HTTPTransport. `send` reaches
    # `transport.handle_request(...)` where `transport` is declared
    # `BaseTransport`; which concrete subclass it holds is decided at runtime
    # by `_init_transport`. A static path to HTTPTransport.handle_request would
    # have to be invented, and the builder's contract is that it never is.
    # HTTPTransport is tied in through the inherits edge instead.
    candidates_from = [n for n in graph.nodes if n.endswith("Client.send")]
    candidates_to = [n for n in graph.nodes if n.endswith("BaseTransport.handle_request")]
    assert candidates_from and candidates_to, "expected httpx symbols missing"
    assert any(nx.has_path(graph, src, dst) for src in candidates_from for dst in candidates_to), (
        "no graph path Client.send → BaseTransport.handle_request"
    )

    # And the concrete transport is reachable as a subclass of the one called.
    concrete = [n for n in graph.nodes if n.endswith("_transports.default.HTTPTransport")]
    base = [n for n in graph.nodes if n.endswith("_transports.base.BaseTransport")]
    assert concrete and base, "expected httpx transport classes missing"
    assert any(graph.has_edge(sub, sup) for sub in concrete for sup in base), (
        "HTTPTransport is not connected to BaseTransport"
    )
