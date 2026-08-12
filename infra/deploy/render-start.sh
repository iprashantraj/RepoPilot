#!/bin/sh
# arq worker + API in one container. See infra/deploy/Dockerfile.render.
#
# Redis is external here (REDIS_URL, from the Render Key Value instance in
# render.yaml), unlike the Hugging Face variant which ran redis-server locally.
set -e

# ONNX Runtime sizes its thread pool from the host's core count, and each intra-op
# thread carries its own memory arena. On a 512 MB service that is the difference
# between running and an OOM restart, and both models are small enough that one
# thread costs little latency.
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"

arq repopilot_api.jobs.index_repo.WorkerSettings &
worker_pid=$!

# If the worker dies, the box stops indexing but keeps answering -- which looks
# like a hang to anyone who pastes a repo. Take the container down instead so
# Render restarts it.
trap 'kill "$worker_pid" 2>/dev/null' INT TERM EXIT
(while kill -0 "$worker_pid" 2>/dev/null; do sleep 5; done; echo "arq worker exited" >&2; kill 1) &

exec uvicorn repopilot_api.app:app \
  --app-dir apps/api/src \
  --host 0.0.0.0 \
  --port "${PORT:-10000}" \
  --timeout-keep-alive 75
