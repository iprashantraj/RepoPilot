## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## Why

<!-- What was wrong or missing. -->

## Checks

- [ ] `make ci` passes (ruff, `mypy --strict`, pytest ≥ 80% coverage)
- [ ] Web checks pass if `apps/web` changed (`npm run typecheck`, `test:store`, `test:e2e`)
- [ ] Docs updated if behaviour, architecture or setup changed
- [ ] Self-reviewed against [`CLAUDE.md`](../CLAUDE.md) §3 — grounded claims, no stat dumps, state discipline

<!-- Touched retrieval or ranking? Run `make test-eval-sampled` and paste the numbers. -->
