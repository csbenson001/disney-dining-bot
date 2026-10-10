#!/bin/bash
set -euo pipefail

PROJECT="${PROJECT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
cd "$PROJECT"

# Start background daemon or single poll
if [[ "${RUN_ONCE:-0}" == "1" ]]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === running single poll ==="
  xvfb-run -a python3 "$PROJECT/disney_bot.py" --once
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === starting continuous polling daemon ==="
  xvfb-run -a python3 "$PROJECT/disney_bot.py"
fi
