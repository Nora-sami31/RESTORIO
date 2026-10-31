#!/usr/bin/env bash
# One command to run the whole demo.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

# Read a few values for display and port handling. We deliberately do NOT
# `source` .env: a value written as `KEY= value` would make the shell execute
# the value as a command, and a key is not something to hand to the shell at
# all. Python loads .env itself in backend/app/config.py.
envget() { [ -f .env ] || return 0; sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" .env | tail -1; }

PORT="${RESTORIO_PORT:-$(envget RESTORIO_PORT)}"; PORT="${PORT:-8080}"
PY="${PYTHON:-python3}"

echo "RESTORIO — starting"
echo "  python : $($PY --version 2>&1)"

$PY -c "import fastapi, uvicorn" 2>/dev/null || {
  echo "  installing backend dependencies…"
  $PY -m pip install -q -r backend/requirements.txt
}

# The built website ships with this package, so a reviewer who only wants to
# run it never needs npm at all. Dependencies are installed only if the build
# is actually missing.
if [ ! -d frontend/dist ] || [ ! -f frontend/dist/index.html ]; then
  if [ ! -d frontend/node_modules ]; then
    echo "  installing website dependencies (first run only)…"
    ( cd frontend && npm install --silent )
  fi
  echo "  building the website…"
  ( cd frontend && npm run build >/dev/null )
fi

lsof -ti :"$PORT" >/dev/null 2>&1 && {
  echo "  port $PORT is busy — stopping what is on it"
  lsof -ti :"$PORT" | xargs kill -9 2>/dev/null || true
  sleep 1
}

HOSTV="${RESTORIO_HOST:-$(envget RESTORIO_HOST)}"; HOSTV="${HOSTV:-127.0.0.1}"
PROV="${RESTORIO_PROVIDER:-$(envget RESTORIO_PROVIDER)}"; PROV="${PROV:-auto (no .env yet)}"
MODL="${RESTORIO_MODEL:-$(envget RESTORIO_MODEL)}"; MODL="${MODL:-default}"
AUTO="${RESTORIO_AUTONOMY:-$(envget RESTORIO_AUTONOMY)}"; AUTO="${AUTO:-guarded}"
echo "  provider: $PROV   model: $MODL   autonomy: $AUTO"
echo ""
echo "  →  http://127.0.0.1:$PORT"
echo ""
cd backend
exec $PY -m uvicorn app.main:app --host "$HOSTV" --port "$PORT"
