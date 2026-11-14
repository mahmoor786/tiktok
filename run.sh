#!/usr/bin/env bash
# Starts the web page. Used by the system service; also fine to run by hand.
set -euo pipefail
cd "$(dirname "$0")"
PORT="$(grep -E '^PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]"' || true)"
# One worker only: the background job runner lives inside this process.
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}" --workers 1 --log-level warning
