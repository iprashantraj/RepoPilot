"""Logical model identifiers and their physical-model resolution per provider.

Agents NEVER reference a physical model name. They ask for a `ModelId`; the
provider resolves it to the concrete model on Groq / Cerebras / Hugging Face.
This indirection is what makes the failover chain transparent — see
`docs/02_TECH_STACK.md` and `docs/03_ARCHITECTURE.md` (Agent table).

Provider fallback chain in v1:
    Groq → Cerebras → Hugging Face (Inference Providers)
Embeddings: fastembed in-process (ONNX; no HTTP, no daemon, no torch).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProviderName(StrEnum):
    GROQ = "groq"
    CEREBRAS = "cerebras"
    HUGGINGFACE = "huggingface"


class ModelId(StrEnum):
    """Logical, agent-facing model identifiers."""

    INTENT_PROFILER = "intent_profiler"
    CAPABILITY_PLANNER = "capability_planner"
    CARTOGRAPHER = "cartographer"
    FLOW_TRACER = "flow_tracer"
    TEACHER = "teacher"
    QA_PRIMARY = "qa_primary"
    QA_FALLBACK = "qa_fallback"
    CODE_HEALTH = "code_health"
    VERIFIER = "verifier"
    EMBEDDINGS = "embeddings"


@dataclass(frozen=True, slots=True)
class ModelBinding:
    """The concrete model name to send to a given provider for one `ModelId`."""

    provider: ProviderName
    physical_model: str


# Per-model resolution chain. The first entry is the preferred provider; the
# remaining entries are tried in order on RateLimitError / connection failure.
#
# **HF is NOT in any chat chain in v1.** The HF Inference Providers gateway
# has a tiny free-tier credit budget (~$0.10/mo) that a single bench run can
# exhaust. Chat chains stop at Cerebras — if BOTH Groq and Cerebras are 429,
# raise `ProviderError` cleanly instead of silently draining HF credits.
# The user can re-add HF explicitly when they have credits.
#
# EMBEDDINGS keeps the HUGGINGFACE ProviderName because that path is served
# by the in-process fastembed embedder (HF model weights, ONNX, no HTTP,
# no credits burned). See LLMProvider.embed().
# Model IDs verified against the LIVE Groq + Cerebras catalogs (2026-07-21):
#   Groq     = {llama-3.3-70b-versatile, llama-3.1-8b-instant,
#               openai/gpt-oss-120b, openai/gpt-oss-20b, qwen/qwen3.6-27b}
#   Cerebras = {zai-glm-4.7, gemma-4-31b, gpt-oss-120b}
# Earlier chains referenced IDs absent from both catalogs (qwen-2.5-32b,
# llama-3.3-70b, llama-4-scout-17b-16e-instruct), so EVERY fallback hop 404'd —
# most visibly the Verifier, which flagged all claims with a provider error.
RESOLUTION: dict[ModelId, tuple[ModelBinding, ...]] = {
    ModelId.INTENT_PROFILER: (
        ModelBinding(ProviderName.GROQ, "llama-3.3-70b-versatile"),
        ModelBinding(ProviderName.CEREBRAS, "zai-glm-4.7"),
    ),
    ModelId.CAPABILITY_PLANNER: (
        ModelBinding(ProviderName.GROQ, "llama-3.3-70b-versatile"),
        ModelBinding(ProviderName.CEREBRAS, "zai-glm-4.7"),
    ),
    ModelId.CARTOGRAPHER: (
        ModelBinding(ProviderName.GROQ, "llama-3.3-70b-versatile"),
        ModelBinding(ProviderName.CEREBRAS, "zai-glm-4.7"),
        ModelBinding(ProviderName.CEREBRAS, "gemma-4-31b"),
    ),
    ModelId.FLOW_TRACER: (
        ModelBinding(ProviderName.GROQ, "qwen/qwen3.6-27b"),
        ModelBinding(ProviderName.CEREBRAS, "zai-glm-4.7"),
        ModelBinding(ProviderName.CEREBRAS, "gpt-oss-120b"),
    ),
    ModelId.TEACHER: (
        ModelBinding(ProviderName.GROQ, "llama-3.3-70b-versatile"),
        ModelBinding(ProviderName.CEREBRAS, "zai-glm-4.7"),
        ModelBinding(ProviderName.CEREBRAS, "gemma-4-31b"),
    ),
    ModelId.QA_PRIMARY: (
        ModelBinding(ProviderName.GROQ, "llama-3.3-70b-versatile"),
        ModelBinding(ProviderName.CEREBRAS, "zai-glm-4.7"),
        ModelBinding(ProviderName.CEREBRAS, "gemma-4-31b"),
        ModelBinding(ProviderName.GROQ, "llama-3.1-8b-instant"),
    ),
    ModelId.QA_FALLBACK: (
        ModelBinding(ProviderName.GROQ, "qwen/qwen3.6-27b"),
        ModelBinding(ProviderName.CEREBRAS, "zai-glm-4.7"),
        ModelBinding(ProviderName.CEREBRAS, "gpt-oss-120b"),
    ),
    ModelId.CODE_HEALTH: (
        ModelBinding(ProviderName.GROQ, "llama-3.1-8b-instant"),
        ModelBinding(ProviderName.CEREBRAS, "gemma-4-31b"),
        ModelBinding(ProviderName.GROQ, "openai/gpt-oss-20b"),
    ),
    # Verifier is the highest call-volume agent and needs strict-JSON discipline.
    # Groq qwen3.6-27b is the primary; Cerebras gpt-oss-120b (strong at JSON) is
    # the failover, with zai-glm-4.7 as the last-resort shim.
    ModelId.VERIFIER: (
        ModelBinding(ProviderName.GROQ, "qwen/qwen3.6-27b"),
        ModelBinding(ProviderName.CEREBRAS, "gpt-oss-120b"),
        ModelBinding(ProviderName.CEREBRAS, "zai-glm-4.7"),
    ),
    # Embeddings run in-process via fastembed (HF model weights, ONNX).
    # physical_model is the HF model id passed to fastembed's TextEmbedding().
    # nomic-embed-text-v1.5 is 768-dim, matches the existing pgvector schema.
    ModelId.EMBEDDINGS: (ModelBinding(ProviderName.HUGGINGFACE, "nomic-ai/nomic-embed-text-v1.5"),),
}


# Opt-in HF Inference Providers chat fallback, appended to a model's chain only
# when settings.llm_hf_chat_fallback is set (see LLMProvider._resolution_for).
# Kept OUT of RESOLUTION so default behavior is unchanged and HF credits are
# never touched unless the operator explicitly enables it. Physical ids are the
# HF router names mirroring each model's Groq primary (verified on the router):
#   llama-3.3-70b → Llama-3.3-70B-Instruct, qwen/qwen3-32b → Qwen3-32B,
#   llama-3.1-8b-instant → Llama-3.1-8B-Instruct.
HF_CHAT_FALLBACK: dict[ModelId, ModelBinding] = {
    ModelId.INTENT_PROFILER: ModelBinding(
        ProviderName.HUGGINGFACE, "meta-llama/Llama-3.3-70B-Instruct"
    ),
    ModelId.CAPABILITY_PLANNER: ModelBinding(
        ProviderName.HUGGINGFACE, "meta-llama/Llama-3.3-70B-Instruct"
    ),
    ModelId.CARTOGRAPHER: ModelBinding(
        ProviderName.HUGGINGFACE, "meta-llama/Llama-3.3-70B-Instruct"
    ),
    ModelId.FLOW_TRACER: ModelBinding(ProviderName.HUGGINGFACE, "Qwen/Qwen3-32B"),
    ModelId.TEACHER: ModelBinding(ProviderName.HUGGINGFACE, "meta-llama/Llama-3.3-70B-Instruct"),
    ModelId.QA_PRIMARY: ModelBinding(ProviderName.HUGGINGFACE, "meta-llama/Llama-3.3-70B-Instruct"),
    ModelId.QA_FALLBACK: ModelBinding(ProviderName.HUGGINGFACE, "Qwen/Qwen3-32B"),
    ModelId.CODE_HEALTH: ModelBinding(ProviderName.HUGGINGFACE, "meta-llama/Llama-3.1-8B-Instruct"),
    ModelId.VERIFIER: ModelBinding(ProviderName.HUGGINGFACE, "Qwen/Qwen3-32B"),
}
