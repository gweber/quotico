#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

# Load .env
set -a
source "$ROOT/.env"
set +a

# Dev overrides (local mongod has no auth)
export MONGO_URI="mongodb://localhost:27017/quotico"
export BACKEND_CORS_ORIGINS="http://localhost:5173"
export COOKIE_SECURE="false"

cleanup() {
  echo "Stopping services..."
  kill 0
  docker compose -f "$ROOT/docker-compose.yml" stop mongodb 2>/dev/null || true
}
trap cleanup EXIT

# Backend
(
  cd "$ROOT/backend"
  source .venv/bin/activate
  uvicorn app.main:app --reload --host 127.0.0.1 --port 4201
) &

# Frontend (Vite dev server)
(
  cd "$ROOT/frontend"
  pnpm dev
) &

wait
