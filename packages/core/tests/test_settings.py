"""Test 5 from the Phase 0 TDD checklist."""

from __future__ import annotations

from pathlib import Path

import pytest

from repopilot_core.settings import Settings


def test_settings_loads_from_env_example() -> None:
    """`.env.example` shipped at the repo root must be a valid pydantic-settings source.

    This guards against drift between the example file and the `Settings` model —
    if a new field is added without updating `.env.example`, this test still passes
    (extras are ignored), but if a *typed* field's example value violates its
    schema, pydantic raises here.
    """
    env_example = Path(__file__).resolve().parents[3] / ".env.example"
    assert env_example.exists(), f"missing {env_example}"

    settings = Settings(_env_file=env_example)

    # Known defaults from the example file:
    assert settings.repopilot_env == "development"
    assert settings.huggingface_base_url.startswith("https://router.huggingface.co/")
    assert settings.llm_max_429_retries == 5

    # The example must ship the *same* embedder as the code default. Drift here
    # is silent and expensive: the example once pinned the full-precision build
    # while the default was the quantized one, so anyone who copied the template
    # downloaded 520MB of weights instead of 130MB and never knew.
    assert (
        settings.huggingface_embedding_model
        == Settings.model_fields["huggingface_embedding_model"].default
    )


def test_production_requires_a_non_default_session_secret() -> None:
    with pytest.raises(ValueError, match="REPOPILOT_SESSION_SECRET"):
        Settings(repopilot_env="production")

    settings = Settings(
        repopilot_env="production",
        repopilot_session_secret="a-production-secret-generated-outside-source-control",
        repopilot_session_cookie_secure=True,
    )
    assert settings.repopilot_session_cookie_secure is True
