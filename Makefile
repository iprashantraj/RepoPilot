.PHONY: help setup services backend frontend dev install lint typecheck test test-slow test-eval-sampled test-eval-full cov ci precommit fmt clean docker-up docker-down db-migrate resummarise resummarise-check

help:
	@echo "RepoPilot — common targets"
	@echo "  setup        install Python + web dependencies"
	@echo "  services     start Postgres/Redis and run DB migrations"
	@echo "  backend      run FastAPI at http://127.0.0.1:8000"
	@echo "  frontend     run Next.js at http://127.0.0.1:3000"
	@echo "  dev          run backend + frontend together"
	@echo "  install      uv sync (install workspace + dev deps)"
	@echo "  lint         ruff check"
	@echo "  fmt          ruff format"
	@echo "  typecheck    mypy --strict on every package"
	@echo "  test         pytest (fast lane — slow/integration markers skipped)"
	@echo "  test-slow    pytest -m 'slow and integration' (needs docker-up + db-migrate)"
	@echo "  test-eval-sampled  pytest -m eval_sampled (PR-time, ≤5 min, 1 repo + subset)"
	@echo "  test-eval-full     pytest -m eval_full (main-time, ≤30 min, full matrix)"
	@echo "  cov          pytest with coverage gate (80%)"
	@echo "  ci           lint + typecheck + test (matches GitHub Actions)"
	@echo "  precommit    run pre-commit on all files"
	@echo "  docker-up    start Postgres+pgvector and Redis, wait until healthy"
	@echo "  docker-down  docker compose down -v"
	@echo "  resummarise        re-run summaries for REPO=<id> (or REPO=--all)"
	@echo "  resummarise-check  report placeholder summaries, write nothing"
	@echo "  db-migrate   alembic upgrade head (runs ingestion migrations)"

setup:
	uv sync --all-packages --all-groups
	cd apps/web && npm install

services: docker-up db-migrate

backend:
	uv run uvicorn repopilot_api.app:app --app-dir apps/api/src --reload --reload-dir apps/api/src --host 127.0.0.1 --port 8000 --timeout-keep-alive 75

frontend:
	cd apps/web && npm run dev

dev:
	@echo "Starting backend on http://127.0.0.1:8000 and frontend on http://127.0.0.1:3000"
	@uv run uvicorn repopilot_api.app:app --app-dir apps/api/src --reload --reload-dir apps/api/src --host 127.0.0.1 --port 8000 --timeout-keep-alive 75 & \
	api_pid=$$!; \
	(cd apps/web && npm run dev) & \
	web_pid=$$!; \
	trap 'kill $$api_pid $$web_pid 2>/dev/null' INT TERM EXIT; \
	wait $$api_pid $$web_pid

install:
	uv sync --all-packages --all-groups

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check . --fix

typecheck:
	uv run mypy packages apps

test:
	uv run pytest -m "not slow and not integration"

# Phase 1 slow lane — runs the 90s httpx index gate + the call-chain test.
# Prereqs: `make docker-up` (waits for healthchecks) and `make db-migrate`.
# Needs GROQ_API_KEY in .env (chunk summaries). Sentence-transformers weights
# download on first run into ~/.cache/huggingface (~250MB).
test-slow:
	uv run pytest -m "slow and integration" --no-cov -ra

# Two-tier eval strategy (docs/06 S6).
# Sampled (PR-time): 1 repo (httpx), subset of datasets, target ≤5 min.
# Tests skip with helpful messages when their dataset file is absent so this
# target stays green on a fresh checkout where Blockers 2/3 aren't done yet.
test-eval-sampled:
	uv run pytest -m eval_sampled --no-cov -ra

# Full matrix (main-time, post-merge): fastapi/httpx/flask, all datasets,
# target ≤30 min. Only invoked by .github/workflows/eval-main.yml.
test-eval-full:
	uv run pytest -m eval_full --no-cov -ra

cov:
	uv run pytest -m "not slow and not integration" --cov-fail-under=80

ci: lint typecheck cov

precommit:
	uv run pre-commit run --all-files

docker-up:
	docker compose up -d --wait

docker-down:
	docker compose down -v

resummarise:
	@test -n "$(REPO)" || (echo 'usage: make resummarise REPO=owner/name@sha  (or REPO=--all)'; exit 1)
	uv run python -m repopilot_ingestion.resummarise $(REPO)

resummarise-check:
	uv run python -m repopilot_ingestion.resummarise --all --dry-run

db-migrate:
	cd packages/ingestion && uv run alembic upgrade head

clean:
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .mypy_cache -o -name .ruff_cache \) -prune -exec rm -rf {} +
	rm -rf .coverage htmlcov
