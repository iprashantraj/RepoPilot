# RepoPilot Startup Guide

This is the local runbook. RepoPilot is not hosted anywhere — it runs on your
machine, against your own provider key. In normal development you only need
four commands.

## The Short Version

From the repo root:

```bash
cp .env.example .env      # then set GROQ_API_KEY=...
make setup
make services
make dev
```

Then open:

```text
http://127.0.0.1:3000
```

That is it. `make dev` runs both:

- Backend API: `http://127.0.0.1:8000`
- Frontend app: `http://127.0.0.1:3000`

The app starts without a provider key, but every tour and question fails until
at least one is set — put a key in `.env` first. The frontend needs no
configuration at all for the default setup; `apps/web/.env.local` is only for
sign-in or a non-default API address.

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh`, or `winget install astral-sh.uv` on Windows |
| Python | 3.12 (`<3.14`) | `uv` downloads and pins it via `.python-version`; no system Python needed |
| Node.js + npm | 22 (what CI runs) | `nvm install 22` |
| Docker | Desktop or Engine | Must be **running** before `make services` — it hosts Postgres and Redis |
| Git | any | |

At least one LLM provider key. [Groq](https://console.groq.com/) is free and is
the default first choice; [Cerebras](https://cloud.cerebras.ai/) and a
[Hugging Face token](https://huggingface.co/settings/tokens) are optional
fallbacks used when Groq rate-limits.

## Commands You Actually Use

| Command | What it does |
|---|---|
| `make setup` | Installs Python workspace deps and frontend npm deps. |
| `make services` | Starts Postgres/Redis and runs DB migrations. |
| `make backend` | Runs only the FastAPI backend. |
| `make frontend` | Runs only the Next.js frontend. |
| `make dev` | Runs backend and frontend together. |
| `make test` | Runs the fast Python test lane. |
| `make lint` | Runs Ruff lint and format checks. |

## 1. Install Dependencies

From the repository root:

```bash
make setup
```

## 2. Configure Environment

Create a local `.env` from the checked-in template:

```bash
cp .env.example .env
```

For a real tour, add at least one chat provider key to `.env`:

```bash
GROQ_API_KEY=...
CEREBRAS_API_KEY=...
HUGGINGFACE_API_KEY=...
```

Notes:

- Unit tests can run without provider keys.
- Real repo indexing and tour generation need provider capacity.
- Embeddings use `nomic-ai/nomic-embed-text-v1.5-Q` through fastembed (ONNX, no torch) and download on first use.
- `GITHUB_PAT` is optional unless you are exercising GitHub issue-context flows.
- Leave the default local datastore settings unless you are pointing at external services:

```bash
POSTGRES_DSN=postgresql+psycopg://repopilot:repopilot@localhost:5432/repopilot
REDIS_URL=redis://localhost:6379/0
REPOPILOT_WEB_ORIGINS=http://127.0.0.1:3000,http://localhost:3000
```

### Optional: Google/GitHub sign-in and saved tours

Sign-in is off unless you configure it, and the app works fully without it —
anonymous sessions keep their own tours via the same cookie. Configure either
provider and the app becomes **gated**: the landing page is a sign-in screen,
and the repository and lens steps come after signing in. A reader's history
then follows their account across devices.

Next.js reads its own environment, so these go in `apps/web/.env.local` (not
the root `.env`):

```bash
AUTH_SECRET=<openssl rand -hex 32>
# Set either provider, or both. Each is wired only when its id is present.
AUTH_GOOGLE_ID=<oauth client id>
AUTH_GOOGLE_SECRET=<oauth client secret>
AUTH_GITHUB_ID=<oauth app client id>
AUTH_GITHUB_SECRET=<oauth app client secret>
NEXTAUTH_URL=http://localhost:3000
AUTH_URL=http://localhost:3000
AUTH_TRUST_HOST=true
# Must be byte-identical to REPOPILOT_SESSION_SECRET in the root .env.
REPOPILOT_SESSION_SECRET=repopilot-development-session-secret
```

Create the Google OAuth client at
<https://console.cloud.google.com/apis/credentials> with redirect URI
`http://localhost:3000/api/auth/callback/google`, and the GitHub OAuth app at
<https://github.com/settings/developers> with callback URL
`http://localhost:3000/api/auth/callback/github`.

Use `localhost`, not `127.0.0.1`. Auth.js derives the callback origin as
`localhost` in local dev whatever `AUTH_URL` says, so an OAuth app registered
against `127.0.0.1` fails the redirect_uri check — the provider reports that
back as nothing more specific than `error=Configuration`.

`REPOPILOT_SESSION_SECRET` is the whole handshake: the web app signs the
stable session id with it and the API verifies that signature. If the two
values differ, sign-in appears to work but the API silently treats every
request as a fresh anonymous session and no history is found.

## 3. Start Local Services

Start Docker first, then:

```bash
make services
```

This starts, waits until both containers report healthy, then runs migrations:

- Postgres 16 + pgvector on `localhost:5432`
- Redis on `localhost:6379`

To stop and clear local service volumes:

```bash
make docker-down
```

## 4. Run The App

The easiest path is one command:

```bash
make dev
```

Open the app:

```text
http://127.0.0.1:3000
```

If you prefer separate terminals:

Terminal 1:

```bash
make backend
```

Terminal 2:

```bash
make frontend
```

API docs are available at:

```text
http://127.0.0.1:8000/docs
```

The local API indexes submitted repositories in-process through the runtime service layer. No separate worker process is required for the normal dev flow.

## 5. Use The App Locally

1. Paste a public Python GitHub repo URL.
2. Wait for indexing to finish.
3. Enter what you are trying to learn or change in the repo.
4. Generate a tour or ask grounded follow-up questions.

Good smoke-test repos are small-to-medium Python projects — `pallets/flask` and
`encode/httpx` are the ones used during development. Very large repos depend
more heavily on model-provider quota and first-run embedding downloads.
Anything over 100 MB is rejected before the clone starts, and anything over
200k lines is rejected after it (`INGESTION_MAX_REPO_MB` /
`INGESTION_MAX_REPO_LOC`).

**First run is the slow one.** The fastembed embedder (~130 MB) and the
cross-encoder reranker (~91 MB) download into `~/.cache/huggingface` during the
first indexing job. Later repositories reuse them.

## 6. Run Checks

Before committing or handing off:

```bash
UV_CACHE_DIR=.uv-cache uv run ruff format --check .
UV_CACHE_DIR=.uv-cache uv run ruff check .
UV_CACHE_DIR=.uv-cache uv run pytest -m "not slow and not integration"
```

Common Make targets:

```bash
make install
make setup
make services
make backend
make frontend
make dev
make lint
make typecheck
make test
make ci
```

Web checks:

```bash
cd apps/web
npm run typecheck
npm run test:store
```

The e2e suite needs no server of its own — Playwright starts one on port 3100
with the OAuth ids cleared, and every spec mocks the API at the network layer.
Do not point it at 3000: `reuseExistingServer` would hand it whatever dev server
you already have running, sign-in gate included, and the override would silently
not apply.

```bash
cd apps/web
npm run test:e2e
```

Lighthouse does need the app running:

```bash
cd apps/web
npm run test:lighthouse
```

`typecheck`, `test:store` and `test:e2e` all run in CI on every pull request and
every push to `main` (the `web` job in `.github/workflows/ci.yml`). Lighthouse
does not.

Evals no longer run in CI. The retrieval artifact gate and the `eval-pr` / `eval-main` workflows were removed on 2026-08-08, now that the RAG phases they guarded are done. Run the bench locally (see [`evals/`](../evals/)) when a change could move ranking.

## Troubleshooting

### First-run failures

| Symptom | Cause | Fix |
|---|---|---|
| `make: uv: No such file or directory` | `uv` not installed, or installed into a shell that has not been reopened | `curl -LsSf https://astral.sh/uv/install.sh \| sh`, then open a new terminal |
| `Cannot connect to the Docker daemon` | Docker is not running | Start Docker Desktop and re-run `make services` |
| `Bind for 0.0.0.0:5432 failed: port is already allocated` | Another Postgres (Homebrew, an older project) owns the port | Stop it, or change the host port in `docker-compose.yml` and `POSTGRES_DSN` to match |
| `connection to server at "localhost", port 5432 failed` during `make services` | Postgres is not up yet | `make docker-up` waits for the healthcheck; if it still fails, `make docker-down && make services` — note `docker-down` passes `-v`, so it **deletes the local database volume** |
| App loads, indexing works, every answer errors | No provider key | Set `GROQ_API_KEY` in the repo-root `.env` (not `apps/web/.env.local`) and restart the API |
| Answers stop mid-tour with a 429 | Free-tier provider quota | Wait out the window, or add `CEREBRAS_API_KEY` / `HUGGINGFACE_API_KEY` as fallbacks |
| `EADDRINUSE :3000` or `:8000` | A previous `make dev` did not exit | Kill it: `lsof -ti:3000,8000 \| xargs kill` |

`.env` lives at the repo root and is read by the Python side (API, ingestion,
agents). `apps/web/.env.local` is read only by Next.js, and only matters for
sign-in or a non-default `API_PROXY_TARGET`. Putting a provider key in the
web env does nothing.

If `uv` tries to use a home cache that the sandbox cannot read, use the workspace cache:

```bash
UV_CACHE_DIR=.uv-cache uv run ...
```

If Postgres or Redis state looks stale:

```bash
make docker-down
make docker-up
make db-migrate
```

If first indexing is slow, check whether fastembed is downloading embedding weights and whether the configured LLM provider is rate-limiting.

### "Internal Server Error" on submit (`POST /api/repos` returns 500)

**Dev only.** The web app talks to the backend through the same-origin Next.js proxy (`/api/*` → uvicorn, see `apps/web/next.config.mjs`). The Next dev server keeps a pool of keep-alive sockets to uvicorn. If uvicorn closes an idle socket (its default `--timeout-keep-alive` is 5s) while that socket still sits in the proxy's pool, the next request reuses the dead socket and the hop fails with `ECONNRESET` / `socket hang up`. The proxy turns that into a **500** and the UI shows "Internal Server Error"; the backend logs no error because the failure is on the proxy→uvicorn hop, not in FastAPI. It is intermittent (timing-dependent) and a plain page reload usually clears it.

This is fixed by running uvicorn with a keep-alive window wider than the proxy's socket reuse gap — `make backend` / `make dev` now pass `--timeout-keep-alive 75`. The web API client (`apps/web/src/lib/api/generated.ts`) also retries idempotent requests once on a transient 5xx. If you run uvicorn by hand, add the flag:

```bash
uv run uvicorn repopilot_api.app:app --app-dir apps/api/src --host 127.0.0.1 --port 8000 --timeout-keep-alive 75
```

This does not occur in production, which sits behind a real reverse proxy rather than the Next dev server.
