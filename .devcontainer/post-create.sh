#!/usr/bin/env bash
# Runs once when the devcontainer is created.
set -euo pipefail

curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

[ -f .env ] || cp .env.example .env

make setup

cat <<'EOF'

RepoPilot devcontainer ready.

  1. Put a key in .env       -> GROQ_API_KEY=...   (free at https://console.groq.com)
  2. make services           -> Postgres + Redis, then migrations
  3. make dev                -> API on :8000, web on :3000

EOF
