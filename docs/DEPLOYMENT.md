# Production deployment

RepoPilot deploys as a web service, an API service, a background worker, Postgres with pgvector, and Redis. Keep the API and worker in the same region as Postgres.

## Service topology

| Service | Build | Start command |
|---|---|---|
| Web | `apps/web/Dockerfile` with `apps/web` as build context | image default |
| API | `Dockerfile.api` with repository root as build context | image default |
| Worker | `Dockerfile.api` with repository root as build context | `arq repopilot_api.jobs.index_repo.WorkerSettings` |
| Migration job | `Dockerfile.api` | `cd packages/ingestion && alembic upgrade head` |

In production, platform-key indexing runs on the ARQ worker. BYOK indexing stays inside the API process because raw user keys are held in process memory and must never be copied into Redis. Keep one API replica while BYOK indexing is active; moving those jobs across replicas requires an encrypted credential-reference service.

## Required production environment

```bash
REPOPILOT_ENV=production
REPOPILOT_WEB_ORIGINS=https://your-domain.example
REPOPILOT_SESSION_SECRET=<openssl-rand-hex-32>
REPOPILOT_SESSION_COOKIE_SECURE=true
POSTGRES_DSN=<managed-postgres-with-pgvector>
REDIS_URL=<managed-redis-tls-url>
GROQ_API_KEY=<platform-free-tier-key>
CEREBRAS_API_KEY=<platform-free-tier-key>
API_PROXY_TARGET=https://api.your-domain.example
NEXT_PUBLIC_API_BASE_URL=/api
```

`CEREBRAS_API_KEY` is not optional in practice. Groq's free tier caps
`llama-3.3-70b-versatile` at 12,000 tokens per minute, and one question spends
roughly 8,000 of them — so the second question inside a minute is refused
(Groq reports this as HTTP 413, not 429). With a Cerebras key the chain fails
over and the reader never sees it. Without one, every second question drops to
the keyword-only answer. `LLM_HF_CHAT_FALLBACK=1` adds Hugging Face as a third
tier; leave it off unless the HF account has credits, since its free budget is
about $0.10/month.

`REPOPILOT_SESSION_SECRET` must be stable across API deployments. Changing it signs every browser out. It also keys the Fernet cipher for stored provider keys (`product_credentials`): rotating it invalidates every saved key, and users must reconnect them.

### Sign-in (web service)

Set these on the **web** service only, alongside the same
`REPOPILOT_SESSION_SECRET` the API uses:

```bash
AUTH_SECRET=<openssl-rand-hex-32>
AUTH_GOOGLE_ID=<oauth client id>
AUTH_GOOGLE_SECRET=<oauth client secret>
AUTH_GITHUB_ID=<oauth app client id>
AUTH_GITHUB_SECRET=<oauth app client secret>
NEXTAUTH_URL=https://your-domain.example
REPOPILOT_SESSION_SECRET=<same value as the API>
REPOPILOT_SESSION_COOKIE_SECURE=true
```

OAuth callback URLs: `https://your-domain.example/api/auth/callback/google`
and `.../callback/github`. Configure whichever providers you set ids for —
each one is wired only when its id is present.

The web app signs a stable, account-derived session id with
`REPOPILOT_SESSION_SECRET` and writes it to the same `repopilot_session`
cookie the API already verifies — there is no second token format and no
bearer plumbing. The two values must match byte for byte; a mismatch degrades
silently to anonymous sessions rather than erroring. With **both** provider
ids unset there is no gate and the product runs anonymously, exactly as
before; with either set, the landing page is a sign-in screen and the
repository step comes after sign-in.

### Managed Postgres (Aiven)

Any Postgres with `pgvector` works. For Aiven:

1. Create a Postgres service (a Hobbyist/Startup plan is enough).
2. Enable the extension: `CREATE EXTENSION IF NOT EXISTS vector;`
3. Copy the *service URI* into `POSTGRES_DSN` unchanged, keeping
   `sslmode=require`. `make_engine` rewrites `postgresql://` to
   `postgresql+psycopg://`, so no edit is needed.
4. Run the migration job (`alembic upgrade head`) against it.

## Release sequence

1. GitHub Actions runs Python lint, formatting, strict MyPy, tests, secret scanning, frontend typechecking, and the production Next.js build.
2. Build the API and web images from the Dockerfiles.
3. Run the migration job once against the target Postgres database.
4. Deploy the API and worker.
5. Verify `GET /health`, Postgres connectivity, and Redis connectivity.
6. Deploy the web image with `API_PROXY_TARGET` pointing at the API.
7. Smoke test sign-in, one repository, a few questions, the key gate, and SSE streaming.

Do not expose the API and web on unrelated sites while using cookie sessions. Prefer `app.example.com` and `api.example.com`, or proxy `/api` through the Next.js service as configured here.

---

# Free-tier deployment plan

The topology above assumes paid managed services. This section is the concrete
plan for a zero-cost first deployment, and the constraints that shape it.
Everything here is reachable without a credit card.

`infra/deploy/deploy.sh` automates it. This document is why it does what it
does.

## What forces the shape

- Embeddings run **in-process** via `fastembed` (ONNX Runtime), not
  `sentence-transformers`. That is a deployment decision, not a modelling one:
  both serve the same `nomic-embed-text-v1.5`, but sentence-transformers drags
  torch in with it — ~2.5 GB of image and ~2 GB resident — which eliminates
  every free tier capped at 512 MB. The quantized ONNX build (`-Q`) is 0.13 GB,
  keeps all 768 dimensions and the full 8192-token context, and the reranker
  already runs on fastembed, so ONNX Runtime is loaded either way.
- Hugging Face Spaces **is no longer an option**. Docker and Gradio Spaces on
  free `cpu-basic` now require a PRO subscription; creating one returns
  `402 Payment Required`. Static Spaces remain free and cannot run this.
- Redis carries **only the ARQ job queue**. Nothing in it is durable; a restart
  loses in-flight index jobs, which can be re-run.
- Postgres needs `pgvector`.
- Web and API must be same-site for the `repopilot_session` cookie, and the
  web app signs that cookie itself with the shared `REPOPILOT_SESSION_SECRET`
  (see `apps/web/src/lib/identity.ts`).

## Target topology

| Piece | Where | Free tier |
|---|---|---|
| Web (Next.js) | Vercel | Hobby |
| API + ARQ worker | One Render web service | free: 512 MB, sleeps after 15 min idle |
| Redis | Render Key Value | free: 25 MB |
| Postgres + pgvector | Neon | free project |

Render because its free tier needs no card and takes a Docker image. Redis is a
managed Key Value instance rather than a process inside the container — the
opposite of the Hugging Face layout this replaced — because 512 MB has no room
to spare. Upstash was rejected for the same reason it always was: its free tier
meters per command, and ARQ polls its queue continuously, so one idle worker
burns roughly 5 M commands a month against a 500 K cap. Render's Key Value
instance is not metered that way.

Postgres stays on Neon rather than Render because Render's free Postgres
expires after 30 days.

One service runs API and worker together. The BYOK constraint above still
holds — one API replica — and a single container satisfies it by construction.

## Blocking prerequisites

`docs/STATUS.md` records that the accepted spend risks assume a private
deployment, and that the decision "expires the moment the API is reachable by
anyone who is not paying for it" — including an unlisted URL. A Render service
has a public URL. Close these two first:

- **Finding 3** — `reserve_question` passes `free_limit=None`, so questions are
  unmetered. Pass a limit from settings; the counting code already exists.
- **Finding 4** — `POST /intent` is an unauthenticated model call with no rate
  limit. Add a per-session sliding window as a FastAPI dependency.

**Finding 5** is worth closing at the same time here, for a reason unrelated to
abuse: the service has ephemeral disk and a repository is cloned in full before
`ingestion_max_repo_loc` (200 000) applies. `clone_to_tempdir` already clones
shallow and single-branch; adding `--filter=blob:none`, or a GitHub API size
check before cloning, keeps one pasted monorepo from filling the container.

## Step 1 — Postgres on Neon

1. Create the Neon project and the Render services in the **same** region.
   `render.yaml` (repository root) ships with `singapore`; change both `region:` keys if your
   Neon project lives elsewhere. Retrieval issues several queries per
   question, so a cross-region database pays that round trip every time.
2. `CREATE EXTENSION IF NOT EXISTS vector;` on the target database.
3. Take the pooled connection string. `make_engine` rewrites `postgresql://`
   to `postgresql+psycopg://`, so paste it unchanged and keep `sslmode=require`.
4. `./infra/deploy/deploy.sh db` runs `infra/postgres/init.sql` and
   `alembic upgrade head` against it.

## Step 2 — create the Render services

`render.yaml` is a Blueprint declaring both services: the Docker web service
and the Key Value instance, already wired to each other through `REDIS_URL`.
It sits at the repository root because that is the only place Render reads a
Blueprint from — there is no way to point the dashboard at a nested path.

1. Render dashboard → **New → Blueprint** → pick this repository. Render finds
   `render.yaml` on its own.
2. The apply prompts for every var marked `sync: false`. Fill them in, or leave
   them blank and let `deploy.sh api` push them.
3. Note the service id (`srv-…`, in the service URL) and create an API key at
   `dashboard.render.com/u/settings#api-keys`.

The image is `infra/deploy/Dockerfile.render`. It differs from `Dockerfile.api`
in three ways, all forced by the host: the ARQ worker runs alongside uvicorn
(Render's free plan has no worker type), Redis is external, and both models'
weights are baked in at build time so a cold start does not also download them.

## Step 3 — deploy

```bash
cp infra/deploy/.env.deploy.example .env.deploy   # then fill it in
./infra/deploy/deploy.sh                          # db + api + web
```

The `api` stage pushes environment variables over the Render API and triggers a
deploy. Two things to know about it:

- Render replaces the **whole** env-var set in one `PUT`. The script sends the
  full array; anything it omits is dropped from the service. `REDIS_URL` is
  deliberately not in that list — `render.yaml` links it to the Key Value
  service, and sending a literal would overwrite the link.
- Render builds the commit **pushed to GitHub**, not the local working tree.
  The script warns on a dirty tree rather than deploying something else.

`REPOPILOT_ENV=production` makes the settings validator reject a default
session secret and a non-secure cookie, so both are mandatory, not advisory.

## Step 4 — the web app on Vercel

Handled by `./infra/deploy/deploy.sh web`, which sets:

```bash
API_PROXY_TARGET=https://<service>.onrender.com
NEXT_PUBLIC_API_BASE_URL=/api
REPOPILOT_SESSION_SECRET=<byte-for-byte the same value as the API>
REPOPILOT_SESSION_COOKIE_SECURE=true
```

Sign-in is optional. With both `AUTH_GOOGLE_ID` and `AUTH_GITHUB_ID` unset the
product runs anonymously. If you set either, also set `AUTH_SECRET` and
`NEXTAUTH_URL`, and register the callback URLs listed earlier in this file.

Keeping `NEXT_PUBLIC_API_BASE_URL=/api` routes the browser through the Next.js
rewrite, so everything is same-origin and the cookie needs no cross-site
exception. The alternative — pointing the browser straight at the API — works
too, but then `REPOPILOT_WEB_ORIGINS` and the `SameSite=None` cookie path both
become load-bearing.

## Step 5 — verify

1. `GET https://<service>.onrender.com/health` returns 200.
2. Render logs show `arq.startup` and no Redis connection error.
3. Load the Vercel URL, paste a small repository (`pallets/click` is a good
   size), and watch indexing complete — that exercises clone, parse, embed,
   Postgres write, and the ARQ round trip in one go.
4. Confirm the tour SSE stream renders progressively rather than arriving in
   one block. This is the most likely thing to break: Vercel proxies the
   rewrite through its edge, and a buffered or time-capped proxy shows up here
   first. If it does break, switch `NEXT_PUBLIC_API_BASE_URL` to the API URL
   so the browser connects directly, and add that origin to
   `REPOPILOT_WEB_ORIGINS`.
5. Confirm the free-repository gate and the "connect your Groq key" 402 path.

## Known ceilings

- **512 MB is the binding constraint.** A cold process serving one question
  sits near 400 MB with both ONNX models resident. Indexing a large repository
  concurrently is what will push it over. `OMP_NUM_THREADS=1` (set in
  `render-start.sh`) is part of staying under: ONNX Runtime otherwise sizes its
  thread pool from the host core count and each intra-op thread carries its own
  memory arena. If the service still OOM-restarts, the next lever is
  `BAAI/bge-small-en-v1.5` — but it is 384-dim, so it needs a migration of
  `chunk_embeddings.embedding` and a full re-index, and its 512-token context
  truncates the tail of any chunk near the 150-line cap.
- A free service sleeps after 15 minutes idle. The first request after that
  pays a container cold start, roughly 40–60s. Weights are baked into the
  image, so it is boot time, not a download.
- Render's free tier has a monthly instance-hour budget shared across services.
  One always-sleeping API fits; two do not.
- Disk is ephemeral. Clones and the LLM SQLite cache (`LLM_CACHE_PATH`) do not
  survive a restart; the index in Postgres does.
- One container means one API replica and one worker. Concurrent indexing of
  two large repositories will contend for CPU with request serving.
- Neon's free tier suspends an idle database, adding a cold-start delay to the
  first query.
- Rotating `REPOPILOT_SESSION_SECRET` signs everyone out **and** invalidates
  every stored provider key in `product_credentials`. Set it once.
