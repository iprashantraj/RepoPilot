#!/usr/bin/env bash
# One-command deploy of RepoPilot onto free tiers.
#
#   API + arq worker    -> Render free web service (512 MB, sleeps when idle)
#   Redis               -> Render Key Value (free tier)
#   Postgres + pgvector -> whatever DSN you put in .env.deploy (Neon, Supabase)
#   Next.js web         -> Vercel
#
# Hugging Face Spaces used to host the API, but HF now requires a PRO
# subscription for Docker Spaces on free hardware (402 on create). Render's
# free tier needs no card. Its 512 MB is the reason the embedder runs on
# fastembed rather than sentence-transformers -- see render.yaml.
#
# The api stage pushes secrets and triggers a deploy; it does NOT create the
# service. Create that once from render.yaml at the repo root (New -> Blueprint).
#
# Usage:
#   cp infra/deploy/.env.deploy.example .env.deploy   # then fill it in
#   ./infra/deploy/deploy.sh                          # db + api + web
#   ./infra/deploy/deploy.sh api web                  # only those stages
#
# Needs: git, curl, psql, uv, npx (Vercel CLI is fetched via npx).
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
ENV_FILE="${ENV_FILE:-.env.deploy}"

die() { echo "deploy: $*" >&2; exit 1; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[ -f "$ENV_FILE" ] || die "$ENV_FILE not found — copy infra/deploy/.env.deploy.example"
set -a; . "./$ENV_FILE"; set +a

for v in RENDER_API_KEY RENDER_SERVICE_ID POSTGRES_DSN REPOPILOT_SESSION_SECRET WEB_URL API_URL; do
  [ -n "${!v:-}" ] || die "$v is unset in $ENV_FILE"
done
[ -n "${GROQ_API_KEY:-}${CEREBRAS_API_KEY:-}${HUGGINGFACE_API_KEY:-}" ] \
  || die "set at least one of GROQ_API_KEY / CEREBRAS_API_KEY / HUGGINGFACE_API_KEY"
stages=("$@"); [ ${#stages[@]} -eq 0 ] && stages=(db api web)
has() { printf '%s\n' "${stages[@]}" | grep -qx "$1"; }

# ── db ──────────────────────────────────────────────────────────────────────
# psql needs a libpq URL; the app's DSN carries SQLAlchemy's +psycopg driver tag.
if has db; then
  step "Postgres: extensions + alembic upgrade head"
  psql "${POSTGRES_DSN/+psycopg/}" -v ON_ERROR_STOP=1 -f infra/postgres/init.sql
  (cd packages/ingestion && POSTGRES_DSN="$POSTGRES_DSN" uv run alembic upgrade head)
fi

# ── api ─────────────────────────────────────────────────────────────────────
if has api; then
  step "Render: environment variables"
  # Render replaces the whole env-var set in one PUT, so this builds the full
  # array and sends it once. Anything omitted here is dropped from the service.
  render_api() {
    method="$1"; path="$2"; shift 2
    curl -fsS -X "$method" "https://api.render.com/v1$path" \
      -H "Authorization: Bearer $RENDER_API_KEY" \
      -H "Content-Type: application/json" "$@"
  }
  env_json="$(
    REPOPILOT_WEB_ORIGINS="$WEB_URL" \
    REPOPILOT_ENV=production \
    REPOPILOT_SESSION_COOKIE_SECURE=true \
    python3 - <<'PY'
import json, os

# REDIS_URL is deliberately absent: render.yaml wires it from the Key Value
# service, and sending it here would overwrite that link with a literal.
keys = [
    "POSTGRES_DSN", "REPOPILOT_SESSION_SECRET", "REPOPILOT_WEB_ORIGINS",
    "REPOPILOT_ENV", "REPOPILOT_SESSION_COOKIE_SECURE", "REPOPILOT_LOG_LEVEL",
    "GROQ_API_KEY", "CEREBRAS_API_KEY", "HUGGINGFACE_API_KEY", "GITHUB_PAT",
]
out = [{"key": k, "value": os.environ[k]} for k in keys if os.environ.get(k)]
print(json.dumps(out))
PY
  )"
  render_api PUT "/services/$RENDER_SERVICE_ID/env-vars" -d "$env_json" >/dev/null \
    || die "could not set env vars (is RENDER_SERVICE_ID correct, and the Blueprint applied?)"
  echo "  set $(printf '%s' "$env_json" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))') variables"

  step "Render: trigger deploy"
  # Render builds from the connected GitHub repo, so it deploys what is pushed
  # there -- not the local working tree. Warn rather than deploy something else.
  if [ -n "$(git status --porcelain)" ]; then
    echo "  warning: working tree is dirty; Render builds the pushed commit, not these edits" >&2
  fi
  deploy_id="$(render_api POST "/services/$RENDER_SERVICE_ID/deploys" -d '{"clearCache":"do_not_clear"}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
  echo "  deploy $deploy_id queued"
  echo "  logs: https://dashboard.render.com/web/$RENDER_SERVICE_ID/deploys/$deploy_id"
fi

# ── web ─────────────────────────────────────────────────────────────────────
if has web; then
  step "Vercel: env + production deploy"
  vercel() { npx --yes vercel@latest "$@" --cwd apps/web --token "$VERCEL_TOKEN" --yes; }
  [ -n "${VERCEL_TOKEN:-}" ] || die "VERCEL_TOKEN is unset in $ENV_FILE"
  vercel link --project "${VERCEL_PROJECT:-repopilot}" >/dev/null

  set_env() {
    [ -n "${2:-}" ] || return 0
    vercel env rm "$1" production >/dev/null 2>&1 || true
    printf '%s' "$2" | vercel env add "$1" production >/dev/null
    echo "  set $1"
  }
  set_env API_PROXY_TARGET "$API_URL"
  set_env NEXT_PUBLIC_API_BASE_URL "${NEXT_PUBLIC_API_BASE_URL:-}"
  set_env AUTH_SECRET "${AUTH_SECRET:?set AUTH_SECRET in $ENV_FILE}"
  set_env NEXTAUTH_URL "$WEB_URL"
  set_env AUTH_GITHUB_ID "${AUTH_GITHUB_ID:-}"
  set_env AUTH_GITHUB_SECRET "${AUTH_GITHUB_SECRET:-}"
  set_env AUTH_GOOGLE_ID "${AUTH_GOOGLE_ID:-}"
  set_env AUTH_GOOGLE_SECRET "${AUTH_GOOGLE_SECRET:-}"
  set_env REPOPILOT_SESSION_SECRET "$REPOPILOT_SESSION_SECRET"
  set_env REPOPILOT_SESSION_COOKIE_SECURE true

  vercel deploy --prod
fi

step "Done"
echo "  API: $API_URL/health"
echo "  Web: $WEB_URL"
echo "  A free Render service sleeps after 15 min idle; the first request after"
echo "  that wakes it — allow ~60s."
