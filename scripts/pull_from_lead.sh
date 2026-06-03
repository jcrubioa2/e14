#!/usr/bin/env bash
# Pull actas + .env from the lead machine over Tailscale SSH. Run interactively.
set -euo pipefail
cd "$(dirname "$0")/.."

LEAD="${E14_LEAD_HOST:-ryzen9}"     # WSL Tailscale MagicDNS name or 100.x IP
PORT="${E14_LEAD_SSH_PORT:-22}"
USER="${E14_LEAD_USER:-quicazan}"
REMOTE="${USER}@${LEAD}"
# Override if lead repo lives elsewhere (discover with: bash scripts/pull_from_lead.sh --probe)
REPO="${E14_LEAD_REPO:-}"

ssh_cmd() { ssh -p "$PORT" "$REMOTE" "$@"; }
rsync_pull() {
  rsync -av --info=progress2 -e "ssh -p ${PORT}" \
    "$REMOTE:$1" "$2"
}

probe_remote() {
  echo ">> Probing lead ($REMOTE:$PORT) ..."
  ssh_cmd bash -s <<'REMOTE'
set -euo pipefail
echo "hostname: $(hostname)"
echo "user:     $(whoami)"
echo "home:     $HOME"
for d in "$HOME/e14" "$HOME/e14/data" "$HOME/e14/data/actas"; do
  if [[ -d "$d" ]]; then
    echo "dir $d: $(find "$d" -maxdepth 1 2>/dev/null | wc -l) entries"
  else
    echo "dir $d: MISSING"
  fi
done
echo "--- PDF counts (may take a moment) ---"
for root in "$HOME/e14/data/actas" "$HOME/e14" "$HOME"; do
  [[ -d "$root" ]] || continue
  n=$(find "$root" -path '*/actas/*.pdf' -o -path '*/data/actas/*/*.pdf' 2>/dev/null | wc -l)
  echo "  under $root → actas-like pdfs: $n"
done
echo "--- .env candidates ---"
find "$HOME/e14" "$HOME" -maxdepth 3 -name '.env' 2>/dev/null | head -5 || true
echo "--- largest dirs under ~/e14/data ---"
if [[ -d "$HOME/e14/data" ]]; then
  du -sh "$HOME/e14/data"/* 2>/dev/null | sort -hr | head -8 || true
fi
REMOTE
}

if [[ "${1:-}" == "--probe" ]]; then
  probe_remote
  echo
  echo "If paths differ, re-run with:"
  echo "  E14_LEAD_REPO=/path/on/lead/e14 bash scripts/pull_from_lead.sh"
  exit 0
fi

if [[ -z "$REPO" ]]; then
  REPO=$(ssh_cmd 'test -d "$HOME/e14" && echo "$HOME/e14"' || true)
fi
if [[ -z "$REPO" ]]; then
  echo "Cannot find ~/e14 on lead. Run: bash scripts/pull_from_lead.sh --probe" >&2
  exit 1
fi

ACTAS="$REPO/data/actas"
ENV_FILE="$REPO/.env"

mkdir -p data/actas

echo ">> Using lead repo: $REPO"
echo ">> Remote PDF count:"
pdf_n=$(ssh_cmd "find '$ACTAS' -name '*.pdf' 2>/dev/null | wc -l" || echo 0)
echo "$pdf_n"

if [[ "${pdf_n// /}" -lt 1000 ]]; then
  echo >&2
  echo "ERROR: lead has only $pdf_n PDFs under $ACTAS" >&2
  echo "  Run: bash scripts/pull_from_lead.sh --probe" >&2
  echo "  Or:  E14_LEAD_REPO=/correct/path bash scripts/pull_from_lead.sh" >&2
  exit 1
fi

if ssh_cmd "test -f '$ENV_FILE'"; then
  echo ">> Pulling .env"
  scp -P "$PORT" "$REMOTE:$ENV_FILE" .env
else
  echo ">> WARNING: no .env at $ENV_FILE on lead"
  echo "   Create locally: fly storage create  (or copy AWS_* + BUCKET_NAME + E14_CDN_BASE_URL)"
  echo "   Cropping works without .env; pull-db / publish need it."
fi

echo ">> Pulling data/actas/ (~22 GB, resumable)"
rsync_pull "$ACTAS/" data/actas/

echo ">> Local PDF count:"
find data/actas -name '*.pdf' | wc -l

if [[ -f .env ]]; then
  echo ">> pull-db test"
  .venv/bin/e14detector pull-db --output-dir data/detector_national
fi

echo ">> Done. Start crop: nohup bash scripts/start_crop_worker.sh >> logs/crop_supervisor.log 2>&1 & disown"
