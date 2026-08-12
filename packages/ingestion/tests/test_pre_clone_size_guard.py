"""The pre-clone size guard rejects oversized repos before any clone happens."""

from __future__ import annotations

import pytest

from repopilot_core.settings import Settings
from repopilot_ingestion import pipeline


@pytest.mark.asyncio
async def test_oversized_repo_is_rejected_without_cloning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "remote_repo_size_kb", lambda *a, **k: 150_000)

    def _no_clone(*args: object, **kwargs: object) -> None:
        raise AssertionError("clone must not run for an oversized repository")

    monkeypatch.setattr(pipeline, "clone_to_tempdir", _no_clone)
    settings = Settings(ingestion_max_repo_mb=100)

    result = await pipeline.index_repo(
        "https://github.com/justutsav/Avinya_hackathon",
        provider=None,  # type: ignore[arg-type]  # never reached
        settings=settings,
    )

    assert result.status == "too_large"
    assert result.message is not None and "100 MB" in result.message
