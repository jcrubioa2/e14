#!/usr/bin/env bash
# Decrypt .env from .env.secret (git-secret). Run in an interactive terminal (WSL).
set -euo pipefail
cd "$(dirname "$0")/.."

export PATH="${HOME}/.local/bin:${PATH}"

if ! command -v git-secret >/dev/null 2>&1; then
  echo "Install git-secret: see docs/MULTI_MACHINE.md (git clone + make install PREFIX=\$HOME/.local)" >&2
  exit 1
fi
if [[ ! -f .env.secret ]]; then
  echo "Missing .env.secret — git pull or: git checkout origin/main -- .env.secret .gitsecret" >&2
  exit 1
fi

git secret reveal -f
echo "OK: .env revealed ($(wc -l < .env) lines). Run: .venv/bin/e14detector env-check"
