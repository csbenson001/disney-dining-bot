#!/bin/bash
set -euo pipefail

PROJECT="${PROJECT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
cd "$PROJECT"

if [[ "${SERVICE_TYPE:-}" == "autobook" || "${RUN_AUTOBOOK:-0}" == "1" ]]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === starting Lightning Lane autobook service on port ${PORT:-8000} ==="
  exec uvicorn autobook_service:app --host 0.0.0.0 --port "${PORT:-8000}"
fi

# Start background daemon or single poll
if [[ "${RUN_ONCE:-0}" == "1" ]]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === running single poll ==="
  xvfb-run -a python3 "$PROJECT/disney_bot.py" --once
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === starting continuous polling daemon ==="
  xvfb-run -a python3 "$PROJECT/disney_bot.py"
fi
