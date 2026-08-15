# Contributing to RepoPilot

Thanks for looking. RepoPilot is open source because we are not running it as a
service — there is no hosted instance to break, so the bar for trying things
locally is low.

## Before you start

1. Get it running: [README quickstart](README.md#run-it-locally), or the full
   [startup guide](docs/STARTUP_GUIDE.md) if something goes wrong.
2. Read [`CLAUDE.md`](CLAUDE.md). It is the project's single source of truth for
   conventions, and it is short. If a rule there conflicts with this file,
   `CLAUDE.md` wins.
3. Skim [`docs/STATUS.md`](docs/STATUS.md) to see what is in flight, and
   [`docs/03_ARCHITECTURE.md`](docs/03_ARCHITECTURE.md) if you are touching the
   agent graph.

## The rules that get PRs rejected

These are the ones people trip over. Full rationale in `CLAUDE.md` §3.

- **Every factual claim an agent makes carries a `file:line` ref.** If the
  system does not know, it says so. Fluent-but-unsourced is the failure mode
  the whole project exists to avoid.
- **No stat dumps.** Agents emit `Insight` objects (`finding` / `because` /
  `so_what` / `goal_link`). An empty `so_what` fails validation on purpose.
- **No agent writes another agent's state field.** Mutate only through node
  return values (`return {"foo": [item]}`), never `state.foo.append(...)`.
- **Six deterministic tools, no more.** A seventh needs a justification that
  starts with *"the model cannot do this from the existing tools because…"*.
  The LLM never computes the call graph; the AST does.
- **Prompt budget ≤ 2000 input tokens per node.** Held by review, not by a
  test. Past it, chunk harder or split the agent.

## Development loop

```bash
make lint         # ruff check + format check
make typecheck    # mypy --strict
make test         # fast pytest lane
make ci           # lint + typecheck + coverage — same gates as GitHub Actions
```

Frontend:

```bash
cd apps/web
npm run typecheck
npm run test:store
npm run test:e2e     # Playwright starts its own server on :3100
```

Install the hooks once and most of this happens before the commit lands:

```bash
uv run pre-commit install
```

`make ci` is the one that matters — it mirrors `.github/workflows/ci.yml`
(ruff, `mypy --strict`, pytest at 80% coverage, gitleaks) plus the `web` job.
If it passes locally it passes in CI.

Tests that need Docker services and a provider key live behind markers and are
not in the fast lane:

```bash
make test-slow           # needs `make services` and an LLM key
make test-eval-sampled   # retrieval bench; run it if you touched ranking
```

Evals do not run in CI. If your change could move retrieval quality, run the
sampled bench and put the numbers in the PR.

## Commits and PRs

Conventional commits, matching the existing history:

```text
fix(ingest): reject oversized repos before cloning them
docs(deploy): finish the render.yaml path move
```

Scopes already in use: `web`, `qa`, `ingestion`, `api`, `llm`, `graph`, `core`,
`access`, `evals`, `deploy`, `ci`. Pick the closest one rather than inventing a
new one.

A PR is ready when:

- [ ] The change is self-reviewed against `CLAUDE.md` §3.
- [ ] `make ci` passes, and the web checks pass if you touched `apps/web`.
- [ ] Docs are updated if behaviour, architecture or setup changed.

Small PRs get read. A 2000-line refactor with no issue behind it probably
will not.

## Filing issues

Bug reports want the repository URL you pasted, what you expected, and the
relevant log lines — the API logs are usually more useful than the browser
console. Use the templates; they ask for what we end up asking for anyway.

Security-relevant findings: please open a private security advisory on GitHub
rather than a public issue. Known accepted gaps are already written down in
[`docs/AUDIT_REPORT.md`](docs/AUDIT_REPORT.md) and
[`docs/STATUS.md`](docs/STATUS.md) — check there first, since "no spend ceiling
on the model key" and friends are documented decisions for a private,
locally-run instance rather than oversights.

## What is worth working on

- **Language coverage.** AST call graphs are Python-only. Every other language
  gets grounded text retrieval and no graph edges. TypeScript is the obvious
  next one.
- **Grounding at answer level.** Per-claim verification is strong; "every claim
  in this answer verified" is the harder bar and is still open.
- **The switched-off features.** Query Understanding, Ingestion Enrichment and
  Context Compression are implemented and measured but disabled because they
  missed their eval gates. Beating those gates is a real contribution.

## License

By contributing you agree your work is released under the
[MIT License](LICENSE).
