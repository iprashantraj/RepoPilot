# RepoPilot — Project Guide (Single Source of Truth)

This file is the **primary, always-loaded set of project rules** for RepoPilot. Every Claude Code prompt and every contributor should treat it as authoritative. Keep it concise: deep design detail lives in [`docs/`](docs/) — this file is the index and the rules, not the encyclopedia.

> If a rule here conflicts with anything else, **this file wins.** When you change a convention, change it here first.

---

## 1. What this project is

**RepoPilot** (internally "Codebase Archaeologist") is a web app where a developer pastes a public GitHub repo URL and gets a **purpose-driven, guided onboarding tour** of an unfamiliar codebase, powered by a multi-agent AI system. Beachhead: junior devs and first-time OSS contributors on **Python** repos.

The distinguishing bet: **before analyzing anything, the system captures pre-context (purpose + focus) and adapts every downstream agent to it.** Product thesis and tech-stack rationale are archived under [`docs/archive/`](docs/archive/) — still true, no longer load-bearing for current work.

| Want to understand… | Read |
|---|---|
| Where the project stands right now | [`docs/STATUS.md`](docs/STATUS.md) |
| How to run the project locally | [`docs/STARTUP_GUIDE.md`](docs/STARTUP_GUIDE.md) |
| How outside contributors are expected to work | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| Agent topology, state schema, tools, verifier | [`docs/03_ARCHITECTURE.md`](docs/03_ARCHITECTURE.md) |
| Historical: product thesis, tech-stack rationale | [`docs/archive/`](docs/archive/) |

---

## 2. How project knowledge is organized

RepoPilot uses four layers of project knowledge. Know which one to edit.

| Layer | Location | Committed? | Purpose |
|---|---|---|---|
| **Project rules** | `CLAUDE.md` (this file) | ✅ Yes | Single source of truth. Auto-loaded by Claude Code every session. |
| **Portable rules** | [`.agents/rules/`](.agents/rules/) | ✅ Yes | The same rules in a tool-neutral format so other AI assistants (Cursor, Gemini, etc.) pick them up. Keep in sync with this file. |
| **Harness config** | [`.claude/`](.claude/) | ✅ Yes | Claude Code-specific wiring: settings and permissions. |
| **Per-user memory** | `~/.claude/…/memory/` | ❌ No (local) | Your private notes across sessions. Never holds shared project rules — those belong here. |

**Interaction model:** `CLAUDE.md` defines the rules → `.agents/rules/` mirrors them for other tools → `.claude/` carries harness wiring. Per-user memory is personal and additive; it must never contradict this file.

---

## 3. Engineering conventions

Enforced in code review and CI — not optional. Full rationale in [`docs/03_ARCHITECTURE.md`](docs/03_ARCHITECTURE.md) (and, historically, in [`docs/archive/02_TECH_STACK.md`](docs/archive/02_TECH_STACK.md)).

- **Truthful over fluent.** Every factual claim from an agent carries a `file:line` ref. Unknown → say so; never invent. Verifier rejections render as "flagged", never silently dropped.
- **No stat dumps.** Agents emit `Insight` objects (`finding` / `because` / `so_what` / `goal_link`), not raw metrics. Empty `so_what`/`goal_link` fails Pydantic validation by design.
- **State discipline.** Pydantic v2. No agent writes another agent's field; mutate only via node return values (`return {"foo": [item]}`, never `state.foo.append`). Append-only lists use `Annotated[..., add]`. `recursion_limit=15`.
- **Six deterministic tools, no more.** A new tool needs a justification starting with "the model cannot do this from existing tools because…". The LLM never computes the call graph — the AST does.
- **Lane C language constraints.** Suspicions use guarded language ("worth investigating", not "bug") and always end with a `confirm_before_pr` step. Enforced in prompt and post-checked by the Verifier.
- **Prompt budget ≤ 2000 input tokens per node.** A convention, held by review — no test measures tokens. What CI *does* enforce is the character cap on chunk bodies (`MAX_CHUNK_CHARS` / `MAX_CHUNKS_CHARS`, `test_prompt_size.py`), which is looser. Past the budget, chunk harder or split the agent.
- **Quality gates:** `ruff` (lint), `mypy --strict` (from day one), `pytest` with **80% coverage**, `pre-commit` (ruff + mypy + gitleaks), GitHub Actions CI. `gitleaks` blocks secret leaks.

---

## 4. Definition of Done

A major task is **not** complete until:

- [ ] Code/docs changes done and self-reviewed against §3.
- [ ] Tests/lints relevant to the change pass.
- [ ] Docs updated if behavior, architecture, or setup changed.

---

## 5. For contributors (human or AI)

1. **Read this file first**, then the relevant `docs/` page.
2. **Edit the right layer** (§2): shared rules → here + `.agents/rules/`; harness wiring → `.claude/`; design depth → `docs/`.
3. **Keep `CLAUDE.md` and `.agents/rules/project.md` in sync** — they say the same things for different tools.
4. **Use unique, descriptive section headings** in this file and in `docs/`.
