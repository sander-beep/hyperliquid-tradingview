#!/usr/bin/env bash
# One-command deploy: pull latest code and (re)build the stack.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "ERROR: .env missing — cp .env.example .env, fill it in, chmod 600 .env" >&2
  exit 1
fi

git pull --ff-only || echo "(not a git checkout or no upstream — deploying local tree)"
docker compose up -d --build
docker compose ps
echo "Deployed. Tail logs with: docker compose logs -f app"
