---
trigger: always_on
description: RepoPilot project rules and engineering conventions. Tool-neutral mirror of CLAUDE.md for non-Claude AI assistants.
---

# RepoPilot — Project Rules (portable)

> This is the tool-neutral mirror of [`CLAUDE.md`](../../CLAUDE.md). `CLAUDE.md` is the single source of truth; keep this in sync with it. Editing one without the other is a bug.

## What this project is
RepoPilot ("Codebase Archaeologist") generates purpose-driven, multi-agent guided tours of unfamiliar Python codebases. The core bet: capture user pre-context (purpose + focus) **before** any analysis, then inject it into every downstream agent. Full design lives in `docs/`.

**Picking work up cold?** Read [`docs/STATUS.md`](../../docs/STATUS.md) first — what shipped, what is next, and what is known-broken. Local setup is in [`docs/STARTUP_GUIDE.md`](../../docs/STARTUP_GUIDE.md); the contributor workflow (dev loop, commit style, PR bar) is in [`CONTRIBUTING.md`](../../CONTRIBUTING.md).

## Engineering conventions
- Truthful over fluent: every agent claim carries a `file:line` ref; unknowns are stated, never invented.
- No stat dumps: emit `Insight` objects (finding/because/so_what/goal_link), not raw metrics.
- State discipline: Pydantic v2; mutate only via node returns; append-only lists use `Annotated[..., add]`; `recursion_limit=15`.
- Six deterministic tools only; the AST builds the call graph, never the LLM.
- Lane C uses guarded language and always ends with `confirm_before_pr`.
- Prompt budget ≤ 2000 input tokens per node — a convention held by review, not by a test.
- Gates: `ruff`, `mypy --strict`, `pytest` (80% coverage), `pre-commit` (ruff + mypy + gitleaks), GitHub Actions.

## Authoring
Use unique, descriptive headings in Markdown so sections stay unambiguous when linked or referenced.
