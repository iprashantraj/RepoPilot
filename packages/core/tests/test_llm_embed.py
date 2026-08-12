"""Tests for ``LLMProvider.embed()`` — cache hit, fresh embed, dim contract."""

from __future__ import annotations

from typing import Any

import pytest

from repopilot_core.llm.models import ModelBinding, ModelId, ProviderName
from repopilot_core.llm.provider import EmbeddingResponse, _BaseClient

from .conftest import make_provider


class FakeEmbedder(_BaseClient):
    """Test double — bypasses the fastembed model load and
    returns canned embeddings."""

    provider = ProviderName.HUGGINGFACE

    def __init__(self, vectors: list[list[float]]) -> None:
        self._vectors = list(vectors)
        self.calls: list[tuple[str, str]] = []

    async def chat(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("chat not expected during embed test")

    async def embed(self, binding: ModelBinding, text: str) -> EmbeddingResponse:
        self.calls.append((binding.physical_model, text))
        head = self._vectors.pop(0)
        return EmbeddingResponse(
            vector=head,
            model=ModelId.EMBEDDINGS,
            provider=self.provider,
            physical_model=binding.physical_model,
        )


class BatchEmbedder(_BaseClient):
    provider = ProviderName.HUGGINGFACE

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], int]] = []

    async def chat(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("chat not expected during embed test")

    async def embed_many(
        self,
        binding: ModelBinding,
        texts: list[str],
        *,
        batch_size: int,
    ) -> list[EmbeddingResponse]:
        self.calls.append((list(texts), batch_size))
        return [
            EmbeddingResponse(
                vector=[float(len(text)), float(index)],
                model=ModelId.EMBEDDINGS,
                provider=self.provider,
                physical_model=binding.physical_model,
            )
            for index, text in enumerate(texts)
        ]


@pytest.mark.asyncio
async def test_embed_returns_vector(tmp_settings: Any) -> None:
    fake = FakeEmbedder(vectors=[[0.1, 0.2, 0.3]])
    provider = make_provider(tmp_settings, clients={}, embedder=fake)

    response = await provider.embed("hello world")

    assert response.vector == [0.1, 0.2, 0.3]
    assert response.dim == 3
    assert response.model == ModelId.EMBEDDINGS
    assert response.provider == ProviderName.HUGGINGFACE
    assert response.cached is False
    assert fake.calls == [("nomic-ai/nomic-embed-text-v1.5", "hello world")]
    await provider.aclose()


@pytest.mark.asyncio
async def test_embed_cache_hit_skips_provider(tmp_settings: Any) -> None:
    fake = FakeEmbedder(vectors=[[0.4, 0.5]])
    provider = make_provider(tmp_settings, clients={}, embedder=fake)

    first = await provider.embed("same text")
    second = await provider.embed("same text")

    assert first.vector == [0.4, 0.5]
    assert second.vector == [0.4, 0.5]
    assert second.cached is True
    # FakeEmbedder would raise on a second call because the queue is empty.
    assert len(fake.calls) == 1
    await provider.aclose()


@pytest.mark.asyncio
async def test_embed_many_batches_unique_misses_and_preserves_order(tmp_settings: Any) -> None:
    fake = BatchEmbedder()
    provider = make_provider(tmp_settings, clients={}, embedder=fake)

    first = await provider.embed_many(["alpha", "beta", "alpha"], batch_size=8)
    second = await provider.embed_many(["beta", "gamma"], batch_size=4)

    assert [response.vector[0] for response in first] == [5.0, 4.0, 5.0]
    assert [response.vector[0] for response in second] == [4.0, 5.0]
    assert second[0].cached is True
    assert fake.calls == [(["alpha", "beta"], 8), (["gamma"], 4)]
    await provider.aclose()


@pytest.mark.asyncio
async def test_embed_many_empty_does_not_touch_backend(tmp_settings: Any) -> None:
    fake = BatchEmbedder()
    provider = make_provider(tmp_settings, clients={}, embedder=fake)

    assert await provider.embed_many([], batch_size=8) == []
    assert fake.calls == []
    with pytest.raises(ValueError, match="batch_size"):
        await provider.embed_many(["text"], batch_size=0)
    await provider.aclose()


@pytest.mark.asyncio
async def test_fastembed_embedder_normalises_raw_vectors() -> None:
    """fastembed returns raw pooled vectors; the embedder must unit-length them.

    sentence-transformers was called with ``normalize_embeddings=True``, so
    every vector already in the ``chunk_embeddings`` column is unit-length.
    fastembed does not normalise, and a silent switch to raw vectors would put
    two conventions in one column. Guards the ONNX model behind a stub so the
    test never downloads 130 MB of weights.
    """
    import numpy

    from repopilot_core.llm.provider import _FastEmbedEmbedder

    class StubModel:
        def __init__(self) -> None:
            self.batch_sizes: list[int] = []

        def embed(self, texts: list[str], batch_size: int) -> Any:
            self.batch_sizes.append(batch_size)
            # Raw, decidedly non-unit vectors — norms 5.0 and 26.0.
            del texts
            yield numpy.array([3.0, 4.0])
            yield numpy.array([10.0, 24.0])

    embedder = _FastEmbedEmbedder("stub-model")
    stub = StubModel()
    embedder._model = stub  # skip the ONNX load

    binding = ModelBinding(
        provider=ProviderName.HUGGINGFACE,
        physical_model="stub-model",
    )
    responses = await embedder.embed_many(binding, ["a", "b"], batch_size=4)

    assert [round(sum(x * x for x in r.vector) ** 0.5, 6) for r in responses] == [1.0, 1.0]
    assert responses[0].vector == pytest.approx([0.6, 0.8])
    assert stub.batch_sizes == [4]
