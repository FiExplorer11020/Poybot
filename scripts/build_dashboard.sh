#!/usr/bin/env bash
# Convenience wrapper around scripts/build_dashboard.mjs.
# Installs npm deps on first run; subsequent runs are ~50ms via esbuild.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v node >/dev/null 2>&1; then
  echo "✗ node not found — install Node 20+ (https://nodejs.org)" >&2
  exit 1
fi

if [ ! -d node_modules ]; then
  echo "[build_dashboard] node_modules missing — running npm install..."
  npm install --silent
fi

node scripts/build_dashboard.mjs "$@"
