#!/usr/bin/env bash
# One documented deploy entry point for the Phase 2 round split. Wraps the routine flyctl/secret
# steps so "when we're ready" is one obvious, low-risk command instead of a remembered sequence.
#
# Subcommands:
#   r2                 Routine deploy of the R2 (runoff) primary app (e14-poll), no secret changes.
#   r1-archive         Deploy the frozen first-round archive app (e14-r1-archive), no secret changes.
#   cutover-r2         THE deliberate flip: point e14-poll at round r2 + the R2 vote backend, then
#                      deploy. Irreversible-ish — read docs/CUTOVER.md first. Prompts before acting.
#   status             Show both apps' release/health at a glance.
#
# Nothing here runs automatically. Local-first: verify crops + stability locally (E14_ELECTION_ROUND=r2)
# BEFORE any deploy. See docs/CUTOVER.md for the full runbook and the values to fill in below.
set -euo pipefail

FLY="${FLY:-$HOME/.fly/bin/fly}"
POLL_APP="${E14_POLL_APP:-e14-poll}"            # the live app; becomes the R2 primary at cutover
ARCHIVE_APP="${E14_ARCHIVE_APP:-e14-r1-archive}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- values you must set for cutover (export them, or edit here) -------------------------------
# The R2 vote backend, from `cdk deploy E14VoteStackR2` outputs (infra/). Leave blank until ready.
R2_AURORA_CLUSTER_ARN="${R2_AURORA_CLUSTER_ARN:-}"
R2_AURORA_SECRET_ARN="${R2_AURORA_SECRET_ARN:-}"
R2_SQS_QUEUE_URL="${R2_SQS_QUEUE_URL:-}"
# Public URL of the deployed R1 archive (shown as the "primera vuelta" button on the R2 page).
R1_ARCHIVE_URL="${R1_ARCHIVE_URL:-https://${ARCHIVE_APP}.fly.dev}"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
die() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }
confirm() { read -r -p "$1 [type 'yes']: " a; [ "$a" = "yes" ] || die "aborted"; }

cmd="${1:-}"
case "$cmd" in
  r2)
    say "Deploying R2 primary ($POLL_APP) from fly.toml"
    "$FLY" deploy -c "$ROOT/fly.toml" -a "$POLL_APP"
    ;;

  r1-archive)
    say "Deploying R1 archive ($ARCHIVE_APP) from fly.r1-archive.toml"
    # First run also creates the app + volume; see docs/CUTOVER.md if this is the initial deploy.
    "$FLY" deploy -c "$ROOT/fly.r1-archive.toml" -a "$ARCHIVE_APP"
    ;;

  cutover-r2)
    say "CUTOVER: $POLL_APP becomes the R2 primary"
    echo "This flips the LIVE app to round r2 and its R2 vote backend. Read docs/CUTOVER.md first."
    [ -n "$R2_AURORA_CLUSTER_ARN" ] && [ -n "$R2_AURORA_SECRET_ARN" ] && [ -n "$R2_SQS_QUEUE_URL" ] \
      || die "set R2_AURORA_CLUSTER_ARN / R2_AURORA_SECRET_ARN / R2_SQS_QUEUE_URL first (cdk E14VoteStackR2 outputs)"
    confirm "Confirm cutover of $POLL_APP to r2"
    say "Setting R2 round + vote-backend secrets on $POLL_APP"
    "$FLY" secrets set -a "$POLL_APP" \
      E14_ELECTION_ROUND=r2 \
      E14_R1_ARCHIVE_URL="$R1_ARCHIVE_URL" \
      AURORA_CLUSTER_ARN="$R2_AURORA_CLUSTER_ARN" \
      AURORA_SECRET_ARN="$R2_AURORA_SECRET_ARN" \
      SQS_QUEUE_URL="$R2_SQS_QUEUE_URL"
    # `fly secrets set` already triggers a rolling release; deploy again only if the image changed.
    say "Cutover secrets applied. Verify: $0 status"
    ;;

  status)
    for app in "$POLL_APP" "$ARCHIVE_APP"; do
      say "$app"
      "$FLY" status -a "$app" || true
    done
    ;;

  *)
    grep -E '^#( |$)' "$0" | sed -E 's/^# ?//' | sed -n '1,30p'
    exit 1
    ;;
esac
